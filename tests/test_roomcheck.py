"""Сверка номеров ценовых находок с карточкой тура витрины.

Логика решает, останется ли находка в отчёте, поэтому проверяется офлайн — сеть
подменяется, правила нет.
"""

from datetime import date
from decimal import Decimal

import pytest

from pegasgap.models import (
    DayOffer,
    GapKind,
    HotelGap,
    HotelOffer,
    ProviderResult,
    ScanResult,
    SearchParams,
)
from pegasgap.roomcheck import pin_rooms

PARAMS = SearchParams(departure_city="Москва", destination_country="Турция",
                      date_from=date(2026, 9, 3), date_to=date(2026, 9, 10),
                      nights_min=7, nights_max=7, adults=2)


CHECKIN = date(2026, 9, 5)


def price_gap(room: str | None, tour_id: str | None = "t-1") -> HotelGap:
    return HotelGap(kind=GapKind.PRICE, hotel_name="Britannia",
                    matched_name="Britannia", checked_checkin=CHECKIN,
                    checked_meal="AI",
                    reference_price=Decimal("100000"), checked_price=Decimal("115000"),
                    checked_room=room, reference_tour_id=tour_id)


def scan_with(gaps: list[HotelGap],
              inventory: list[DayOffer] | None = None) -> ScanResult:
    """Прогон с нашей выдачей: `inventory` — предложения отеля на дату находки."""
    ours = HotelOffer(provider="sletat", hotel_name="Britannia",
                      price=Decimal("115000"),
                      day_offers={CHECKIN: inventory} if inventory else {})
    checked = ProviderResult(provider="sletat", success=True, duration_seconds=1.0,
                             hotel_offers=[ours])
    return ScanResult(params=PARAMS, operator="Pegas Touristik", gaps=gaps,
                      checked=checked)


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
async def test_room_we_do_not_sell_removes_the_finding_with_a_note():
    """Витрина показала номер, которого у нас нет вовсе: сравнивать не с чем, а «нет
    такого номера» — вопрос ассортимента, не цены."""
    scan = scan_with([price_gap("Стандартный номер")])
    await pin_rooms(scan, fetcher("Promo Room"))
    assert scan.gaps == []
    assert any("номера витрины у нас нет" in n for n in scan.notes)


@pytest.mark.asyncio
async def test_different_min_rooms_but_same_room_agrees_settles_the_finding():
    """Живой случай: наш минимум «Jasmine Pool View», у витрины «camelia family
    superior». Это разные номера, и находка «цена расходится» не значила ничего. Но у
    нас ЕСТЬ camelia — и на нём цены сходятся: разница была номером, не площадками."""
    scan = scan_with(
        [price_gap("Jasmine Pool View")],
        inventory=[DayOffer(price=Decimal("103000"), meal="AI",
                            room="Camelia Family Superior")])
    await pin_rooms(scan, fetcher("camelia family superior"))
    assert scan.gaps == []
    assert any("разница была номером" in n for n in scan.notes)


@pytest.mark.asyncio
async def test_same_room_still_apart_becomes_the_finding():
    """Тот же номер нашёлся, а цены всё равно врозь — вот это и есть расхождение
    площадок; находка пересчитывается на одинаковый номер."""
    scan = scan_with(
        [price_gap("Jasmine Pool View")],
        inventory=[DayOffer(price=Decimal("131000"), meal="AI",
                            room="Camelia Family Superior")])
    await pin_rooms(scan, fetcher("camelia family superior"))
    assert len(scan.gaps) == 1
    gap = scan.gaps[0]
    assert gap.checked_room == "Camelia Family Superior"
    assert gap.checked_price == Decimal("131000")
    assert "на одинаковом номере" in gap.note


@pytest.mark.asyncio
async def test_other_meal_or_day_does_not_count_as_the_same_room():
    """Тот же номер, но другое питание — не замена: сравнение держится на всём составе."""
    scan = scan_with(
        [price_gap("Jasmine Pool View")],
        inventory=[DayOffer(price=Decimal("99000"), meal="RO",
                            room="Camelia Family Superior")])
    await pin_rooms(scan, fetcher("camelia family superior"))
    assert scan.gaps == []
    assert any("номера витрины у нас нет" in n for n in scan.notes)


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
