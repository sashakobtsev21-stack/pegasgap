"""Сверка номеров ценовых находок с карточкой тура витрины.

Логика решает, останется ли находка в отчёте, поэтому проверяется офлайн — сеть
подменяется, правила нет.
"""

from datetime import date
from decimal import Decimal

import pytest

from pegasgap.models import GapKind, HotelGap, ScanResult, SearchParams
from pegasgap.roomcheck import pin_rooms

PARAMS = SearchParams(departure_city="Москва", destination_country="Турция",
                      date_from=date(2026, 9, 3), date_to=date(2026, 9, 10),
                      nights_min=7, nights_max=7, adults=2)


def price_gap(room: str | None, tour_id: str | None = "t-1") -> HotelGap:
    return HotelGap(kind=GapKind.PRICE, hotel_name="Britannia",
                    reference_price=Decimal("100000"), checked_price=Decimal("115000"),
                    checked_room=room, reference_tour_id=tour_id)


def scan_with(gaps: list[HotelGap]) -> ScanResult:
    return ScanResult(params=PARAMS, operator="Pegas Touristik", gaps=gaps)


def fetcher(room: str | None):
    async def fetch(tour_id):
        return room
    return fetch


@pytest.mark.asyncio
async def test_matching_rooms_keep_the_finding_and_record_the_name():
    scan = scan_with([price_gap("Стандартный номер")])
    await pin_rooms(scan, fetcher("стандарт 2 местный"))
    assert len(scan.gaps) == 1
    assert scan.gaps[0].reference_room == "стандарт 2 местный"


@pytest.mark.asyncio
async def test_clashing_categories_remove_the_finding_with_a_note():
    """Промо против стандарта — расхождение состава, а не цены площадок."""
    scan = scan_with([price_gap("Стандартный номер")])
    await pin_rooms(scan, fetcher("Promo Room"))
    assert scan.gaps == []
    assert any("номера сторон разных категорий" in n for n in scan.notes)


@pytest.mark.asyncio
async def test_unfetchable_room_keeps_the_finding_unverified():
    """Не удалось узнать — не значит «разные»: находка остаётся, номер пуст, и отчёт
    честно покажет «не сверен»."""
    scan = scan_with([price_gap("Стандартный номер")])
    await pin_rooms(scan, fetcher(None))
    assert len(scan.gaps) == 1
    assert scan.gaps[0].reference_room is None


@pytest.mark.asyncio
async def test_non_price_gaps_are_untouched():
    hotel_gap = HotelGap(kind=GapKind.HOTEL, hotel_name="Lavra")
    scan = scan_with([hotel_gap, price_gap("Стандартный номер")])
    await pin_rooms(scan, fetcher("Promo Room"))
    assert [g.hotel_name for g in scan.gaps] == ["Lavra"]
