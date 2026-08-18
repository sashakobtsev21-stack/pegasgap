"""Ссылка на тот же поиск на Слетать — чтобы находку можно было открыть и увидеть самому.

Без неё отчёт требует доверия на слово: «этого отеля у нас нет» проверяется только
повторением поиска руками, по десятку полей формы. Ссылка превращает разбор находки в
один клик.

Формат пути снят с живой площадки (публичного описания нет):

    /search/from-<город>-to-<страна>-for-<месяц>-nights-<мин>..<макс>
            -adults-<N>-kids-<возрасты|zero>
        ?datefrom=ДД/ММ/ГГГГ&dateto=ДД/ММ/ГГГГ&currency=RUB&ticketsincluded=<bool>

Город, страна и месяц в пути — **английские**, и это главная ловушка: составные имена
пишутся через ПОДЧЁРКИВАНИЕ, потому что дефис у площадки разделяет поля пути. На дефисе
`saint-petersburg` молча не опознаётся, город выпадает из поиска, и ссылка ведёт не туда
— то есть врёт правдоподобно. Проверено живьём на всех десяти городах и странах матрицы.

Английские имена стран отдаёт сам шлюз (`OriginalName` в `GetCountries`), а для городов
он возвращает пустое поле — их словарь пришлось снять со страницы площадки и держать
здесь. Незнакомый город ссылки не даёт: пустое место честнее ссылки на чужой поиск.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlencode

from pegasgap.models import SearchParams

BASE = "https://sletat.ru/search"

# Месяц в пути — английский, в нижнем регистре.
_MONTHS = ["january", "february", "march", "april", "may", "june",
           "july", "august", "september", "october", "november", "december"]

# Города вылета: русское имя → английское. Взято из данных самой площадки
# (`departureCities[].originalName`), потому что шлюз для городов отдаёт null.
CITY_NAMES = {
    "Москва": "Moscow",
    "Санкт-Петербург": "Saint-Petersburg",
    "Екатеринбург": "Yekaterinburg",
    "Новосибирск": "Novosibirsk",
    "Казань": "Kazan",
    "Самара": "Samara",
    "Уфа": "Ufa",
    "Краснодар": "Krasnodar",
    "Красноярск": "Krasnoyarsk",
    "Пермь": "Perm",
    "Нижний Новгород": "Nizhny Novgorod",
    "Ростов-на-Дону": "Rostov-on-Don",
    "Тюмень": "Tyumen",
    "Челябинск": "Chelyabinsk",
}

# Страны: русское имя → английское (`OriginalName` из GetCountries).
COUNTRY_NAMES = {
    "Турция": "Turkey",
    "Египет": "Egypt",
    "ОАЭ": "UAE",
    "Таиланд": "Thailand",
    "Вьетнам": "Vietnam",
    "Россия": "Russia",
    "Абхазия": "Abkhazia",
    "Грузия": "Georgia",
    "Мальдивы": "Maldives",
    "Шри-Ланка": "Sri Lanka",
    "Кипр": "Cyprus",
    "Тунис": "Tunisia",
    "Индия": "India",
    "Куба": "Cuba",
    "Доминикана": "Dominican Republic",
}


def slugify(name: str) -> str:
    """Английское имя → фрагмент пути.

    Пробелы и дефисы становятся подчёркиванием: дефис у площадки разделяет поля пути,
    и `saint-petersburg` она разбирает как обрывки, а не как город.
    """
    return re.sub(r"[\s\-]+", "_", (name or "").strip().lower())


def _kids(ages: list[int]) -> str:
    return ".".join(str(a) for a in ages) if ages else "zero"


def search_url(params: SearchParams) -> str | None:
    """Ссылка на тот же поиск. None — города или страны нет в словаре.

    Молчаливая подстановка чего-то похожего здесь недопустима: ссылка на соседний город
    выглядит рабочей и уводит разбор в сторону.
    """
    city = CITY_NAMES.get((params.departure_city or "").strip())
    country = COUNTRY_NAMES.get((params.destination_country or "").strip())
    if not city or not country:
        return None

    path = (f"from-{slugify(city)}-to-{slugify(country)}"
            f"-for-{_MONTHS[params.date_from.month - 1]}"
            f"-nights-{params.nights_min}..{params.nights_max}"
            f"-adults-{params.adults}-kids-{_kids(list(params.children_ages))}")
    query = urlencode({
        "datefrom": params.date_from.strftime("%d/%m/%Y"),
        "dateto": params.date_to.strftime("%d/%m/%Y"),
        "currency": params.currency,
        # Режим «отели» — это поиск без перелёта.
        "ticketsincluded": "true" if params.search_mode == "tours" else "false",
    })
    return f"{BASE}/{path}?{query}"


def search_url_from_row(params: dict) -> str | None:
    """То же, но из сохранённого словаря параметров прогона (`params_json`)."""
    try:
        return search_url(SearchParams(
            departure_city=params["departure_city"],
            destination_country=params["destination_country"],
            date_from=date.fromisoformat(params["date_from"]),
            date_to=date.fromisoformat(params["date_to"]),
            nights_min=params["nights_min"], nights_max=params["nights_max"],
            adults=params["adults"],
            children_ages=list(params.get("children_ages") or []),
            search_mode=params.get("search_mode") or "tours",
            currency=params.get("currency") or "RUB",
        ))
    except (KeyError, ValueError):
        return None
