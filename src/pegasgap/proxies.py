"""Пул прокси: обе площадки считают запросы по IP, и с одного адреса обход не проходит.

Турвизор отвечает `HTTP 401 Invalid session`, шлюз Слетать — «превышен лимит кол-ва
поисковых запросов». На прямом адресе обход вставал на первом десятке кейсов, а с
постраничным сбором эталона (до десяти запусков поиска на кейс) стал вставать ещё раньше.

**Прокси выдаётся на весь поиск, а не на запрос.** Постраничный сбор ходит по одному
`requestid`, и витрина связывает его с адресом: сменить IP на середине — гарантированно
получить чужую или пустую страницу. Поэтому `acquire()` берётся один раз в начале поиска
и держится до конца.

**Креды никогда не попадают в логи.** `str(proxy)` — это `host:port` без пары
логин-пароль, и именно эта форма используется во всех сообщениях. Полный URL живёт только
в `url`, который уходит в httpx и больше никуда.
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("pegasgap.proxies")

DEFAULT_FILE = Path(os.environ.get("PEGASGAP_PROXIES") or "proxies.txt")

# На сколько прокси уходит в остывание после отказа площадки. Он не сломан — он засвечен,
# и через несколько минут снова годен. Выбрасывать его насовсем значило бы за один обход
# сточить весь список.
COOLDOWN = timedelta(minutes=float(os.environ.get("PEGASGAP_PROXY_COOLDOWN_MIN") or 10))


# Признаки того, что площадка отшила именно АДРЕС, а не запрос. По ним прокси уходит
# остывать; всё прочее (кривые параметры, пустая выдача, таймаут поиска) к адресу
# отношения не имеет, и наказывать за это значило бы сточить пул на ровном месте.
_BLOCK_MARKS = (
    "401",                 # Турвизор: Invalid session
    "403",
    "429",
    "превышен лимит",      # шлюз Слетать
    "invalid session",
)


def is_blocked(error: str) -> bool:
    """Похоже ли, что площадка забанила адрес, а не отвергла сам запрос."""
    text = (error or "").lower()
    return any(mark in text for mark in _BLOCK_MARKS)


@dataclass(frozen=True)
class Proxy:
    """Один прокси. Печатается БЕЗ кредов — это его единственная форма для логов."""

    host: str
    port: int
    user: str = ""
    password: str = ""

    @property
    def url(self) -> str:
        """Адрес для httpx. Содержит пару логин-пароль, поэтому в логи не годится."""
        auth = f"{self.user}:{self.password}@" if self.user else ""
        return f"http://{auth}{self.host}:{self.port}"

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


def parse_line(line: str) -> Proxy | None:
    """Строка `host:port:user:pass` (или `host:port`) → прокси. Мусор — None.

    Формат ровно тот, в котором прокси обычно и выдают, чтобы список можно было вставить
    в файл как есть, ничего не переписывая.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(":")
    if len(parts) not in (2, 4):
        log.warning("строка прокси не разобрана (нужно host:port или host:port:user:pass)")
        return None
    host, port = parts[0].strip(), parts[1].strip()
    if not host or not port.isdigit():
        log.warning("строка прокси не разобрана: неверный host или port")
        return None
    user, password = (parts[2], parts[3]) if len(parts) == 4 else ("", "")
    return Proxy(host=host, port=int(port), user=user, password=password)


class ProxyPool:
    """Круговая выдача прокси с остыванием засвеченных.

    Потокобезопасен: воркер и точечные проверки живут в одном процессе, но в разных
    задачах, и общий счётчик без замка выдавал бы один адрес двоим сразу.
    """

    def __init__(self, proxies: list[Proxy]) -> None:
        self._proxies = list(proxies)
        self._cycle = itertools.cycle(range(len(self._proxies))) if self._proxies else None
        self._cooling: dict[Proxy, datetime] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._proxies)

    def __bool__(self) -> bool:
        return bool(self._proxies)

    def acquire(self, now: datetime | None = None) -> Proxy | None:
        """Следующий годный прокси. None — пул пуст (работаем напрямую).

        Если остыть не успел никто, отдаём наименее давно засвеченный, а не None: пауза
        всего обхода хуже, чем повторная попытка через подостывший адрес.
        """
        now = now or datetime.now()
        with self._lock:
            if not self._proxies or self._cycle is None:
                return None
            for _ in range(len(self._proxies)):
                candidate = self._proxies[next(self._cycle)]
                until = self._cooling.get(candidate)
                if until is None or until <= now:
                    self._cooling.pop(candidate, None)
                    return candidate
            oldest = min(self._cooling, key=lambda p: self._cooling[p])
            log.warning("все %d прокси остывают — беру самый давний, %s",
                        len(self._proxies), oldest)
            return oldest

    def penalise(self, proxy: Proxy | None, now: datetime | None = None) -> None:
        """Отправить засвеченный прокси остывать."""
        if proxy is None:
            return
        now = now or datetime.now()
        with self._lock:
            self._cooling[proxy] = now + COOLDOWN
        log.info("прокси %s остывает до %s", proxy, (now + COOLDOWN).strftime("%H:%M:%S"))

    def stats(self, now: datetime | None = None) -> dict:
        """Сводка для дашборда: сколько всего и сколько сейчас годны."""
        now = now or datetime.now()
        with self._lock:
            cooling = sum(1 for until in self._cooling.values() if until > now)
        return {"total": len(self._proxies), "cooling": cooling,
                "available": len(self._proxies) - cooling}


def load_pool(path: Path | str | None = None) -> ProxyPool:
    """Прочитать список прокси. Файла нет — пустой пул, и это не ошибка.

    Без прокси инструмент работает, просто упирается в лимиты площадок гораздо раньше.
    Падать на старте из-за отсутствующего файла значило бы сделать их обязательными.
    """
    path = Path(path or DEFAULT_FILE)
    if not path.exists():
        log.info("файла прокси нет (%s) — работаю напрямую", path)
        return ProxyPool([])
    proxies = [p for p in (parse_line(line)
                           for line in path.read_text(encoding="utf-8").splitlines())
               if p is not None]
    log.info("прокси загружено: %d", len(proxies))
    return ProxyPool(proxies)


# Один пул на процесс: круговая выдача имеет смысл только когда счётчик общий.
_POOL: ProxyPool | None = None


def pool() -> ProxyPool:
    global _POOL
    if _POOL is None:
        _POOL = load_pool()
    return _POOL


def reload_pool(path: Path | str | None = None) -> ProxyPool:
    """Перечитать файл — чтобы добавить прокси не перезапуская сервер."""
    global _POOL
    _POOL = load_pool(path)
    return _POOL
