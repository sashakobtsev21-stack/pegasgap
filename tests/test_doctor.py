"""Инварианты базы: doctor ловит то, что не может записать здоровый прогон."""

from datetime import date
from decimal import Decimal

import pytest

from pegasgap import storage
from pegasgap.doctor import run_checks
from pegasgap.models import GapKind, HotelGap, ScanResult, SearchParams


@pytest.fixture
def conn(tmp_path):
    with storage.session(tmp_path / "doctor.db") as c:
        yield c


def scan(gaps):
    return ScanResult(
        params=SearchParams(departure_city="Москва", destination_country="Турция",
                            date_from=date(2026, 9, 3), date_to=date(2026, 9, 10),
                            nights_min=7, nights_max=7, adults=2),
        operator="Pegas Touristik", gaps=gaps)


def test_healthy_db_passes(conn):
    storage.save_scan(conn, scan([
        HotelGap(kind=GapKind.HOTEL, hotel_name="Kemer Star"),
        HotelGap(kind=GapKind.PRICE, hotel_name="Britannia",
                 reference_price=Decimal("100000"), checked_price=Decimal("110000"),
                 reference_checkin=date(2026, 9, 5), checked_checkin=date(2026, 9, 5)),
        HotelGap(kind=GapKind.REVERSE, hotel_name="Grand", catalog_id=42),
    ]))
    assert all(c.ok for c in run_checks(conn)), [c for c in run_checks(conn) if not c.ok]


def test_orphan_gap_is_caught(conn):
    storage.save_scan(conn, scan([HotelGap(kind=GapKind.HOTEL, hotel_name="Kemer Star")]))
    # Штатно сироты невозможны (FK с каскадом) — ломаем нарочно, отключив его: проверка
    # ловит именно то, что не должен уметь записать ни один код.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM runs")
    bad = {c.name for c in run_checks(conn) if not c.ok}
    assert any(c.startswith("Ж1а") for c in bad)


def test_price_on_different_days_is_caught(conn):
    storage.save_scan(conn, scan([
        HotelGap(kind=GapKind.PRICE, hotel_name="Britannia",
                 reference_price=Decimal("100000"), checked_price=Decimal("110000"),
                 reference_checkin=date(2026, 9, 5), checked_checkin=date(2026, 9, 8)),
    ]))
    bad = {c.name for c in run_checks(conn) if not c.ok}
    assert any(c.startswith("Ж2а") for c in bad)


def test_lying_trustworthy_is_caught(conn):
    storage.save_scan(conn, scan([]))
    conn.execute("UPDATE runs SET trustworthy = 0")       # причины при этом пустые
    bad = {c.name for c in run_checks(conn) if not c.ok}
    assert any(c.startswith("Ж6") for c in bad)
