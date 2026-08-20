"""Верификация обратных находок прижатой пробой."""

from datetime import date
from decimal import Decimal

from pegasgap.models import (
    GapKind,
    HotelGap,
    ProviderResult,
    ScanResult,
    SearchParams,
)
from pegasgap.reversecheck import verify_reverse

PARAMS = SearchParams(departure_city="Москва", destination_country="Турция",
                      date_from=date(2026, 12, 18), date_to=date(2026, 12, 25),
                      nights_min=7, nights_max=7, adults=2)


def reverse(name: str, ref_id: int | None) -> HotelGap:
    return HotelGap(kind=GapKind.REVERSE, hotel_name=name, reference_hotel_id=ref_id,
                    checked_price=Decimal("100000"))


def scan_of(gaps, truncated: bool = False) -> ScanResult:
    reference = ProviderResult(provider="tourvisor", success=True, duration_seconds=1.0,
                               truncated=truncated)
    return ScanResult(params=PARAMS, operator="Pegas Touristik", gaps=list(gaps),
                      reference=reference)


def probe_returning(found):
    async def probe(params, ids):
        return found
    return probe


async def test_found_tours_remove_the_phantom():
    """Живой замер: 19 из 52 «туров нет» оказались фантомами — листинг был неполон."""
    scan = scan_of([reverse("Sette Serenity", 71169), reverse("Atlantis Royal", 71351)])
    await verify_reverse(scan, probe_returning({71169}))
    assert [g.hotel_name for g in scan.gaps] == ["Atlantis Royal"]
    assert any("листинг витрины был неполон" in n for n in scan.notes)


async def test_empty_probe_confirms_the_findings():
    """Пустое множество — утверждение: туров нет ни у кого. Находки крепнут."""
    scan = scan_of([reverse("Atlantis Royal", 71351)])
    await verify_reverse(scan, probe_returning(set()))
    assert len(scan.gaps) == 1
    assert any("верифицированы прижатой пробой" in n for n in scan.notes)


async def test_failed_probe_removes_nothing_and_says_so():
    """Сбой сети — не повод ни снимать, ни утверждать."""
    scan = scan_of([reverse("Atlantis Royal", 71351)])
    await verify_reverse(scan, probe_returning(None))
    assert len(scan.gaps) == 1
    assert any("не верифицированы" in n for n in scan.notes)


async def test_candidates_without_showcase_id_are_left_alone():
    """Отель не опознан в словаре витрины — пробовать нечего, честность держит диагноз."""
    calls = []

    async def probe(params, ids):
        calls.append(ids)
        return set()

    scan = scan_of([reverse("Гранд Пляж Юг", None)])
    await verify_reverse(scan, probe)
    assert len(scan.gaps) == 1 and not calls


async def test_truncated_listing_keeps_only_probe_confirmed():
    """Решение (в): в режиме «отели» листинг всегда упирается в потолок 50, и сторона
    держится только пробами — подтверждённые остаются, найденные снимаются."""
    scan = scan_of([reverse("Confirmed", 1), reverse("Phantom", 2)], truncated=True)
    await verify_reverse(scan, probe_returning({2}))
    assert [g.hotel_name for g in scan.gaps] == ["Confirmed"]


async def test_truncated_listing_drops_unprovable_candidates():
    """Кандидат без id витрины на неполном листинге недоказуем — проверить нечем."""
    scan = scan_of([reverse("NoId", None), reverse("Confirmed", 1)], truncated=True)
    await verify_reverse(scan, probe_returning(set()))
    assert [g.hotel_name for g in scan.gaps] == ["Confirmed"]
    assert any("проверить нечем" in n for n in scan.notes)


async def test_truncated_listing_with_failed_probe_claims_nothing():
    scan = scan_of([reverse("Anything", 1)], truncated=True)
    await verify_reverse(scan, probe_returning(None))
    assert scan.gaps == []
    assert any("утверждать нечего" in n for n in scan.notes)
