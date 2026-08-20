"""Тесты очереди кейсов и непрерывного воркера.

Очередь — единственное, что отличает круглосуточную работу от повторного прогона одного
и того же списка. Поэтому здесь проверяется в первую очередь её память: что уже
проверено, что нет, и что посев конфига эту память не затирает.
"""

import asyncio
from datetime import date, datetime, timedelta

import pytest

from pegasgap import queue as q
from pegasgap import storage
from pegasgap.models import PEGAS, GapKind, HotelGap, OperatorStatus, ScanResult
from pegasgap.scenarios import Matrix, Pax, Window


@pytest.fixture
def conn(tmp_path):
    with storage.session(tmp_path / "queue.db") as c:
        yield c


def add(conn, country="Турция", city="Москва", mode="tours", priority=0,
        adults=2, children=None, day=1):
    return q.add_case(
        conn, departure_city=city, country=country, search_mode=mode,
        date_from=date(2026, 9, day), date_to=date(2026, 9, day + 7),
        nights=7, adults=adults, children_ages=children or [], priority=priority)


# --------------------------------- состав кейса ---------------------------------


def test_pax_is_part_of_the_case():
    """«Двое взрослых» и «трое взрослых с ребёнком» — разные поиски с разной выдачей,
    и находка по одному ничего не говорит о другом."""
    a = q.case_key("Москва", "Египет", "tours", date(2026, 10, 20), date(2026, 10, 25),
                   5, 2, [])
    b = q.case_key("Москва", "Египет", "tours", date(2026, 10, 20), date(2026, 10, 25),
                   5, 3, [12])
    assert a != b


def test_case_key_ignores_child_order():
    """Порядок возрастов детей не должен плодить дубли одного и того же кейса."""
    args = ("Москва", "Египет", "tours", date(2026, 10, 20), date(2026, 10, 25), 5, 2)
    assert q.case_key(*args, [12, 5]) == q.case_key(*args, [5, 12])


def test_case_title_reads_like_the_report_line():
    conn_case = q.Case(
        case_key="t|k",
        id=1, operator="Coral Travel",
        departure_city="Москва", country="Египет", search_mode="tours",
        date_from=date(2026, 10, 20), date_to=date(2026, 10, 25), nights=5,
        adults=3, children_ages=[12], priority=0, last_checked=None,
        checks=0, gaps_found=0)
    title = conn_case.title
    assert "Coral Travel" in title        # оператор — измерение кейса, он в заголовке
    assert "Москва → Египет" in title
    assert "20.10.2026" in title
    assert "3 взр." in title and "12" in title


# --------------------------------- порядок обхода ---------------------------------


def test_never_checked_go_first(conn):
    old = add(conn, country="Турция", priority=100, day=1)
    add(conn, country="Египет", priority=1, day=2)
    q.mark_checked(conn, old, run_id=1, gaps=0)
    # Турция приоритетнее, но уже проверена — вперёд выходит непроверенный Египет.
    assert q.next_case(conn).country == "Египет"


def test_priority_decides_among_unchecked(conn):
    add(conn, country="Египет", priority=1)
    add(conn, country="ОАЭ", priority=50)
    assert q.next_case(conn).country == "ОАЭ"


def test_checked_case_moves_to_the_back(conn):
    first = add(conn, country="ОАЭ", priority=10)
    add(conn, country="Египет", priority=5)
    q.mark_checked(conn, first, run_id=1, gaps=0)
    assert q.next_case(conn).country == "Египет"


def test_recently_checked_are_skipped(conn):
    """Гонять список по кругу каждый час бессмысленно: цены столько не меняются,
    а квота на поиски конечна."""
    case_id = add(conn)
    q.mark_checked(conn, case_id, run_id=1, gaps=0)
    assert q.next_case(conn, min_age_hours=12) is None
    assert q.next_case(conn, min_age_hours=0) is not None


def test_stale_case_returns_to_the_queue(conn):
    case_id = add(conn)
    q.mark_checked(conn, case_id, run_id=1, gaps=0,
                   when=datetime.now() - timedelta(days=2))
    assert q.next_case(conn, min_age_hours=12) is not None


# --------------------------------- посев ---------------------------------


def test_seed_does_not_duplicate_or_forget(conn):
    """Повторный посев конфига не должен ни плодить дубли, ни обнулять память очереди —
    иначе воркер вечно перепроверял бы одно и то же."""
    matrix = Matrix(routes=[("Москва", "ОАЭ")], modes=["tours"],
                    windows=[Window(30)], pax=[Pax()])
    assert q.seed_from_matrix(conn, matrix, date(2026, 9, 1))[0] == 1
    case_id = q.next_case(conn).id
    q.mark_checked(conn, case_id, run_id=7, gaps=3)

    q.seed_from_matrix(conn, matrix, date(2026, 9, 1))
    assert q.stats(conn)["total"] == 1
    kept = q.list_cases(conn)[0]
    assert kept.checks == 1 and kept.gaps_found == 3


def test_seed_expands_every_dimension(conn):
    matrix = Matrix(
        routes=[("Москва", "ОАЭ"), ("Москва", "Египет")],
        modes=["tours", "hotels"], windows=[Window(30), Window(60)],
        pax=[Pax(adults=2), Pax(adults=3, children=[12])])
    # 2 маршрута × 2 режима × 2 окна × 2 состава
    assert q.seed_from_matrix(conn, matrix, date(2026, 9, 1))[0] == 16


def test_seed_order_becomes_priority(conn):
    """`pegasgap top` выписывает маршруты по убыванию объёма, и порядок в конфиге —
    самый дешёвый способ хранить приоритет: он виден в диффе."""
    matrix = Matrix(routes=[("Москва", "ОАЭ"), ("Москва", "Абхазия")],
                    modes=["tours"], windows=[Window(30)], pax=[Pax()])
    q.seed_from_matrix(conn, matrix, date(2026, 9, 1))
    assert q.next_case(conn).country == "ОАЭ"


# --------------------------------- счётчики ---------------------------------


def test_stats_count_progress(conn):
    a = add(conn, country="ОАЭ")
    add(conn, country="Египет")
    q.mark_checked(conn, a, run_id=1, gaps=2)
    s = q.stats(conn)
    assert s == {"total": 2, "checked": 1, "pending": 1, "runs": 1, "gaps": 2}


# --------------------------------- воркер ---------------------------------


def make_scan(gaps: int) -> ScanResult:
    from datetime import date as d

    from pegasgap.models import SearchParams
    params = SearchParams(departure_city="Москва", destination_country="Турция",
                          date_from=d(2026, 9, 1), date_to=d(2026, 9, 8),
                          nights_min=7, nights_max=7, adults=2)
    return ScanResult(
        params=params, operator=PEGAS,
        reference_status=OperatorStatus.PRICED, checked_status=OperatorStatus.PRICED,
        gaps=[HotelGap(kind=GapKind.HOTEL, hotel_name=f"H{i}") for i in range(gaps)])


async def test_worker_walks_the_queue_and_marks_progress(tmp_path):
    from pegasgap.worker import Worker
    db = tmp_path / "w.db"
    with storage.session(db) as c:
        for i, country in enumerate(["ОАЭ", "Египет"]):
            q.add_case(c, departure_city="Москва", country=country, search_mode="tours",
                       date_from=date(2026, 9, 1), date_to=date(2026, 9, 8), nights=7,
                       priority=10 - i)

    seen: list[str] = []

    async def fake_scan(case):
        seen.append(case.country)
        return 1, make_scan(gaps=2)

    worker = Worker(db_path=db, scan_runner=fake_scan, pause_s=0.01, idle_s=0.01)
    worker.start()
    for _ in range(60):                      # ждём, пока обойдёт оба кейса
        if len(seen) >= 2:
            break
        await _tick()
    await worker.stop()

    assert seen[:2] == ["ОАЭ", "Египет"]     # порядок по приоритету
    with storage.session(db) as c:
        assert q.stats(c)["checked"] == 2
        assert q.stats(c)["gaps"] == 4


async def test_failing_case_does_not_stop_the_worker(tmp_path):
    """Сбой одного направления не должен ронять круглосуточный процесс — и не должен
    зацикливать его на себе."""
    from pegasgap.worker import Worker
    db = tmp_path / "w.db"
    with storage.session(db) as c:
        q.add_case(c, departure_city="Москва", country="ОАЭ", search_mode="tours",
                   date_from=date(2026, 9, 1), date_to=date(2026, 9, 8), nights=7)

    calls = 0

    async def boom(case):
        nonlocal calls
        calls += 1
        raise RuntimeError("площадка отвалилась")

    worker = Worker(db_path=db, scan_runner=boom, pause_s=0.01, idle_s=0.01)
    worker.start()
    for _ in range(40):
        if calls >= 1:
            break
        await _tick()
    await worker.stop()

    assert calls == 1                        # кейс не берётся повторно
    assert worker.state.errors == 1
    with storage.session(db) as c:
        assert q.stats(c)["checked"] == 1    # помечен, несмотря на сбой


async def _tick() -> None:
    import asyncio
    await asyncio.sleep(0.05)


async def test_finding_event_does_not_collide_with_event_kind(tmp_path):
    """Регресс: поле находки называлось `kind`, как и тип события в шине, и публикация
    падала с «got multiple values for argument». Ошибка глушилась внутри задачи воркера,
    поэтому снаружи выглядела как «обошёл только один кейс»."""
    from pegasgap.events import bus
    from pegasgap.worker import Worker
    db = tmp_path / "w.db"
    with storage.session(db) as c:
        q.add_case(c, departure_city="Москва", country="ОАЭ", search_mode="tours",
                   date_from=date(2026, 9, 1), date_to=date(2026, 9, 8), nights=7)

    received = bus.subscribe()

    async def fake_scan(case):
        return 1, make_scan(gaps=1)

    worker = Worker(db_path=db, scan_runner=fake_scan, pause_s=0.01, idle_s=0.01)
    worker.start()
    for _ in range(40):
        if worker.state.checked >= 1:
            break
        await _tick()
    await worker.stop()
    bus.unsubscribe(received)

    events = []
    while not received.empty():
        events.append(received.get_nowait())
    findings = [e for e in events if e["kind"] == "finding"]
    assert findings, "находка не доехала до шины"
    assert findings[0]["gap_kind"] == GapKind.HOTEL.value
    assert worker.state.errors == 0


async def test_worker_stops_when_the_platform_is_down(tmp_path):
    """Если площадка легла, проверка не состоится ни на одном кейсе. Продолжать нельзя:
    воркер пройдёт всю очередь, пометит каждый кейс проверенным и не проверит ничего —
    потерянный обход хуже остановки, потому что выглядит как выполненный."""
    from pegasgap.worker import MAX_CONSECUTIVE_FAILURES, Worker
    db = tmp_path / "w.db"
    with storage.session(db) as c:
        for i in range(MAX_CONSECUTIVE_FAILURES + 5):
            q.add_case(c, departure_city="Москва", country=f"Страна{i}",
                       search_mode="tours", date_from=date(2026, 9, 1),
                       date_to=date(2026, 9, 8), nights=7)

    calls = 0

    async def down(case):
        nonlocal calls
        calls += 1
        scan = make_scan(gaps=0)
        scan.problems = ["эталон: поиск не удался — HTTP 401"]
        return 1, scan

    # concurrency=1: контракт «ровно после N сбоев» проверяем в последовательном
    # режиме; параллельный успевает взять до concurrency кейсов — на него свой тест.
    worker = Worker(db_path=db, scan_runner=down, pause_s=0.01, idle_s=0.01,
                    concurrency=1)
    worker.start()
    for _ in range(80):
        if not worker.state.running:
            break
        await _tick()
    await worker.stop()

    assert calls == MAX_CONSECUTIVE_FAILURES     # остановился, а не съел очередь
    assert worker.state.stopped_reason
    assert "легла" in worker.state.stopped_reason
    with storage.session(db) as c:
        assert q.stats(c)["pending"] >= 5        # непроверенные остались непроверенными


def test_failure_threshold_scales_with_concurrency(monkeypatch):
    """При параллельности серия сбоев набирается одной пачкой: три тяжёлых кейса одного
    направления (живой случай — «Москва—Россия, отели») падают разом, и порог 3 остановил
    обход при живых площадках. Порог обязан расти вместе с параллельностью."""
    import importlib

    import pegasgap.worker as w

    monkeypatch.delenv("PEGASGAP_MAX_FAILURES", raising=False)
    monkeypatch.setenv("PEGASGAP_WORKER_CONCURRENCY", "6")
    assert importlib.reload(w).MAX_CONSECUTIVE_FAILURES == 8   # пачка + 2
    monkeypatch.setenv("PEGASGAP_WORKER_CONCURRENCY", "1")
    assert importlib.reload(w).MAX_CONSECUTIVE_FAILURES == 3   # последовательный минимум
    monkeypatch.setenv("PEGASGAP_MAX_FAILURES", "5")
    assert importlib.reload(w).MAX_CONSECUTIVE_FAILURES == 5   # явная настройка сильнее
    # Вернуть модуль в честное состояние для остальных тестов: monkeypatch откатит
    # окружение, но перезагрузить модуль за нас не сможет.
    monkeypatch.delenv("PEGASGAP_MAX_FAILURES", raising=False)
    monkeypatch.delenv("PEGASGAP_WORKER_CONCURRENCY", raising=False)
    importlib.reload(w)


async def test_untrustworthy_data_does_not_stop_the_worker(tmp_path):
    """Сомнительные данные — свойство направления, а не площадки: следующее может быть
    в порядке, и останавливаться из-за этого нельзя."""
    from pegasgap.worker import Worker
    db = tmp_path / "w.db"
    with storage.session(db) as c:
        for i in range(4):
            q.add_case(c, departure_city="Москва", country=f"Страна{i}",
                       search_mode="tours", date_from=date(2026, 9, 1),
                       date_to=date(2026, 9, 8), nights=7)

    calls = 0

    async def shaky(case):
        nonlocal calls
        calls += 1
        scan = make_scan(gaps=0)
        scan.problems = ["цены сторон систематически расходятся на -48%"]
        return 1, scan

    worker = Worker(db_path=db, scan_runner=shaky, pause_s=0.01, idle_s=0.01)
    worker.start()
    for _ in range(80):
        if calls >= 4:
            break
        await _tick()
    await worker.stop()

    assert calls == 4                            # прошёл всё, не остановился
    assert worker.state.stopped_reason is None


def test_same_search_by_different_operators_are_different_cases(conn):
    """Иначе три оператора схлопнулись бы в один кейс и проверялся бы только первый."""
    common = dict(departure_city="Москва", country="Турция", search_mode="tours",
                  date_from=date(2026, 9, 1), date_to=date(2026, 9, 8), nights=7)
    a = q.add_case(conn, operator="Pegas Touristik", **common)
    b = q.add_case(conn, operator="Coral Travel", **common)
    c = q.add_case(conn, operator="Sunmar", **common)
    assert len({a, b, c}) == 3
    assert {case.operator for case in q.list_cases(conn)} == {
        "Pegas Touristik", "Coral Travel", "Sunmar"}


def test_pegas_case_key_is_unchanged_so_history_survives():
    """Оператор идёт в ключ только когда он не Pegas: прежние кейсы все были по нему, и
    менять им ключ значило бы обнулить накопленную историю проверок."""
    args = ("Москва", "Турция", "tours", date(2026, 9, 1), date(2026, 9, 8), 7, 2, [])
    assert q.case_key(*args) == q.case_key(*args, "Pegas Touristik")
    assert q.case_key(*args, "Coral Travel") != q.case_key(*args)


def test_case_params_carry_the_case_operator_not_a_default():
    """Воркер навязывал свой оператор каждому кейсу, и кейсы Coral и Sunmar отмечались
    пройденными, хотя искался по ним Pegas. Оператор — измерение кейса, он и решает."""
    case = q.Case(
        case_key="t|k",
        id=1, operator="Sunmar", departure_city="Москва", country="Турция",
        search_mode="tours", date_from=date(2026, 9, 1), date_to=date(2026, 9, 8),
        nights=7, adults=2, children_ages=[], priority=0, last_checked=None,
        checks=0, gaps_found=0)
    assert case.to_params().operators == ["Sunmar"]


def test_worker_does_not_override_the_case_operator():
    import inspect

    from pegasgap.worker import Worker
    src = inspect.getsource(Worker._scan)
    assert "case.to_params()" in src
    assert "self.operator" not in src


# --- Ключ переживает смену дня ------------------------------------------------------

def test_key_is_the_same_scenario_on_a_different_day():
    """Окно задано смещением, и на календарных датах тот же самый кейс назавтра получал
    другой ключ."""
    monday, tuesday = date(2026, 8, 19), date(2026, 8, 20)
    same = [
        q.case_key("Москва", "Турция", "tours", day + timedelta(days=14),
                   day + timedelta(days=21), 7, 2, [], PEGAS, day)
        for day in (monday, tuesday)
    ]
    assert same[0] == same[1]


def test_different_windows_are_still_different_cases():
    day = date(2026, 8, 19)
    near = q.case_key("Москва", "Турция", "tours", day + timedelta(days=14),
                      day + timedelta(days=21), 7, 2, [], PEGAS, day)
    far = q.case_key("Москва", "Турция", "tours", day + timedelta(days=60),
                     day + timedelta(days=67), 7, 2, [], PEGAS, day)
    longer = q.case_key("Москва", "Турция", "tours", day + timedelta(days=14),
                        day + timedelta(days=28), 7, 2, [], PEGAS, day)
    assert len({near, far, longer}) == 3


def test_reseeding_the_next_day_retires_nothing(conn):
    """Главный симптом прежнего ключа: пересборка гасила ВСЮ очередь и заводила такую же
    новую — «3960 актуальных, 3960 отключено», а давность проверки обнулялась."""
    matrix = Matrix(routes=[("Москва", "ОАЭ")], modes=["tours"],
                    windows=[Window(30)], pax=[Pax()])
    q.seed_from_matrix(conn, matrix, date(2026, 9, 1))
    q.mark_checked(conn, q.next_case(conn).id, run_id=7, gaps=3)

    seeded, retired = q.seed_from_matrix(conn, matrix, date(2026, 9, 2))
    assert (seeded, retired) == (1, 0)
    assert q.stats(conn)["total"] == 1
    assert q.list_cases(conn)[0].checks == 1


def test_case_carries_its_key_for_history(conn):
    """Воркер прокидывает ключ кейса в историю возраста; на живом обходе отсутствие
    этого поля уронило три кейса подряд и остановило круг."""
    matrix = Matrix(routes=[("Москва", "ОАЭ")], modes=["tours"],
                    windows=[Window(30)], pax=[Pax()])
    q.seed_from_matrix(conn, matrix, date(2026, 9, 1))
    case = q.next_case(conn)
    assert case.case_key.startswith("tours|Москва|ОАЭ|+30d..")


async def test_parallel_worker_covers_the_queue_without_duplicates(tmp_path):
    """Параллельность — главный множитель скорости круга (цель владельца: до суток).
    Каждый кейс должен быть проверен ровно один раз: взятые в работу исключаются из
    выдачи очереди до завершения."""
    from pegasgap.worker import Worker
    db = tmp_path / "wp.db"
    with storage.session(db) as c:
        for i in range(9):
            q.add_case(c, departure_city="Москва", country=f"Страна{i}",
                       search_mode="tours", date_from=date(2026, 9, 1),
                       date_to=date(2026, 9, 8), nights=7)

    seen: list[int] = []

    async def ok(case):
        seen.append(case.id)
        await asyncio.sleep(0.02)
        return 1, make_scan(gaps=1)

    worker = Worker(db_path=db, scan_runner=ok, pause_s=0.0, idle_s=0.05,
                    concurrency=4)
    worker.start()
    for _ in range(200):
        with storage.session(db) as c:
            if q.stats(c)["pending"] == 0:
                break
        await _tick()
    await worker.stop()

    assert sorted(seen) == sorted(set(seen))          # без дублей
    assert len(seen) == 9                             # вся очередь пройдена
    assert worker.state.checked == 9
