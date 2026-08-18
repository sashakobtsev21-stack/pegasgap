"""Непрерывный воркер: берёт кейс из очереди, проверяет, рассказывает о себе.

Устроен как цикл без конца, а не как «прогон матрицы»: очередь длиннее, чем сутки работы,
и смысл в том, чтобы она обходилась по кругу сама, а человек лишь смотрел на находки.

Три вещи, которые определяют его поведение.

**Никогда не падает целиком.** Сбой одного кейса — это сбой одного кейса: он логируется,
кейс отмечается проверенным (иначе воркер зациклится на нём) и цикл идёт дальше. Уронить
круглосуточный процесс из-за одного отвалившегося направления недопустимо.

**Уважает квоту.** Шлюз считает поиски по IP и при превышении отвечает отказом. Это не
повод молотить дальше: воркер делает паузу, иначе сожжёт лимит впустую и не проверит
ничего. Пауза видна в логе, чтобы молчание не выглядело зависанием.

**Говорит вслух.** Каждый шаг уходит в шину событий: что взял, что нашёл, чего ждёт. Без
этого круглосуточный процесс — чёрный ящик, по которому нельзя понять, работает он или
завис.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pegasgap import storage
from pegasgap.events import bus
from pegasgap.models import PEGAS, GapKind, ScanResult
from pegasgap.queue import Case, mark_checked, next_case, stats

log = logging.getLogger("pegasgap.worker")

# Пауза между кейсами. Не оптимизация, а вежливость к площадкам: очередь бесконечна,
# и выигрыш от спешки нулевой, а риск упереться в квоту — реальный.
PAUSE_BETWEEN_S = 3.0
# Пауза после отказа по квоте. Заметно длиннее обычной: смысл в том, чтобы дать лимиту
# восстановиться, а не постучаться ещё раз через секунду.
RATE_LIMIT_PAUSE_S = 120.0
# Нечего делать (очередь пуста или всё свежее) — засыпаем надолго.
IDLE_PAUSE_S = 60.0
# Не перепроверять кейс чаще этого срока.
MIN_RECHECK_HOURS = 12.0

_RATE_LIMIT_MARK = "превышен лимит"


@dataclass
class WorkerState:
    """Состояние воркера — то, что показывают счётчики и индикатор в шапке."""

    running: bool = False
    paused_until: datetime | None = None
    current: str | None = None
    started_at: datetime | None = None
    checked: int = 0
    gaps: int = 0
    errors: int = 0
    last_error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "current": self.current,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "paused_until": self.paused_until.isoformat() if self.paused_until else None,
            "checked": self.checked,
            "gaps": self.gaps,
            "errors": self.errors,
            "last_error": self.last_error,
        }


def _is_rate_limited(scan: ScanResult) -> bool:
    text = " ".join(scan.problems).lower()
    return _RATE_LIMIT_MARK in text


def _finding_events(case: Case, scan: ScanResult, run_id: int) -> None:
    """Отправить находки в шину — по одной, чтобы отчёт наполнялся на глазах."""
    if not scan.gaps:
        return
    for gap in scan.gaps:
        bus.publish(
            "finding",
            run_id=run_id,
            case_id=case.id,
            case=case.title,
            departure_city=case.departure_city,
            country=case.country,
            search_mode=case.search_mode,
            date_from=case.date_from.isoformat(),
            date_to=case.date_to.isoformat(),
            adults=case.adults,
            children_ages=case.children_ages,
            # Не `kind`: так называется тип самого события в шине, и совпадение имён
            # роняло публикацию с «got multiple values for argument».
            gap_kind=gap.kind.value,
            gap_kind_title=gap.kind.title,
            hotel_name=gap.hotel_name,
            stars=gap.stars,
            diagnosis=gap.diagnosis.value,
            diagnosis_title=gap.diagnosis.title,
            note=gap.note,
            trustworthy=scan.trustworthy,
        )


class Worker:
    """Обходчик очереди. Один на процесс."""

    def __init__(self, db_path: Path, operator: str = PEGAS, scan_runner=None,
                 pause_s: float = PAUSE_BETWEEN_S, idle_s: float = IDLE_PAUSE_S,
                 rate_limit_pause_s: float = RATE_LIMIT_PAUSE_S,
                 min_recheck_hours: float = MIN_RECHECK_HOURS) -> None:
        self.db_path = Path(db_path)
        self.operator = operator
        self.state = WorkerState()
        self._stop = asyncio.Event()
        # Инъекция ради тестов: настоящий прогон ходит в сеть, а проверять логику цикла
        # надо без неё. Паузы тоже параметры, а не константы: тест, подогнанный под
        # трёхсекундную задержку, ломается от любой её правки.
        self._run_scan = scan_runner
        self.pause_s = pause_s
        self.idle_s = idle_s
        self.rate_limit_pause_s = rate_limit_pause_s
        self.min_recheck_hours = min_recheck_hours

    async def _scan(self, case: Case):
        if self._run_scan is not None:
            return await self._run_scan(case)
        from pegasgap.web import run_scan  # локальный импорт: web тянет FastAPI
        return await run_scan(case.to_params(self.operator), self.operator, self.db_path)

    def start(self) -> bool:
        """Запустить цикл. False — уже работает."""
        if self.state.running:
            return False
        self._stop.clear()
        self.state = WorkerState(running=True, started_at=datetime.now())
        self.state.task = asyncio.create_task(self._loop())
        bus.log("Воркер запущен")
        bus.publish("state", **self.state.as_dict())
        return True

    async def stop(self) -> None:
        """Попросить остановиться и дождаться завершения текущего кейса.

        Именно дождаться, а не оборвать: прерванный на середине прогон оставил бы кейс
        непомеченным и наполовину записанным.
        """
        if not self.state.running:
            return
        bus.log("Остановка запрошена — доработаю текущий кейс")
        self._stop.set()
        if self.state.task is not None:
            await self.state.task

    async def _sleep(self, seconds: float) -> None:
        """Пауза, которую можно прервать остановкой."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                with storage.session(self.db_path) as conn:
                    case = next_case(conn, min_age_hours=self.min_recheck_hours)
                if case is None:
                    self.state.current = None
                    bus.log(f"Очередь пуста или всё проверено недавно — жду "
                            f"{self.idle_s:.0f} с", level="dim")
                    bus.publish("state", **self.state.as_dict())
                    await self._sleep(self.idle_s)
                    continue

                self.state.current = case.title
                bus.log(f"Проверяю: {case.title}")
                bus.publish("state", **self.state.as_dict())

                pause = self.pause_s
                try:
                    run_id, scan = await self._scan(case)
                except Exception as exc:
                    self.state.errors += 1
                    self.state.last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("кейс %s упал", case.title)
                    bus.log(f"Сбой на «{case.title}»: {self.state.last_error}",
                            level="error")
                    with storage.session(self.db_path) as conn:
                        # Помечаем даже упавший: иначе воркер вечно берёт его же.
                        mark_checked(conn, case.id, None, 0)
                else:
                    self.state.checked += 1
                    self.state.gaps += len(scan.gaps)
                    with storage.session(self.db_path) as conn:
                        mark_checked(conn, case.id, run_id, len(scan.gaps))
                    _finding_events(case, scan, run_id)
                    self._report(case, scan)
                    if _is_rate_limited(scan):
                        pause = self.rate_limit_pause_s
                        self.state.paused_until = datetime.now()
                        bus.log(f"Площадка ограничила частоту запросов — пауза "
                                f"{self.rate_limit_pause_s:.0f} с", level="warn")

                bus.publish("state", **self.state.as_dict())
                await self._sleep(pause)
        finally:
            self.state.running = False
            self.state.current = None
            bus.log("Воркер остановлен")
            bus.publish("state", **self.state.as_dict())

    def _report(self, case: Case, scan: ScanResult) -> None:
        """Одна строка итога в лог — чтобы по логу читалась картина без отчёта."""
        if not scan.trustworthy:
            bus.log(f"  ↳ прогон недостоверен: {'; '.join(scan.problems)}", level="warn")
            return
        if not scan.gaps:
            bus.log("  ↳ расхождений нет", level="dim")
            return
        by_kind: dict[str, int] = {}
        for gap in scan.gaps:
            by_kind[gap.kind.title] = by_kind.get(gap.kind.title, 0) + 1
        detail = ", ".join(f"{k}: {n}" for k, n in by_kind.items())
        level = "found" if any(g.kind is GapKind.HOTEL or g.kind is GapKind.FULL
                               for g in scan.gaps) else "info"
        bus.log(f"  ↳ найдено {len(scan.gaps)} ({detail})", level=level)

    def snapshot(self) -> dict:
        """Состояние воркера вместе со сводкой очереди."""
        with storage.session(self.db_path) as conn:
            queue_stats = stats(conn)
        return {**self.state.as_dict(), "queue": queue_stats}
