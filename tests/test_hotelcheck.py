"""Верификация «отеля нет на Слетать» прижатой пробой шлюза."""

from datetime import date
from decimal import Decimal

from pegasgap.hotelcheck import verify_hotel_gaps
from pegasgap.models import GapKind, HotelDiagnosis, HotelGap, ScanResult, SearchParams

PARAMS = SearchParams(departure_city="Москва", destination_country="Турция",
                      date_from=date(2026, 9, 19), date_to=date(2026, 9, 26),
                      nights_min=7, nights_max=7, adults=2)


def gap(catalog_id, diagnosis=HotelDiagnosis.LINKED_NO_OFFER, name="Kemer Star"):
    return HotelGap(kind=GapKind.HOTEL, hotel_name=name, catalog_id=catalog_id,
                    diagnosis=diagnosis, reference_price=Decimal("100000"))


def scan_of(gaps):
    return ScanResult(params=PARAMS, operator="Pegas Touristik", gaps=list(gaps))


def probe_returning(found):
    async def probe(params, ids):
        return found
    return probe


async def test_found_tours_remove_the_phantom():
    scan = scan_of([gap(116596, name="Almera"), gap(16485, name="Ontur")])
    await verify_hotel_gaps(scan, probe_returning({116596}))
    assert [g.hotel_name for g in scan.gaps] == ["Ontur"]
    assert any("наша выдача была недочитана" in n for n in scan.notes)


async def test_empty_probe_confirms():
    scan = scan_of([gap(16485)])
    await verify_hotel_gaps(scan, probe_returning(set()))
    assert len(scan.gaps) == 1
    assert any("подтверждён" in n or "верифицированы" in n for n in scan.notes)


async def test_uncertain_resolution_is_never_probed():
    """Туры ЧУЖОГО отеля не должны снимать находку по шаткому кандидату."""
    calls = []

    async def probe(params, ids):
        calls.append(ids)
        return set(ids)

    scan = scan_of([gap(123568, diagnosis=HotelDiagnosis.UNCERTAIN)])
    await verify_hotel_gaps(scan, probe)
    assert len(scan.gaps) == 1 and not calls


async def test_failed_probe_keeps_and_says_so():
    scan = scan_of([gap(16485)])
    await verify_hotel_gaps(scan, probe_returning(None))
    assert len(scan.gaps) == 1
    assert any("не верифицированы" in n for n in scan.notes)
