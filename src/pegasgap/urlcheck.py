"""Проверка параметров поиска по URL результата.

После клика «Найти» площадка формирует URL, кодирующий ВСЕ параметры поиска — это
надёжный «источник правды» того, что реально искалось (виджеты могут врать: напр.
React-инпут показывает одно, а в поиск уходит дефолт). Здесь — чистые функции разбора
и сверки URL, легко покрываемые тестами.

Примеры:
  Sletat:
    https://sletat.ru/search/from-moscow-to-turkey-for-june-nights-7..10-adults-2-kids-zero
      ?datefrom=27/06/2026&dateto=30/06/2026&currency=RUB&ticketsincluded=true
      &onlyCharter=false&onlyInstant=false&onlyDirect=false&onlyTransfer=false
  Tourvisor:
    https://tourvisor.ru/tours/turkey/moskva?s_nights_from=6&s_nights_to=14
      &s_directflight=0&s_j_date_from=31.05.2026&s_j_date_to=09.06.2026&s_adults=2
      &s_country=4&s_currency=0
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from pegasgap.models import SearchParams

Problem = tuple[str, object, object]  # (field, expected, actual)


# --------------------------- Sletat ---------------------------

_SLETAT_PATH = re.compile(
    r"/search/(?:from-(?P<city>.+?)-)?to-(?P<country>.+?)-for-(?P<month>[a-z]+)"
    r"-nights-(?P<nmin>\d+)\.\.(?P<nmax>\d+)-adults-(?P<adults>\d+)-kids-(?P<kids>[^/?]+)",
    re.IGNORECASE,
)


def parse_sletat_url(url: str) -> dict:
    u = urlparse(url)
    query = {k: v[0] for k, v in parse_qs(u.query).items()}
    out: dict = {"query": query, "path": u.path}
    m = _SLETAT_PATH.search(u.path)
    if m:
        out.update(m.groupdict())
        # Капча kids жадная (`[^/?]+`): после kids в пути могут идти ещё сегменты —
        # напр. `-kids-zero-stars-3,4,5` при выбранных звёздах. Иначе цифры звёзд
        # считались бы за детей (children_count). Оставляем только сам kids-токен
        # (`zero` или возрасты), отрезая последующие сегменты `-<буквы>-…`.
        if out.get("kids"):
            out["kids"] = re.split(r"-(?=[a-zA-Z])", out["kids"], maxsplit=1)[0]
    return out


def _kids_count(token: str) -> int:
    # Токен kids кодирует ВОЗРАСТЫ детей, а не их число: "zero" = 0 детей,
    # "7" = один ребёнок (7 лет), "5.10"/"5-10" = двое. Считаем группы цифр.
    if not token or token == "zero":
        return 0
    return len(re.findall(r"\d+", token))


def verify_sletat_search_url(url: str, params: SearchParams) -> list[Problem]:
    """Сверить параметры поиска Sletat по URL результата. Возвращает расхождения."""
    p = parse_sletat_url(url)
    q = p.get("query", {})
    problems: list[Problem] = []

    def eq(field, expected, actual) -> None:
        if str(expected) != str(actual):
            problems.append((field, expected, actual))

    if "nmin" in p:
        # В режиме «Отели» ночи задаются диапазоном дат (отдельного контрола нет) — не сверяем.
        if params.search_mode != "hotels":
            eq("nights_min", params.nights_min, int(p["nmin"]))
            eq("nights_max", params.nights_max, int(p["nmax"]))
        eq("adults", params.adults, int(p["adults"]))
        eq("children_count", len(params.children_ages), _kids_count(p["kids"]))

    if "datefrom" in q:
        # datefrom = дата заезда — совпадает с date_from в обоих режимах.
        eq("date_from", params.date_from.strftime("%d/%m/%Y"), q["datefrom"])
    if "dateto" in q and params.search_mode != "hotels":
        # В режиме «Отели» dateto — это дата ВЫЕЗДА (заезд + ночи, минимум 1 ночь),
        # а не конец окна вылета: Sletat выводит ночи из диапазона дат и при совпадении
        # date_from==date_to ставит checkout = date+1. Поэтому dateto тут не сверяем
        # (как и ночи выше) — иначе валидный поиск ложно падает.
        eq("date_to", params.date_to.strftime("%d/%m/%Y"), q["dateto"])
    if "currency" in q:
        eq("currency", params.currency, q["currency"])

    # режим: туры -> ticketsincluded=true, отели (без перелёта) -> false
    if "ticketsincluded" in q:
        eq("search_mode/tickets", "true" if params.search_mode == "tours" else "false", q["ticketsincluded"])

    for field, key in [
        ("charter_only", "onlyCharter"),
        ("direct_only", "onlyDirect"),
        ("with_transfer", "onlyTransfer"),
        ("instant_confirmation", "onlyInstant"),
    ]:
        if key in q:
            eq(field, str(getattr(params, field)).lower(), q[key])

    return problems


# --------------------------- Tourvisor ---------------------------

def parse_tourvisor_url(url: str) -> dict:
    u = urlparse(url)
    query = {k: v[0] for k, v in parse_qs(u.query).items()}
    out: dict = {"query": query, "path": u.path}
    m = re.search(r"/tours/(?P<country>[^/]+)/(?P<city>[^/?]+)", u.path)
    if m:
        out.update(m.groupdict())
    return out


def verify_tourvisor_search_url(url: str, params: SearchParams) -> list[Problem]:
    """Сверить параметры поиска Tourvisor по URL (/tours/...). Возвращает расхождения."""
    q = parse_tourvisor_url(url).get("query", {})
    problems: list[Problem] = []

    def eq(field, expected, actual) -> None:
        if str(expected) != str(actual):
            problems.append((field, expected, actual))

    if "s_nights_from" in q:
        eq("nights_min", params.nights_min, int(q["s_nights_from"]))
    if "s_nights_to" in q:
        eq("nights_max", params.nights_max, int(q["s_nights_to"]))
    if "s_adults" in q:
        eq("adults", params.adults, int(q["s_adults"]))
    if "s_j_date_from" in q:
        eq("date_from", params.date_from.strftime("%d.%m.%Y"), q["s_j_date_from"])
    if "s_j_date_to" in q:
        eq("date_to", params.date_to.strftime("%d.%m.%Y"), q["s_j_date_to"])
    if "s_directflight" in q:
        eq("direct_only", "1" if params.direct_only else "0", q["s_directflight"])

    return problems
