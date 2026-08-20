"""Причина отсутствия туров — со стороны самого поиска, а не справочников.

Справочники объясняют, почему отель НЕ МОГ появиться в выдаче. Но самый частый случай
другой: отель заведён, слинкован, направление открыто — а тура всё равно нет. Дальше
справочники молчат, и до сих пор инструмент останавливался на «линкован, тура нет»,
что для разбора почти бесполезно.

Ответ лежит в логах самого плагина, и он достижим: **наш анонимный поиск через шлюз
полностью прослеживается по `requestId`.** Шлюз возвращает его при старте поиска, плагин
пишет с ним каждую свою строку. Проверено живьём на 687744199 — по идентификатору
находится весь ход поиска, включая решающую строку:

    Tours Count From TO = 3260; Tour Count After Filtering = 3257;
    FilteredCount = 3; UnicHotelsCount = 720; UnicHotelsCountAfterFilering = 718

то есть сколько туров дал оператор и сколько потеряли уже мы.

Самая ценная находка — `Pegas search fan-out truncated to N of M`: плагин разбивает
поиск на запросы «дата × ночи» и отправляет не все. В логе прямо написано «Some
dates/durations will not be searched». За час это 15 235 раз, и худшие случаи — окно в
пятнадцать дней, ужатое до ОДНОЙ даты. Витрина ищет всё окно, мы — один день; отели,
доступные на остальные даты, в выдачу не попадают вовсе. Это не сбой инструмента и не
проблема справочников, а настоящая причина расхождения.

Сигнатуры взяты из таблицы «Причины, по которым тур теряется» во флоу-документации
плагинов (`ai-shared/context/plugins/PegasV4CommonPluginBase/flows/SearchFlow.md`) —
там же указаны места в коде, которые их печатают.

**Слой опциональный и выключен без доступа.** У логов своя авторизация, и вписывать
её сюда нельзя. Без доступа `requestId` всё равно сохраняется с каждым прогоном —
этого хватает, чтобы поднять историю поиска руками.

Разбор строк отделён от их получения: сигнатуры проверяются офлайн, целиком и без
доступов, потому что именно они решают, что будет написано в отчёте.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger("pegasgap.pluginlog")

# Настройки читаются ЛЕНИВО, при обращении: модульная константа замерзала бы до
# загрузки `.env` (cli подтягивает его после импортов) и слой молча оставался бы
# выключенным при заполненном файле.


def _kibana_url() -> str:
    # Фолбэк — переменная MCP-коннектора этой же машины.
    return (os.environ.get("PEGASGAP_KIBANA_URL")
            or os.environ.get("KIBANA_BASE_URL") or "").rstrip("/")


def _kibana_index() -> str:
    return os.environ.get("PEGASGAP_KIBANA_INDEX") or "gelf-slt-backend*"


def _max_lines() -> int:
    # Плагин пишет строки десятками, а не тысячами.
    return int(os.environ.get("PEGASGAP_KIBANA_LINES") or 200)


@dataclass(frozen=True)
class LogCause:
    """Что лог говорит о судьбе туров в этом поиске."""

    code: str
    title: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.title} — {self.detail}" if self.detail else self.title


def _fan_out(m: re.Match) -> str:
    sent, total = int(m.group(1)), int(m.group(2))
    return (f"отправлено {sent} запросов из {total} по датам и длительностям — "
            f"остальные даты окна не искались вовсе")


def _counts(m: re.Match) -> str:
    from_to, after = int(m.group(1)), int(m.group(2))
    lost = from_to - after
    tail = f", из них потеряно у нас {lost}" if lost else ""
    return f"оператор вернул {from_to} туров{tail}"


def _hotels(m: re.Match) -> str:
    before, after = int(m.group(1)), int(m.group(2))
    lost = before - after
    return (f"отелей в ответе оператора {before}, в выдачу попало {after}"
            + (f" — потеряно {lost}" if lost else ""))


# Сигнатура → как её назвать и что вытащить. Порядок не важен: строки независимы.
# Каждая причина названа тем, ЧТО случилось с турами, а не тем, как устроен плагин:
# читателю отчёта нужно «даты не искались», а не «веер обрезан капом».
_SIGNATURES: list[tuple[str, str, re.Pattern[str], object]] = [
    ("fan_out_truncated", "часть дат окна не искалась",
     re.compile(r"fan-out truncated to (\d+) of (\d+)"), _fan_out),
    ("counts", "объём ответа оператора",
     re.compile(r"Tours Count From TO\s*=\s*(\d+).*?Tour Count After Filtering\s*=\s*(\d+)",
                re.DOTALL), _counts),
    ("hotels_lost", "отели потеряны при обработке",
     re.compile(r"UnicHotelsCount\s*=\s*(\d+).*?UnicHotelsCountAfterFilering\s*=\s*(\d+)",
                re.DOTALL), _hotels),
    ("gate_skipped", "поиск пропущен по календарю доступности",
     re.compile(r"No available dates in requested window, search skipped"), None),
    ("gate_no_dates", "не нашлось доступных дат и длительностей",
     re.compile(r"No available dates/nights found for request"), None),
    ("batch_failed", "подзапрос к оператору не прошёл",
     re.compile(r"search batch failed"), None),
    ("hotels_unavailable", "отели выкинуты как недоступные у оператора",
     re.compile(r"Founded (\d+) hotels that are not available from TO"),
     lambda m: f"{m.group(1)} отел(ь/я/ей) нет в кэше доступных у оператора"),
    ("no_ranges", "запрос отброшен целиком — нет линковки",
     re.compile(r"Ranges count:\s*0\b"), None),
    ("no_link_hotel", "тур отброшен: у отеля нет линковки",
     re.compile(r"Tour Hotel does not have a link"), None),
    ("parse_error", "тур не разобрался и потерян",
     re.compile(r"Error while parsing tour"), None),
    ("towns_unresolved", "курорты не опознаны в справочнике плагина",
     re.compile(r"Local towns doesn't contain any local keys"), None),
    # Две сигнатуры сняты с ЖИВОГО прогона Sunmar (сверка Е4 через Kibana): это причины
    # других семейств плагинов, во флоу-доке Pegas их нет.
    ("rows_capped", "выдача оператора упёрлась в лимит строк",
     re.compile(r"Reached max recommended rows count limit \((\d+)\)"),
     lambda m: f"плагин остановил добор на {m.group(1)} строках — хвост выдачи не читался"),
    ("no_linked_hotels", "поиск шёл без фильтра отелей",
     re.compile(r"Empty HotelIds in linked data"), None),
    ("destinations_unavailable", "курорты недоступны у оператора",
     re.compile(r"Destinations with ids: .* are not available"), None),
]


def parse_lines(messages: list[str]) -> list[LogCause]:
    """Строки лога → причины. Повторы схлопываются с подсчётом.

    Одна и та же причина приходит десятками строк (по одной на подзапрос или на тур), и
    вываливать их в отчёт по одной значит утопить всё остальное.
    """
    found: dict[str, tuple[LogCause, int]] = {}
    for message in messages:
        for code, title, pattern, extract in _SIGNATURES:
            match = pattern.search(message or "")
            if not match:
                continue
            detail = extract(match) if extract else ""
            if code in found:
                cause, count = found[code]
                found[code] = (cause, count + 1)
            else:
                found[code] = (LogCause(code=code, title=title, detail=detail), 1)

    out: list[LogCause] = []
    for cause, count in found.values():
        if count > 1:
            detail = f"{cause.detail}; повторов {count}" if cause.detail else f"повторов {count}"
            out.append(LogCause(code=cause.code, title=cause.title, detail=detail))
        else:
            out.append(cause)
    return out


def _auth() -> dict[str, str] | tuple[str, str] | None:
    """Как авторизоваться в логах. None — слой выключен.

    Ни ключ, ни пароль сюда не попадают иначе как из окружения и никуда не логируются.
    """
    api_key = os.environ.get("PEGASGAP_KIBANA_API_KEY")
    if api_key:
        return {"Authorization": f"ApiKey {api_key}"}
    user = (os.environ.get("PEGASGAP_KIBANA_USER")
            or os.environ.get("KIBANA_USERNAME"))
    password = (os.environ.get("PEGASGAP_KIBANA_PASSWORD")
                or os.environ.get("KIBANA_PASSWORD"))
    if user and password:
        return (user, password)
    return None


def available() -> bool:
    return bool(_kibana_url()) and _auth() is not None


async def fetch_causes(request_id: int | None) -> list[LogCause]:
    """Причины из логов плагина по идентификатору нашего поиска.

    Пустой список означает «не смотрели или не нашли» и НЕ означает «всё в порядке».
    Различать это должен вызывающий: выдать «причин нет» там, где мы просто не имели
    доступа, значит соврать увереннее, чем промолчать.
    """
    if not request_id or not available():
        return []

    import httpx

    auth = _auth()
    headers = {"kbn-xsrf": "true", "Content-Type": "application/json"}
    if isinstance(auth, dict):
        headers.update(auth)
        basic = None
    else:
        basic = auth

    query = {
        "size": _max_lines(),
        "query": {"bool": {"must": [{"match_phrase": {"message": str(request_id)}}]}},
        "sort": [{"@timestamp": {"order": "asc"}}],
    }
    url = (f"{_kibana_url()}/api/console/proxy"
           f"?path={_kibana_index()}/_search&method=POST")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=query, headers=headers, auth=basic)
            response.raise_for_status()
            hits = response.json().get("hits", {}).get("hits", [])
    except Exception as exc:
        log.warning("логи плагина недоступны (%s) — причина без них", type(exc).__name__)
        return []

    messages = [str((h.get("_source") or {}).get("message") or "") for h in hits]
    causes = parse_lines(messages)
    log.info("логи поиска %s: строк %d, причин %d", request_id, len(messages), len(causes))
    return causes
