"""Тесты разбора ответов JSON-шлюза Слетать (чистые функции, без сети)."""

from decimal import Decimal

import pytest

pytest.importorskip("httpx")

from pegasgap.models import PEGAS
from pegasgap.providers.sletat_api import (
    IDX_HOTEL_NAME,
    IDX_OPERATOR_NAME,
    IDX_PRICE,
    IDX_RATING,
    IDX_RESORT,
    IDX_STARS,
    _find_by_name,
    _redact,
    build_hotel_offers,
    parse_price,
    parse_stars,
    split_load_state,
)


def row(hotel: str, price: int, operator: str = PEGAS, stars: int = 5,
        resort: str = "Кемер", rating: float = 8.4) -> list:
    """Строка aaData как её отдаёт живой шлюз: массив без имён, цена и звёзды — СТРОКИ."""
    out: list = [None] * (IDX_RATING + 1)
    out[IDX_HOTEL_NAME] = hotel
    out[IDX_PRICE] = f"{price} RUB"
    out[IDX_OPERATOR_NAME] = operator
    out[IDX_STARS] = f"{stars}*"
    out[IDX_RESORT] = resort
    out[IDX_RATING] = str(rating)
    return out


# --------------------------------- секреты ---------------------------------


def test_redact_hides_credentials():
    """Шлюз принимает логин и пароль в query — в логи они попадать не должны."""
    url = "https://module.sletat.ru/Main.svc/GetTours?login=vasya&password=hunter2&countryId=4"
    hidden = _redact(url)
    assert "vasya" not in hidden
    assert "hunter2" not in hidden
    assert "countryId=4" in hidden


# --------------------------------- выдача ---------------------------------


def test_only_requested_operator_is_kept():
    """Имя оператора лежит в самой строке — фильтрация достоверна, в отличие от карточек."""
    rows = [row("A Palace", 100000), row("B Grand", 90000, operator="Anex")]
    offers = build_hotel_offers(rows, PEGAS)
    assert [o.hotel_name for o in offers] == ["A Palace"]


def test_cheapest_price_per_hotel_wins():
    rows = [row("A Palace", 120000), row("A Palace", 99000)]
    offers = build_hotel_offers(rows, PEGAS)
    assert len(offers) == 1
    assert offers[0].price == Decimal("99000")


def test_fields_are_mapped():
    offers = build_hotel_offers([row("A Palace", 100000)], PEGAS)
    assert offers[0].stars == 5
    assert offers[0].destination == "Кемер"
    assert offers[0].rating == 8.4


def test_broken_rows_are_skipped_not_crashing():
    """Шлюз отдаёт массив без имён полей: короткая или битая строка не должна ронять прогон."""
    rows = [["слишком", "коротко"], row("A Palace", 100000), row("Zero", 0), row("", 50000)]
    offers = build_hotel_offers(rows, PEGAS)
    assert [o.hotel_name for o in offers] == ["A Palace"]


def test_offers_sorted_by_price():
    rows = [row("Dear", 300000), row("Cheap", 100000), row("Mid", 200000)]
    assert [o.hotel_name for o in build_hotel_offers(rows, PEGAS)] == ["Cheap", "Mid", "Dear"]


# --------------------------------- статусы операторов ---------------------------------


def test_error_and_timeout_mean_not_responding():
    """Главное различие инструмента: не ответил ≠ ответил «пусто»."""
    states = [
        {"Name": "Anex", "IsError": True, "IsProcessed": True, "RowsCount": 0},
        {"Name": "Coral", "IsTimeout": True, "IsProcessed": False, "RowsCount": 0},
    ]
    _, no_tours, not_responding = split_load_state(states)
    assert sorted(not_responding) == ["Anex", "Coral"]
    assert no_tours == []


def test_processed_with_zero_rows_means_no_tours():
    states = [{"Name": PEGAS, "IsProcessed": True, "RowsCount": 0}]
    _, no_tours, not_responding = split_load_state(states)
    assert no_tours == [PEGAS]
    assert not_responding == []


def test_rows_present_means_priced():
    states = [{"Name": PEGAS, "IsProcessed": True, "RowsCount": 12, "MinPrice": 99000}]
    priced, no_tours, not_responding = split_load_state(states)
    assert [(o.operator, o.price) for o in priced] == [(PEGAS, Decimal("99000"))]
    assert not no_tours and not not_responding


def test_skipped_operator_is_not_a_finding():
    """Оператора не опрашивали — записывать ему пропуск было бы враньём."""
    states = [{"Name": "Anex", "IsSkipped": True, "IsProcessed": False, "RowsCount": 0}]
    priced, no_tours, not_responding = split_load_state(states)
    assert not priced and not no_tours and not not_responding


def test_unfinished_operator_is_neither_priced_nor_no_tours():
    """Ещё считает — ни находка, ни цена; иначе поспешный опрос дал бы ложный пропуск."""
    states = [{"Name": "Anex", "IsProcessed": False, "RowsCount": 0}]
    priced, no_tours, not_responding = split_load_state(states)
    assert not priced and not no_tours and not not_responding


# --------------------------------- справочники ---------------------------------


def test_find_by_name_prefers_exact_match():
    items = [{"Id": 1, "Name": "Москва область"}, {"Id": 2, "Name": "Москва"}]
    assert _find_by_name(items, "Москва") == 2


def test_find_by_name_falls_back_to_substring():
    assert _find_by_name([{"Id": 7, "Name": "Турция (чартер)"}], "Турция") == 7


def test_find_by_name_is_case_insensitive():
    assert _find_by_name([{"Id": 7, "Name": "ТУРЦИЯ"}], "турция") == 7


def test_find_by_name_returns_none_when_absent():
    """Лучше честно не найти направление, чем угадать не то."""
    assert _find_by_name([{"Id": 7, "Name": "Египет"}], "Турция") is None
    assert _find_by_name(None, "Турция") is None


# --------------------------------- форматы живого шлюза ---------------------------------


def test_price_comes_with_currency_suffix():
    """Шлюз отдаёт «12015 RUB», а не число — сверено с живым ответом."""
    assert parse_price("12015 RUB") == Decimal("12015")
    assert parse_price("1 250 000 RUB") == Decimal("1250000")
    assert parse_price(27526) == Decimal("27526")
    assert parse_price("") is None
    assert parse_price(None) is None


def test_stars_come_with_asterisk():
    assert parse_stars("2*") == 2
    assert parse_stars("5*") == 5
    assert parse_stars("") is None
    assert parse_stars(None) is None


def test_operator_name_index_is_18_not_25():
    """Документация указывает индекс 25, но в живом ответе там пусто, а имя — на 18.
    Тест фиксирует проверенный факт, чтобы правка «по документации» его не сломала."""
    assert IDX_OPERATOR_NAME == 18


def test_rating_keeps_fraction():
    """Рейтинг разбирается отдельно от цены: через ценовой парсер «8.4» стало бы «8»."""
    from pegasgap.providers.sletat_api import parse_rating
    assert parse_rating("8.4") == 8.4
    assert parse_rating("8,4") == 8.4
    assert parse_rating("0") == 0.0
    assert parse_rating("") is None


# --------------------------------- город вылета ---------------------------------


def test_gateway_serves_only_one_departure_city():
    """Шлюз не применяет cityFromId: для Москвы, Петербурга, Казани и Тюмени он вернул
    побайтово одинаковые выдачу, цены (30332…76566) и счётчики — отличалось только эхо
    параметра в строке. Значит для любого другого города мы сравнивали бы РЕАЛЬНУЮ
    выдачу Турвизора с чужой нашей, и каждое расхождение было бы выдумкой."""
    from pegasgap.providers.sletat_api import GATEWAY_CITY, city_is_supported
    assert city_is_supported(GATEWAY_CITY)
    assert city_is_supported(GATEWAY_CITY.lower())      # регистр не должен мешать
    assert not city_is_supported("Казань")
    assert not city_is_supported("")


async def test_unsupported_city_fails_instead_of_lying():
    """Отказ, а не молчаливый неверный ответ: пустой результат честнее выдуманных находок."""
    from datetime import date

    from pegasgap.models import SearchParams
    from pegasgap.providers.sletat_api import SletatApiProvider
    params = SearchParams(
        departure_city="Казань", destination_country="Турция",
        date_from=date(2026, 9, 16), date_to=date(2026, 9, 23),
        nights_min=7, nights_max=7, adults=2,
    )
    result = await SletatApiProvider().search(params)
    assert result.success is False
    assert "города вылета" in (result.error or "")


def test_rate_limit_is_recognised_as_quota_not_breakage():
    """Шлюз считает поиски по IP и при превышении отвечает мгновенным отказом. Это квота,
    а не свойство направления: путать одно с другим значит либо выбросить рабочее
    направление из ранжирования, либо сжечь остаток лимита частыми повторами."""
    from pegasgap.ranking import is_rate_limited
    assert is_rate_limited(
        "IPS [172.30.67.201 - SLETAT.RU]: С вашего IP адреса превышен лимит "
        "кол-ва поисковых запросов. Обратитесь в службу поддержки")
    assert not is_rate_limited("поиск не завершился за 45 с")
    assert not is_rate_limited("")
