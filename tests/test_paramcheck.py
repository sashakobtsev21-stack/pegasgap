"""Тесты сверки: то ли искали, что просили."""

from datetime import date

from pegasgap.models import SearchParams
from pegasgap.paramcheck import OfferFacts, verify

PARAMS = SearchParams(
    departure_city="Москва", destination_country="Турция",
    date_from=date(2026, 10, 17), date_to=date(2026, 10, 24),
    nights_min=7, nights_max=7, adults=2,
)


def facts(n: int, **over) -> list[OfferFacts]:
    base = dict(checkin=date(2026, 10, 20), nights=7, operator="Pegas Touristik")
    return [OfferFacts(**{**base, **over}) for _ in range(n)]


def test_matching_search_raises_nothing():
    assert verify(PARAMS, facts(10), "Pegas Touristik") == []


def test_wrong_nights_are_caught():
    """Главный страх: в одну площадку ушли одни параметры, в другую другие. Отели при
    этом сойдутся, и отчёт покажет «пропуски», которых нет."""
    problems = verify(PARAMS, facts(10, nights=10), "Pegas Touristik")
    assert len(problems) == 1
    assert "ночей" in problems[0] and "просили 7–7" in problems[0]
    assert "10" in problems[0]


def test_dates_outside_the_window_are_caught():
    problems = verify(PARAMS, facts(10, checkin=date(2026, 12, 1)), "Pegas Touristik")
    assert any("дата заезда" in p for p in problems)


def test_foreign_operator_is_caught():
    problems = verify(PARAMS, facts(10, operator="Coral Travel"), "Pegas Touristik")
    assert any("оператор" in p for p in problems)


def test_a_few_odd_offers_do_not_fail_the_run():
    """Площадки иногда подмешивают соседние даты; падать из-за единичной строки значило
    бы браковать здоровые прогоны."""
    mixed = facts(19) + facts(1, nights=10)
    assert verify(PARAMS, mixed, "Pegas Touristik") == []


def test_majority_wrong_does_fail():
    mixed = facts(5) + facts(15, nights=14)
    assert verify(PARAMS, mixed, "Pegas Touristik") != []


def test_unknown_fields_are_not_judged():
    """Пустая выдача и выдача без подробностей расхождений не дают: молчание честнее
    выдуманной уверенности."""
    assert verify(PARAMS, [], "Pegas Touristik") == []
    assert verify(PARAMS, [OfferFacts()] * 5, "Pegas Touristik") == []


def test_range_of_nights_is_respected():
    wide = PARAMS.model_copy(update={"nights_min": 7, "nights_max": 14})
    assert verify(wide, facts(10, nights=10), "Pegas Touristik") == []
