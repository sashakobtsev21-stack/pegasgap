"""Тесты хранилища и истории находок."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from pegasgap import storage
from pegasgap.models import (
    PEGAS,
    GapKind,
    HotelGap,
    OperatorStatus,
    ScanResult,
    SearchParams,
)

PARAMS = SearchParams(
    departure_city="Москва", destination_country="Турция",
    date_from=date(2026, 9, 10), date_to=date(2026, 9, 17),
    nights_min=7, nights_max=7, adults=2,
)


def gap(name: str, kind: GapKind = GapKind.HOTEL) -> HotelGap:
    return HotelGap(kind=kind, hotel_name=name, reference_price=Decimal("100000"))


def scan(gaps: list[HotelGap], *, trustworthy: bool = True, when: datetime | None = None) -> ScanResult:
    return ScanResult(
        params=PARAMS, operator=PEGAS,
        run_at=when or datetime.now(),
        reference_status=OperatorStatus.PRICED,
        checked_status=OperatorStatus.PRICED,
        gaps=gaps,
        problems=[] if trustworthy else ["фильтр по оператору не применился"],
    )


@pytest.fixture
def conn(tmp_path):
    with storage.session(tmp_path / "test.db") as c:
        yield c


def test_save_and_read_back(conn):
    run_id = storage.save_scan(conn, scan([gap("A Palace"), gap("B Grand")]))
    rows = storage.gaps_of_run(conn, run_id)
    assert {r["hotel_name"] for r in rows} == {"A Palace", "B Grand"}


def test_new_gaps_are_new_only_once(conn):
    first = scan([gap("A Palace")])
    assert len(storage.new_gaps(conn, first)) == 1
    storage.save_scan(conn, first)

    second = scan([gap("A Palace"), gap("B Grand")])
    fresh = storage.new_gaps(conn, second)
    assert [g.hotel_name for g in fresh] == ["B Grand"]


def test_untrustworthy_run_does_not_pollute_history(conn):
    """Иначе один прогон со сломанным фильтром засеет историю выдуманными находками,
    и «висит давно» перестанет что-либо значить."""
    storage.save_scan(conn, scan([gap("A Palace")], trustworthy=False))
    # Находка сохранена как факт прогона...
    assert conn.execute("SELECT COUNT(*) c FROM gaps").fetchone()["c"] == 1
    # ...но в историю не попала, поэтому в следующем достоверном прогоне она НОВАЯ.
    assert len(storage.new_gaps(conn, scan([gap("A Palace")]))) == 1


def test_times_seen_grows_across_runs(conn):
    for _ in range(3):
        storage.save_scan(conn, scan([gap("A Palace")]))
    row = storage.gap_age(conn, PARAMS.scenario_key(), gap("A Palace"))
    assert row is not None
    _, times = row
    assert times == 3


def test_standing_gaps_filters_by_repeats(conn):
    for _ in range(3):
        storage.save_scan(conn, scan([gap("Old Palace")]))
    storage.save_scan(conn, scan([gap("Fresh Grand")]))
    standing = storage.standing_gaps(conn, min_times=3)
    assert len(standing) == 1
    assert "old palace" in standing[0]["gap_key"]


def test_summary_counts_only_trustworthy(conn):
    storage.save_scan(conn, scan([gap("A Palace")]))
    storage.save_scan(conn, scan([gap("B Grand")], trustworthy=False))
    counts = storage.summary_since(conn, datetime.now() - timedelta(hours=1))
    assert counts[GapKind.HOTEL.value] == 1


def test_gap_key_is_stable_across_objects():
    """Ключ должен переживать пересоздание объекта, иначе история не склеится."""
    assert gap("A Palace").key() == HotelGap(
        kind=GapKind.HOTEL, hotel_name="  a palace  ").key()


def test_different_kinds_are_different_findings():
    """Один отель может дать и пропуск, и расхождение цены — это разные случаи."""
    assert gap("A", GapKind.HOTEL).key() != gap("A", GapKind.PRICE).key()


def test_report_hides_reverse_gaps_from_older_runs(conn):
    """Прогоны, сделанные до решения об обратных пропусках, лежат в базе как есть —
    587 строк из 703. Отчёт их не показывает и не считает, но данные остаются:
    фильтр стоит на чтении, а не удалением строк."""
    run_id = storage.save_scan(conn, scan([
        gap("A Palace"),
        gap("B Resort", GapKind.REVERSE),
    ]))
    since = datetime.now() - timedelta(days=1)

    assert [r["hotel_name"] for r in storage.findings(conn, since)] == ["A Palace"]
    assert storage.findings_summary(conn, since)["total"] == 1
    assert len(storage.gaps_of_run(conn, run_id)) == 2


def test_review_survives_a_recheck(conn):
    """Отметка держится на ПРОБЛЕМЕ, а не на строке. Строк у одной проблемы десятки —
    «LIFE RESORTS CORAL HILLS» встретился в 53 прогонах, — и флаг на строке означал бы,
    что разобранное всплывает заново после каждой перепроверки направления."""
    first = scan([gap("A Palace")])
    storage.save_scan(conn, first)
    since = datetime.now() - timedelta(days=1)

    row = storage.findings(conn, since)[0]
    storage.set_problem_reviewed(conn, storage.problem_key_of(conn, row["id"]))
    assert storage.findings_summary(conn, since)["unique_reviewed"] == 1

    # Направление перепроверили — появилась новая строка той же проблемы.
    storage.save_scan(conn, scan([gap("A Palace")]))
    summary = storage.findings_summary(conn, since)
    assert summary["unique"] == 1
    assert summary["unique_reviewed"] == 1        # осталась разобранной
    assert all(r["reviewed"] for r in storage.findings(conn, since))


def test_unreviewing_a_problem_brings_it_back(conn):
    storage.save_scan(conn, scan([gap("A Palace")]))
    since = datetime.now() - timedelta(days=1)
    key = storage.problem_key_of(conn, storage.findings(conn, since)[0]["id"])
    storage.set_problem_reviewed(conn, key)
    storage.set_problem_reviewed(conn, key, reviewed=False)
    assert storage.findings_summary(conn, since)["unique_reviewed"] == 0
    assert len(storage.findings(conn, since, only_open=True)) == 1
