"""Поиск кандидатов в словарь по почерку парных находок."""

from datetime import date

import pytest

from pegasgap import storage
from pegasgap.candidates import find_candidates
from pegasgap.models import GapKind, HotelGap, ScanResult, SearchParams


@pytest.fixture
def conn(tmp_path):
    with storage.session(tmp_path / "c.db") as c:
        yield c


def scan(gaps):
    return ScanResult(
        params=SearchParams(departure_city="Москва", destination_country="Турция",
                            date_from=date(2026, 9, 3), date_to=date(2026, 9, 10),
                            nights_min=7, nights_max=7, adults=2),
        operator="Pegas Touristik", gaps=gaps)


def test_paired_findings_become_candidates(conn):
    """Живой почерк: KAFTANS HOTEL BY RRH&R (нет у нас) + Kaftans City Hotel (нет у них)
    на одном направлении — один отель под разными именами."""
    storage.save_scan(conn, scan([
        HotelGap(kind=GapKind.HOTEL, hotel_name="KAFTANS HOTEL BY RRH&R", stars=3),
        HotelGap(kind=GapKind.REVERSE, hotel_name="Kaftans City Hotel", stars=3),
        HotelGap(kind=GapKind.REVERSE, hotel_name="Совсем другой отель", stars=5),
    ]))
    found = find_candidates(conn)
    assert len(found) == 1
    assert found[0].ours == "Kaftans City Hotel"
    assert found[0].stars_agree


def test_place_only_overlap_is_not_a_candidate(conn):
    storage.save_scan(conn, scan([
        HotelGap(kind=GapKind.HOTEL, hotel_name="BODRUM BEACH RESORT"),
        HotelGap(kind=GapKind.REVERSE, hotel_name="Prive Hotel Bodrum"),
    ]))
    assert find_candidates(conn) == []


def test_same_chain_different_city_is_penalized(conn):
    """«AKRA ANTALYA» и «Akra Kemer» — разные объекты одной сети: город у сетевых
    отелей различитель, и такая пара не должна выглядеть стопроцентной."""
    storage.save_scan(conn, scan([
        HotelGap(kind=GapKind.HOTEL, hotel_name="AKRA ANTALYA", stars=5),
        HotelGap(kind=GapKind.REVERSE, hotel_name="Akra Kemer", stars=5),
    ]))
    assert find_candidates(conn) == []
