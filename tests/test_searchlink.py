"""Тесты ссылки на поиск Слетать."""

from datetime import date

from pegasgap.models import SearchParams
from pegasgap.searchlink import search_url, slugify


def params(**over) -> SearchParams:
    base = dict(departure_city="Москва", destination_country="Египет",
                date_from=date(2026, 10, 5), date_to=date(2026, 10, 12),
                nights_min=7, nights_max=7, adults=2)
    return SearchParams(**{**base, **over})


def test_link_matches_the_live_format():
    assert search_url(params()) == (
        "https://sletat.ru/search/from-moscow-to-egypt-for-october-nights-7..7"
        "-adults-2-kids-zero"
        "?datefrom=05%2F10%2F2026&dateto=12%2F10%2F2026&currency=RUB&ticketsincluded=true")


def test_compound_names_use_underscore_not_hyphen():
    """Дефис у площадки разделяет поля пути: на `saint-petersburg` город молча теряется,
    и ссылка ведёт в поиск без города — то есть врёт правдоподобно."""
    assert slugify("Saint-Petersburg") == "saint_petersburg"
    assert slugify("Sri Lanka") == "sri_lanka"
    assert "from-saint_petersburg-to-sri_lanka-" in search_url(
        params(departure_city="Санкт-Петербург", destination_country="Шри-Ланка"))


def test_hotels_mode_is_a_search_without_tickets():
    assert "ticketsincluded=false" in search_url(params(search_mode="hotels"))


def test_children_ages_go_into_the_path():
    assert "-kids-7.12" in search_url(params(children_ages=[7, 12]))
    assert "-kids-zero" in search_url(params())


def test_month_comes_from_the_departure_window():
    assert "-for-december-" in search_url(
        params(date_from=date(2026, 12, 16), date_to=date(2026, 12, 23)))


def test_unknown_place_gives_no_link_rather_than_a_wrong_one():
    """Ссылка на соседний город выглядит рабочей и уводит разбор в сторону."""
    assert search_url(params(departure_city="Урюпинск")) is None
    assert search_url(params(destination_country="Антарктида")) is None
