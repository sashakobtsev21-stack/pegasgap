"""Веб-интерфейс: FastAPI + собранный React-дашборд под `/app`.

Авторизации нет намеренно. Инструмент внутренний, поднимается локально или на служебной
машине, и данные в нём — публичная выдача двух витрин. Экран входа тут защищал бы не от
кого, но добавлял бы состояние, сессии и права, которые надо поддерживать. Если однажды
понадобится вынести дашборд наружу — авторизация станет отдельной задачей со своей
моделью угроз, а не декорацией, добавленной заранее.

Прогон одного направления занимает секунды, поэтому `POST /api/scan` синхронный: живой
поток событий (SSE) здесь стоил бы дороже, чем даёт. Обход матрицы — минуты, и он
наоборот фоновый: держать его на открытой вкладке значит терять ночной обход при
закрытии браузера.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pegasgap import queue as case_queue
from pegasgap import storage
from pegasgap.catalog import fetch_catalog, resolve_country_id
from pegasgap.diagnosis import diagnose, diagnose_reverse, reverse_index
from pegasgap.events import bus
from pegasgap.gaps import detect
from pegasgap.hotelcheck import verify_hotel_gaps
from pegasgap.linking import load_direction, load_links
from pegasgap.models import (
    PEGAS,
    GapKind,
    HotelDiagnosis,
    ScanResult,
    SearchParams,
)
from pegasgap.orchestrator import CHECKED, REFERENCE, run_pair
from pegasgap.pluginlog import fetch_causes
from pegasgap.providers.tourvisor_api import fetch_country_hotels
from pegasgap.proxies import pool, reload_pool
from pegasgap.reversecheck import verify_reverse
from pegasgap.roomcheck import pin_rooms
from pegasgap.scenarios import DEFAULT_CONFIG, load_matrix
from pegasgap.searchlink import search_url_from_row
from pegasgap.worker import Worker

log = logging.getLogger("pegasgap.web")

# Запасные справочники на случай, если шлюз не ответил: форма должна остаться рабочей.
_FALLBACK_COUNTRIES = ["Турция", "Египет", "ОАЭ", "Таиланд", "Вьетнам", "Мальдивы"]
_FALLBACK_CITIES = ["Москва", "Санкт-Петербург", "Екатеринбург", "Казань", "Новосибирск"]


class ScanRequest(BaseModel):
    country: str
    departure: str = "Москва"
    date_from: date
    date_to: date
    nights: int = Field(default=7, ge=1, le=30)
    adults: int = Field(default=2, ge=1, le=6)
    mode: str = "tours"
    # Оператор выбирается на форме: их несколько, и точечная проверка чаще всего нужна
    # именно чтобы разобрать жалобу по конкретному ТО. Пусто — берём первый из конфига.
    operator: str | None = None

    def to_params(self, operator: str) -> SearchParams:
        if self.mode not in ("tours", "hotels"):
            raise HTTPException(400, "режим должен быть tours или hotels")
        try:
            return SearchParams(
                departure_city=self.departure, destination_country=self.country,
                date_from=self.date_from, date_to=self.date_to,
                nights_min=self.nights, nights_max=self.nights,
                adults=self.adults, search_mode=self.mode,  # type: ignore[arg-type]
                operators=[operator],
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


class SweepState:
    """Состояние фонового обхода. Одно на процесс — параллельные обходы не нужны.

    Второй обход поверх идущего удвоил бы нагрузку на обе площадки и перемешал результаты
    в одной ленте, поэтому запуск при активном обходе отклоняется, а не ставится в очередь.
    """

    def __init__(self) -> None:
        self.running = False
        self.total = 0
        self.done = 0
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.results: list[dict] = []
        self.error: str | None = None
        # Ссылку на фоновую задачу держим обязательно: asyncio хранит только слабую, и
        # без сильной ссылки сборщик мусора вправе прибить обход на середине.
        self.task: asyncio.Task | None = None

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "total": self.total,
            "done": self.done,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "results": self.results,
            "error": self.error,
        }


async def _diagnose(scan: ScanResult) -> None:
    """Разобрать причины отельных пропусков. Недоступность справочников не фатальна."""
    # Номера ценовых находок сверяются с витриной ДО раннего выхода: прогону с одними
    # ценовыми находками отельный разбор не нужен, а сверка номеров — нужна.
    await pin_rooms(scan)
    # Обратная сторона разбирается по словарю ВИТРИНЫ: «нет на Турвизоре» без причины
    # читается как ошибка инструмента, что показал живой Atlantis Royal.
    if scan.gaps_of(GapKind.REVERSE):
        their_hotels = await fetch_country_hotels(scan.params.destination_country)
        diagnose_reverse(scan, reverse_index(their_hotels))
        # «Нет в листинге» ещё не «нет»: прижатая проба подтверждает или снимает.
        await verify_reverse(scan)
    if not scan.gaps_of(GapKind.HOTEL):
        return
    country_id = await resolve_country_id(scan.params.destination_country)
    catalog = await fetch_catalog(country_id) if country_id else []
    links = await asyncio.to_thread(load_links, scan.operator)
    direction = await asyncio.to_thread(
        load_direction, scan.operator, scan.params.departure_city,
        scan.params.destination_country)
    diagnose(scan, catalog, links, direction)
    # Главный класс находок подтверждается пробой шлюза — после диагностики: пробуются
    # только уверенно опознанные отели, а опознание даёт она.
    await verify_hotel_gaps(scan)
    # Причина со стороны самого поиска: справочники объясняют, почему отель
    # НЕ МОГ появиться, а логи — почему его не оказалось при живом поиске.
    for cause in await fetch_causes(scan.checked_request_id):
        scan.notes.append(f"логи поиска: {cause}")


async def run_scan(params: SearchParams, operator: str, db_path: Path,
                   headless: bool = True,
                   scenario_key: str | None = None) -> tuple[int, ScanResult]:
    """Прогнать одно направление, разобрать причины и сохранить. Возвращает (id, итог).

    `scenario_key` — устойчивый ключ кейса очереди для истории возраста; без него
    берётся параметрный (годится для точечных прогонов, где история не главное).
    """
    results = await run_pair(params, headless=headless)
    scan = detect(params, results.get(REFERENCE), results.get(CHECKED), operator=operator)
    scan.scenario_key = scenario_key
    await _diagnose(scan)
    with storage.session(db_path) as conn:
        run_id = storage.save_scan(conn, scan)
    return run_id, scan


def _gap_dict(row: Any) -> dict:
    kind = GapKind(row["kind"])
    diagnosis = HotelDiagnosis(row["diagnosis"] or "unknown")
    reference = float(row["reference_price"]) if row["reference_price"] is not None else None
    checked = float(row["checked_price"]) if row["checked_price"] is not None else None
    diff = None
    if reference and checked is not None:
        diff = (checked - reference) / reference * 100
    return {
        "kind": kind.value,
        "kind_title": kind.title,
        "hotel_name": row["hotel_name"],
        "stars": row["stars"],
        "resort": row["resort"],
        "reference_price": reference,
        "checked_price": checked,
        "diff_pct": diff,
        "note": row["note"],
        # Вердикт «не проверялось» на экран не выносим: пустая колонка честнее подписи,
        # которая выглядит как результат разбора.
        "diagnosis": diagnosis.value if diagnosis is not HotelDiagnosis.UNKNOWN else None,
        "diagnosis_title": diagnosis.title if diagnosis is not HotelDiagnosis.UNKNOWN else None,
        "catalog_id": row["catalog_id"],
        "catalog_name": row["catalog_name"],
    }


def _run_dict(run: Any, gaps: list[Any]) -> dict:
    params = json.loads(run["params_json"])
    diagnoses: dict[str, int] = {}
    for g in gaps:
        if g["kind"] == GapKind.HOTEL.value and g["diagnosis"] != HotelDiagnosis.UNKNOWN.value:
            diagnoses[g["diagnosis"]] = diagnoses.get(g["diagnosis"], 0) + 1
    return {
        "run_id": run["id"],
        "run_at": run["run_at"],
        "operator": run["operator"],
        "params": params,
        "reference_status": run["reference_status"],
        "checked_status": run["checked_status"],
        "reference_hotels": run["reference_hotels"],
        "checked_hotels": run["checked_hotels"],
        "matched_hotels": run["matched_hotels"],
        "price_offset_pct": run["price_offset_pct"],
        "trustworthy": bool(run["trustworthy"]),
        "problems": json.loads(run["problems"]),
        "notes": json.loads(run["notes"] or "[]"),
        "unmatched": json.loads(run["unmatched"]),
        "gaps": [_gap_dict(g) for g in gaps],
        # Что делать — сводкой на прогон, а не повтором в каждой строке таблицы.
        "actions": [
            {"title": HotelDiagnosis(k).title, "count": n, "action": HotelDiagnosis(k).action}
            for k, n in sorted(diagnoses.items(), key=lambda kv: -kv[1])
        ],
    }


def create_app(db_path: str | Path = storage.DEFAULT_DB,
               config_path: str | Path = DEFAULT_CONFIG,
               operator: str = PEGAS) -> FastAPI:
    app = FastAPI(title="Gap Monitor", docs_url="/api/docs", redoc_url=None)
    db_path = Path(db_path)
    sweep = SweepState()

    # Список операторов берём из конфига: обход идёт по всем, и точечная проверка должна
    # уметь то же самое. Конфиг может не читаться (его правят руками) — тогда работаем
    # с одним оператором, а не падаем на старте.
    try:
        operators = load_matrix(config_path).operators or [operator]
    except (OSError, ValueError) as exc:
        log.warning("конфиг сценариев не прочитан (%s) — только «%s»", exc, operator)
        operators = [operator]
    if operator not in operators:
        operator = operators[0]

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/refdata")
    async def refdata() -> dict:
        """Списки для формы. Тянем из шлюза, при неудаче — запасные значения."""
        countries, cities = _FALLBACK_COUNTRIES, _FALLBACK_CITIES
        try:
            import httpx

            from pegasgap.providers.sletat_api import BASE_URL, REFERER
            async with httpx.AsyncClient(timeout=30, headers={"Referer": REFERER}) as client:
                cr = await client.get(f"{BASE_URL}/GetCountries")
                dr = await client.get(f"{BASE_URL}/GetDepartCities")
                cs = ((cr.json().get("GetCountriesResult") or {}).get("Data")) or []
                ds = ((dr.json().get("GetDepartCitiesResult") or {}).get("Data")) or []
                if cs:
                    countries = sorted({str(x["Name"]).strip() for x in cs if x.get("Name")})
                if ds:
                    cities = sorted({str(x["Name"]).strip() for x in ds if x.get("Name")})
        except Exception as exc:
            log.warning("справочники недоступны (%s) — отдаю запасные", type(exc).__name__)
        return {"countries": countries, "departure_cities": cities,
                "operator": operator, "operators": operators}

    @app.post("/api/scan")
    async def scan(req: ScanRequest) -> dict:
        wanted = (req.operator or "").strip() or operator
        if wanted not in operators:
            raise HTTPException(
                400, f"оператор «{wanted}» не в списке: {', '.join(operators)}")
        params = req.to_params(wanted)
        run_id, result = await run_scan(params, wanted, db_path)
        return {"run_id": run_id, "gaps": len(result.gaps), "trustworthy": result.trustworthy}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: int) -> dict:
        with storage.session(db_path) as conn:
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise HTTPException(404, f"прогон #{run_id} не найден")
            gaps = storage.gaps_of_run(conn, run_id)
            return _run_dict(run, gaps)

    @app.get("/api/history")
    async def history(days: int = 7, standing: int = 3) -> dict:
        since = datetime.now() - timedelta(days=max(1, days))
        with storage.session(db_path) as conn:
            counts = storage.summary_since(conn, since)
            runs = storage.runs_since(conn, since)
            gap_counts = {
                r["run_id"]: r["n"] for r in conn.execute(
                    "SELECT run_id, COUNT(*) AS n FROM gaps GROUP BY run_id")
            }
            old = storage.standing_gaps(conn, standing)
            return {
                "days": days,
                "trustworthy_runs": sum(1 for r in runs if r["trustworthy"]),
                "summary": [
                    {"kind": k.value, "title": k.title, "hint": k.hint,
                     "count": counts.get(k.value, 0)}
                    for k in GapKind
                ],
                "runs": [
                    {
                        "run_id": r["id"], "run_at": r["run_at"],
                        "departure_city": r["departure_city"],
                        "destination_country": r["destination_country"],
                        "date_from": r["date_from"], "date_to": r["date_to"],
                        "search_mode": r["search_mode"],
                        "trustworthy": bool(r["trustworthy"]),
                        "gaps": gap_counts.get(r["id"], 0),
                    }
                    for r in runs
                ],
                "standing": [
                    {"scenario_key": s["scenario_key"], "gap_key": s["gap_key"],
                     "first_seen": s["first_seen"], "times_seen": s["times_seen"]}
                    for s in old[:50]
                ],
            }

    @app.get("/api/sweep/matrix")
    async def sweep_matrix() -> dict:
        try:
            matrix = load_matrix(config_path)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        items = matrix.build(date.today())
        return {
            "operator": ", ".join(matrix.operators),
            "operators": matrix.operators,
            "countries": matrix.countries,
            "departure_cities": matrix.departure_cities,
            "modes": matrix.modes,
            "total": len(items),
            # Развёрнутый список, а не только его размер. «12 сценариев» ничего не говорит
            # о том, что именно проверится, а окна дат вдобавок считаются от дня запуска —
            # по конфигу их не прочитать, там смещения.
            "scenarios": [
                {
                    "departure_city": p.departure_city,
                    "country": p.destination_country,
                    "mode": p.search_mode,
                    "date_from": p.date_from.isoformat(),
                    "date_to": p.date_to.isoformat(),
                    "nights": p.nights_min,
                    "adults": p.adults,
                }
                for p in items
            ],
        }

    @app.get("/api/sweep")
    async def sweep_status() -> dict:
        return sweep.as_dict()

    @app.post("/api/sweep")
    async def sweep_start() -> dict:
        if sweep.running:
            raise HTTPException(409, "обход уже идёт")
        try:
            matrix = load_matrix(config_path)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        items = matrix.build(date.today())

        sweep.running = True
        sweep.total = len(items)
        sweep.done = 0
        sweep.results = []
        sweep.error = None
        sweep.started_at = datetime.now()
        sweep.finished_at = None

        async def worker() -> None:
            # Ограничение параллельности: каждый сценарий — это два поиска, а в режиме
            # «Отели» ещё и браузер. Без предела десяток сценариев съест память машины.
            sem = asyncio.Semaphore(4)

            async def one(p: SearchParams) -> None:
                try:
                    async with sem:
                        run_id, res = await run_scan(p, p.operators[0], db_path)
                    sweep.results.append({
                        "run_id": run_id, "country": p.destination_country,
                        "departure_city": p.departure_city, "mode": p.search_mode,
                        "gaps": len(res.gaps), "trustworthy": res.trustworthy,
                    })
                except Exception as exc:
                    log.warning("сценарий %s упал: %s", p.scenario_key(), exc)
                    sweep.results.append({
                        "run_id": None, "country": p.destination_country,
                        "departure_city": p.departure_city, "mode": p.search_mode,
                        "gaps": 0, "trustworthy": False, "error": str(exc),
                    })
                finally:
                    sweep.done += 1

            try:
                await asyncio.gather(*(one(p) for p in items))
            except Exception as exc:
                sweep.error = str(exc)
            finally:
                sweep.running = False
                sweep.finished_at = datetime.now()

        sweep.task = asyncio.create_task(worker())
        return {"started": True, "total": sweep.total}

    worker = Worker(db_path=db_path, operator=operator)

    def sse(payload: dict) -> str:
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    @app.post("/api/logs/clear")
    async def logs_clear() -> dict:
        """Очистить накопленный лог. На идущий обход не влияет."""
        return {"cleared": bus.clear()}

    @app.get("/api/proxies")
    async def proxies_state() -> dict:
        """Сколько прокси в пуле и сколько сейчас годны. Адреса наружу не отдаём."""
        return pool().stats()

    @app.post("/api/proxies/reload")
    async def proxies_reload() -> dict:
        """Перечитать файл — чтобы добавить прокси не перезапуская сервер."""
        stats = reload_pool().stats()
        log.info("пул прокси перечитан: %s", stats)
        return stats

    @app.get("/api/worker")
    async def worker_state() -> dict:
        return worker.snapshot()

    @app.post("/api/worker/start")
    async def worker_start() -> dict:
        if not worker.start():
            raise HTTPException(409, "воркер уже работает")
        return worker.snapshot()

    @app.post("/api/worker/stop")
    async def worker_stop() -> dict:
        await worker.stop()
        return worker.snapshot()

    @app.get("/api/stream")
    async def stream() -> StreamingResponse:
        """Живой поток событий: строки лога и находки по мере появления.

        Опрос состояния отвечает на вопрос «сколько сделано», но не на «что происходит».
        Для процесса, который работает часами, второе важнее: если он молчит десять
        минут, надо видеть, ждёт он площадку или упёрся в квоту.
        """
        async def events():
            queue = bus.subscribe()
            try:
                # Хвост лога сразу: подключившийся в середине прогона должен увидеть
                # контекст, а не пустой экран.
                for event in bus.history():
                    yield sse(event)
                yield sse({"kind": "state", **worker.snapshot()})
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=20)
                    except TimeoutError:
                        # Пульс: держит соединение живым через прокси, которые рвут
                        # простаивающие потоки.
                        yield ": ping\n\n"
                        continue
                    yield sse(event)
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # nginx не должен буферизовать поток
        })

    @app.get("/api/queue")
    async def queue_list(limit: int = 200) -> dict:
        """Сводка очереди. `limit=0` — без поимённого списка кейсов.

        Дашборду список не нужен: кейсов тысячи, и он показывает сводку по маршрутам.
        Поимённый перечень остаётся для отладки и внешних запросов.
        """
        with storage.session(db_path) as conn:
            cases = case_queue.list_cases(conn, limit=limit) if limit > 0 else []
            return {
                "stats": case_queue.stats(conn),
                "dimensions": case_queue.dimensions(conn),
                "composition": case_queue.composition(conn),
                "cases": [
                    {
                        "id": c.id, "title": c.title,
                        "operator": c.operator,
                        "departure_city": c.departure_city, "country": c.country,
                        "search_mode": c.search_mode,
                        "date_from": c.date_from.isoformat(),
                        "date_to": c.date_to.isoformat(),
                        "adults": c.adults, "children_ages": c.children_ages,
                        "priority": c.priority,
                        "last_checked": (c.last_checked.isoformat()
                                         if c.last_checked else None),
                        "checks": c.checks, "gaps_found": c.gaps_found,
                    }
                    for c in cases
                ],
            }

    @app.post("/api/queue/seed")
    async def queue_seed() -> dict:
        """Пересобрать очередь из scenarios.yaml.

        Существующие кейсы не дублируются и не теряют историю проверок — обновляется
        только приоритет.
        """
        try:
            matrix = load_matrix(config_path)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        with storage.session(db_path) as conn:
            seeded, retired = case_queue.seed_from_matrix(conn, matrix)
            queue_stats = case_queue.stats(conn)
        # Число «погашенных» кейсов наружу не показываем: оно почти всегда равно размеру
        # прежней очереди (окна дат считаются от дня сборки, и назавтра ключи другие),
        # читается как «половина очереди сломалась» и объясняет только внутреннюю кухню.
        bus.log(f"Очередь собрана: {seeded} кейсов (погашено прежних: {retired})")
        return {"seeded": seeded, "retired": retired, "stats": queue_stats}

    @app.get("/api/findings")
    async def findings(days: int = 7, only_open: bool = False, limit: int = 500,
                       min_times: int = 1, operator: str = "", departure_city: str = "",
                       country: str = "", kind: str = "", diagnosis: str = "") -> dict:
        since = datetime.now() - timedelta(days=max(1, days))
        filters = {"operator": operator, "departure_city": departure_city,
                   "country": country, "kind": kind, "diagnosis": diagnosis}
        with storage.session(db_path) as conn:
            rows = storage.findings(conn, since, only_open=only_open, limit=limit,
                                    min_times=min_times, filters=filters)
            summary = storage.findings_summary(conn, since, filters=filters)
            queue_stats = case_queue.stats(conn)
            failed = storage.failed_runs(conn, since, filters=filters)
            facets = storage.finding_facets(conn, since)
        return {
            "summary": {**summary, "queue": queue_stats},
            # Списки для фильтров — только то, что реально есть в отчёте за период.
            "facets": {
                **facets,
                # «не проверялось» из списка причин убрано: это не причина, а её
                # отсутствие — диагностика по справочнику не запускалась. Фильтровать
                # по ней значит отбирать находки, про которые ничего не известно.
                "diagnoses": [d for d in facets["diagnoses"]
                              if d != HotelDiagnosis.UNKNOWN.value],
                "kind_titles": {k.value: k.title for k in GapKind},
                "diagnosis_titles": {d.value: d.title for d in HotelDiagnosis},
            },
            # Непроверенное — рядом с отчётом, а не на отдельной вкладке: без него не
            # видно, покрыто ли направление вообще.
            "failed": [
                {
                    "run_id": r["id"], "run_at": r["run_at"], "operator": r["operator"],
                    "departure_city": r["departure_city"],
                    "country": r["destination_country"],
                    "search_mode": r["search_mode"],
                    "problems": json.loads(r["problems"] or "[]"),
                }
                for r in failed
            ],
            "findings": [
                {
                    "id": r["id"], "run_id": r["run_id"], "run_at": r["run_at"],
                    "operator": r["operator"],
                    "departure_city": r["departure_city"],
                    "country": r["destination_country"],
                    "search_mode": r["search_mode"],
                    "date_from": r["run_date_from"], "date_to": r["run_date_to"],
                    "params": json.loads(r["params_json"]),
                    # Ссылка на тот же поиск: без неё находка проверяется только
                    # повторением поиска руками по десятку полей формы.
                    "search_url": search_url_from_row(json.loads(r["params_json"]),
                                                     r["catalog_id"],
                                                     r["checked_checkin"]),
                    # Ссылка на витрину: базовый адрес прогона плюс конкретный отель.
                    "reference_url": (
                        f'{r["reference_url"]}&x_hotel_codes={r["reference_hotel_id"]}'
                        if r["reference_url"] and r["reference_hotel_id"]
                        else r["reference_url"]),
                    "reference_checkin": r["reference_checkin"],
                    "checked_checkin": r["checked_checkin"],
                    "checked_meal": r["checked_meal"],
                    "checked_room": r["checked_room"],
                    "reference_room": r["reference_room"],
                    "kind": r["kind"],
                    "kind_title": GapKind(r["kind"]).title,
                    "hotel_name": r["hotel_name"], "stars": r["stars"],
                    "diagnosis_title": (
                        HotelDiagnosis(r["diagnosis"]).title
                        if r["diagnosis"] != HotelDiagnosis.UNKNOWN.value else None),
                    # Причина словами и что делать — иначе находка требует знания того,
                    # как устроен разбор, чтобы понять ярлык.
                    "cause": (HotelDiagnosis(r["diagnosis"]).cause
                              if r["diagnosis"] != HotelDiagnosis.UNKNOWN.value
                              else GapKind(r["kind"]).hint),
                    "action": (HotelDiagnosis(r["diagnosis"]).action
                               if r["diagnosis"] != HotelDiagnosis.UNKNOWN.value else None),
                    # Цены сторон как есть. Проценты без них нечитаемы: «+34.4%» не
                    # говорит ни сколько стоит тур, ни где он дороже.
                    "reference_price": (float(r["reference_price"])
                                        if r["reference_price"] is not None else None),
                    "checked_price": (float(r["checked_price"])
                                      if r["checked_price"] is not None else None),
                    "currency": r["currency"],
                    "note": r["note"],
                    "reviewed": bool(r["reviewed"]),
                    "reviewed_at": r["reviewed_at"],
                    # Возраст находки: сколько прогонов держится и когда увидели впервые.
                    "times_seen": r["times_seen"] or 1,
                    "first_seen": r["first_seen"],
                }
                for r in rows
            ],
        }

    @app.post("/api/findings/{gap_id}/review")
    async def review(gap_id: int, reviewed: bool = True) -> dict:
        """Отметить разобранной ПРОБЛЕМУ, к которой относится строка.

        Отметка ставится на проблему целиком (оператор + направление + отель + класс), а
        не на строку: строк у одной проблемы десятки, они разбросаны по прогонам, и часть
        из них в отчёт даже не загружена. Заодно отметка переживает перепроверку —
        иначе следующий обход завёл бы свежие строки, и разобранное всплыло бы заново.
        """
        with storage.session(db_path) as conn:
            key = storage.problem_key_of(conn, gap_id)
            if key is None:
                raise HTTPException(404, f"находка #{gap_id} не найдена")
            storage.set_problem_reviewed(conn, key, reviewed)
        return {"id": gap_id, "problem": key, "reviewed": reviewed}

    # Собранный дашборд. Раздаём под /app, корень редиректит туда — чтобы у пользователя
    # был один адрес, который «просто открывается».
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.is_dir():
        @app.middleware("http")
        async def no_cache_index(request, call_next):
            """Запретить кеширование index.html.

            Имена ассетов содержат хеш содержимого, поэтому их браузеру кешировать можно
            и нужно. А вот index.html ссылается на эти имена — закешированный, он после
            пересборки фронта продолжает тянуть старые чанки, и правки «не появляются»,
            пока не нажмёшь Ctrl+F5. Ловушка неочевидная, поэтому закрываем заголовком.
            """
            response = await call_next(request)
            path = request.url.path
            if path in ("/app", "/app/") or path.endswith("/index.html"):
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

        app.mount("/app", StaticFiles(directory=str(dist), html=True), name="dashboard")

        @app.get("/")
        async def root() -> RedirectResponse:
            return RedirectResponse("/app/")
    else:
        @app.get("/")
        async def root_missing() -> JSONResponse:
            return JSONResponse(
                {"error": "Фронтенд не собран. Выполните: cd frontend && npm install && npm run build"},
                status_code=503,
            )

    return app
