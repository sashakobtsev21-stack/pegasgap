"""Провайдер Турвизора через его собственные JSON-эндпоинты.

Основной путь для эталона. Заменяет драйвинг браузера: та же выдача, что видит
посетитель сайта, но данными — быстрее, полностью и без зависимости от вёрстки.

Протокол (снят с живой страницы, публичной документации у него нет):

1. `tourvisor.ru/xml/listdev.php?type=departure,allcountry,operator` — справочники
   городов вылета, стран и туроператоров: имя → id.
2. `tourvisor.ru/xml/listdev.php?type=allhotel&hotcountry=<id>` — словарь отелей страны
   (около 12 тысяч записей: id, имя, звёзды, рейтинг, регион). В выдаче поиска отели
   приходят ТОЛЬКО идентификаторами, имя берётся отсюда.
3. `tourvisor.ru/xml/modsearch.php?...&operators=<id>` — запуск поиска, возвращает
   `requestid` и список операторов со статусами. Фильтр по оператору серверный.
4. `search3.tourvisor.ru/modresult.php?requestid=<id>` — результат: блоки по операторам,
   в каждом отели с ценой. Готовность — `data.status.finished`.

**Оба эндпоинта требуют `referrer` параметром запроса и заголовок `Referer`.**

**Про полноту.** Выдача приходит страницами примерно по полтора десятка отелей, и её
надо забирать до конца — см. `_collect_pages`. То, что на сайте выглядит кнопкой
«показать ещё», это повторный вызов `modsearch.php` с `nextpage=1` и тем же `requestid`;
у `modresult.php` параметра страницы нет вовсе. Раньше это принимали за жёсткую обрезку
на стороне витрины, и сравнение шло против первой страницы: по Турции 15 отелей вместо
177, то есть восемь процентов эталона.

Полностью собранной выдача считается, когда очередная страница не приносит новых
отелей. Упёрлись в `MAX_PAGES` раньше — результат помечается `truncated`: недогруженный
отель неотличим от отсутствующего, и молчать об этом нельзя.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import httpx

from pegasgap.models import (
    HotelOffer,
    NotApplicableError,
    Offer,
    OperatorOffer,
    ProviderResult,
    SearchParams,
)
from pegasgap.names import operator_matches
from pegasgap.paramcheck import OfferFacts, verify
from pegasgap.providers.base import register_provider
from pegasgap.proxies import is_blocked, pool

log = logging.getLogger("pegasgap.providers.tourvisor_api")

LIST_URL = "https://tourvisor.ru/xml/listdev.php"
SEARCH_URL = "https://tourvisor.ru/xml/modsearch.php"
RESULT_URL = "https://search3.tourvisor.ru/modresult.php"

# Сколько страниц выдачи забирать. Каждая — отдельный запуск и опрос, то есть секунды
# времени и заметная нагрузка на площадку, которая и без того режет нас по IP. Десять
# страниц на живой Турции дали около полутора сотен отелей против пятнадцати на одной.
MAX_PAGES = int(os.environ.get("PEGASGAP_TOURVISOR_PAGES") or 10)

# Оба эндпоинта требуют реферер — и заголовком, и параметром запроса. Без него ответ
# приходит пустым; это часть контракта, а не обход защиты.
REFERER = "https://tourvisor.ru/"
# `referrer` должен указывать на ТУ САМУЮ страницу, чью форму мы имитируем: с туровым
# реферером запрос в режиме отелей отбивается 401. Проверяется, судя по всему, связка
# «страница ↔ formmode», а не просто наличие параметра.
REFERRER_TOURS = "https://tourvisor.ru/tours/"
REFERRER_HOTELS = "https://tourvisor.ru/poisk-otelej"

# Страницы направлений, где витрина применяет параметры поиска из адреса. Их немного —
# только те страны, у которых есть своя карта сайта. Путь при этом ЛИШЬ НОСИТЕЛЬ:
# `s_country` и `s_flyfrom` в запросе перекрывают его полностью (проверено — адрес
# турецко-московский, а форма показывает «Екатеринбург → Египет»). Слаг нужен только
# чтобы заголовок страницы не расходился с содержимым.
_LANDING_COUNTRIES = {
    "Турция": "turkey", "Египет": "egipet", "Таиланд": "tailand", "ОАЭ": "oae",
    "Вьетнам": "vietnam", "Абхазия": "abkhazia", "Шри-Ланка": "srilanka",
    "Мальдивы": "maldives", "Куба": "cuba", "Китай": "kitai", "Тунис": "tunis",
}
_LANDING_CITIES = {
    "Москва": "moskva", "Санкт-Петербург": "sankt-peterburg",
    "Екатеринбург": "ekaterinburg", "Новосибирск": "novosibirsk", "Казань": "kazan",
    "Самара": "samara", "Уфа": "ufa", "Красноярск": "krasnoyarsk", "Пермь": "perm",
}
# Запасной путь для направлений без своей страницы (Грузия, Россия): поиск по нему
# отработает верно, разойдётся только заголовок. Пустой `/tours/` не годится — на нём
# витрина параметры игнорирует, проверено.
_LANDING_FALLBACK = "turkey/moskva"

# «Город вылета» для режима без перелёта. Витрина моделирует проживание без билета как
# отдельный псевдогород, а не отдельный вид поиска.
_NO_FLIGHT_DEPARTURE = 99

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = float(os.environ.get("PEGASGAP_TV_POLL_TIMEOUT_S") or 120)

# Словарь отелей страны — около двух мегабайт JSON. Он меняется редко, а нужен на каждый
# прогон, поэтому держим в памяти процесса: при обходе матрицы направлений это разница
# между одной загрузкой и десятком одинаковых.
_HOTEL_CACHE: dict[int, dict[int, dict]] = {}
_LIST_CACHE: dict[str, Any] = {}


class TourvisorApiError(RuntimeError):
    """Эндпоинт ответил ошибкой или неожиданной структурой."""


def _to_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        result = float(str(value).replace(",", "."))
    except (ValueError, TypeError):
        return None
    return result or None


def offer_facts(blocks: list[dict], operators: dict[int, str] | None = None
                ) -> list[OfferFacts]:
    """Что каждый тур выдачи говорит о себе: дата вылета, ночи, оператор.

    Витрина кладёт это прямо в строку отеля (`tour[].dt`, `.nt`, `.op`), так что сверять
    можно не эхо запроса, а свойства найденного.
    """
    operators = operators or {}
    facts: list[OfferFacts] = []
    for block in blocks:
        for row in block.get("hotel") or []:
            for tour in row.get("tour") or []:
                facts.append(OfferFacts(
                    checkin=_parse_day(tour.get("dt")),
                    nights=_to_int(tour.get("nt")),
                    operator=operators.get(_to_int(tour.get("op")) or -1),
                ))
    return facts


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _blocks_are_ours(blocks: list[dict], operator_id: int) -> bool:
    """Все ли блоки выдачи принадлежат запрошенному оператору.

    Витрина размечает блоки идентификатором ТО, поэтому чужой блок означает ровно одно:
    серверный фильтр не применился. Отсев чужих блоков в `build_hotel_offers` уберёт из
    результата чужие цены, но не вернёт недостающие страницы наших — а недобор выдачи
    превращается в выдуманные пропуски.
    """
    seen = {_to_int(b.get("operator")) for b in blocks}
    seen.discard(None)
    return not seen or seen == {operator_id}


def _hotel_codes(blocks: list[dict]) -> set[int]:
    """Идентификаторы отелей во всех блоках — по ним меряется прирост страницы."""
    return {
        code
        for block in blocks
        for row in (block.get("hotel") or [])
        if (code := _to_int(row.get("id"))) is not None
    }


def build_hotel_offers(blocks: list[dict], hotels: dict[int, dict],
                       operator_id: int | None,
                       regions: dict[int, str] | None = None) -> list[HotelOffer]:
    """Блоки выдачи + словарь отелей → предложения по отелям, мин. цена на отель.

    Отель, которого нет в словаре, пропускаем: без имени его не с чем сопоставлять, а
    подставить идентификатор вместо названия значило бы гарантированно выдать его за
    отсутствующий на другой стороне.
    """
    regions = regions or {}
    best: dict[str, HotelOffer] = {}
    for block in blocks:
        if operator_id is not None and _to_decimal(block.get("operator")) != operator_id:
            continue
        for row in block.get("hotel") or []:
            ref = hotels.get(row.get("id"))
            if not ref:
                continue
            name = str(ref.get("name") or "").strip()
            price = _to_decimal(row.get("price"))
            if not name or price is None or price <= 0:
                continue
            offer = HotelOffer(
                provider="tourvisor",
                hotel_name=name,
                price=price,
                stars=_to_int(ref.get("stars")),
                rating=_to_float(ref.get("rating")),
                destination=regions.get(_to_int(ref.get("regioncode")) or -1),
                raw_label=str(row.get("id")),
            )
            seen = best.get(name)
            if seen is None or offer.price < seen.price:
                best[name] = offer
    return sorted(best.values(), key=lambda h: h.price)


def split_operators(states: list[dict]) -> tuple[list[Offer], list[str], list[str]]:
    """Список операторов из ответа → (офферы с ценой, «туров нет», «не отвечает»).

    Турвизор отдаёт по оператору `status` и `minprice`. Статус здесь беднее, чем у шлюза
    Слетать: отдельного признака «не ответил» нет, поэтому в эту категорию не относим
    никого — выдумывать её из нуля значило бы плодить ложные диагнозы. Эталону это и не
    нужно: его роль — показать, что у оператора есть, а разбираются со статусами на
    проверяемой стороне.
    """
    priced: list[Offer] = []
    no_tours: list[str] = []
    for state in states:
        name = str(state.get("name") or "").strip()
        if not name:
            continue
        price = _to_decimal(state.get("minprice"))
        if price and price > 0:
            priced.append(Offer(provider="tourvisor", operator=name, price=price))
        else:
            no_tours.append(name)
    return priced, no_tours, []


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Витрина сокращает названия городов, и по полному имени они не находятся: поиск из
# Петербурга молча превращался в «города нет на Турвизоре». Ключ — имя без точек,
# пробелов и дефисов в нижнем регистре, значение — как записано у витрины.
_CITY_ALIASES = {
    "санктпетербург": "С.Петербург",
    "нижнийновгород": "Н.Новгород",
    "минеральныеводы": "Мин.Воды",
    "петропавловсккамчатский": "П.Камчатский",
    "южносахалинск": "Ю.Сахалинск",
    "нижнийтагил": "Н.Тагил",
}


def _dictionary_key(name: str) -> str:
    return re.sub(r"[\s.\-]", "", (name or "").strip().casefold())


def _find_id(items: list[dict], wanted: str) -> int | None:
    """ID записи справочника по имени: алиас, точное совпадение, затем вхождение."""
    target = (wanted or "").strip().casefold()
    if not target:
        return None
    target = _CITY_ALIASES.get(_dictionary_key(target), target).casefold()

    exact = [i for i in items if str(i.get("name") or "").strip().casefold() == target]
    partial = [i for i in items if target in str(i.get("name") or "").strip().casefold()]
    for candidate in (exact, partial):
        if candidate:
            return _to_int(candidate[0].get("id"))
    return None


@register_provider("tourvisor_api")
class TourvisorApiProvider:
    """Поиск на Турвизоре через его JSON-эндпоинты."""

    name = "tourvisor"  # роль в отчёте та же, что у браузерного провайдера

    def __init__(self, headless: bool = True, timeout_ms: int = 90_000) -> None:
        # `headless` игнорируется — параметр в сигнатуре ради взаимозаменяемости
        # с браузерным провайдером по протоколу SearchProvider.
        self.timeout_ms = timeout_ms
        self.on_frame = None

    async def search(self, params: SearchParams) -> ProviderResult:
        start = time.monotonic()
        operator = params.operators[0] if params.operators else ""
        # Прокси берётся ОДИН на весь поиск: постраничный сбор ходит по одному requestid,
        # и витрина связывает его с адресом — сменить IP на середине значит получить
        # чужую страницу.
        proxy = pool().acquire()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_ms / 1000,
                headers={"Referer": REFERER, "User-Agent": DESKTOP_UA},
                proxy=proxy.url if proxy else None,
            ) as client:
                lists = await self._reference(client)
                city_id = _find_id(_items(lists, "departures", "departure"),
                                   params.departure_city)
                if city_id is None:
                    raise NotApplicableError(
                        f"город вылета «{params.departure_city}» не найден на Турвизоре")
                country_id = _find_id(_items(lists, "allcountry", "country"),
                                      params.destination_country)
                if country_id is None:
                    raise NotApplicableError(
                        f"направление «{params.destination_country}» недоступно на Турвизоре")
                operator_id = self._operator_id(lists, operator)
                request_id, states = await self._start(
                    client, params, city_id, country_id, operator_id)
                blocks, finished, complete = await self._collect_pages(
                    client, request_id, self._referrer(params))
                hotels = await self._hotels(client, country_id)
                regions = _region_names(lists)
        except NotApplicableError as exc:
            return self._fail(params, start, str(exc))
        except httpx.HTTPError as exc:
            # Сетевой отказ через прокси — чаще всего мёртвый прокси, а не мёртвая
            # площадка. Отправляем адрес остывать, чтобы следующий кейс взял другой.
            pool().penalise(proxy)
            return self._fail(params, start, f"Турвизор недоступен: {type(exc).__name__}: {exc}")
        except TourvisorApiError as exc:
            if is_blocked(str(exc)):
                pool().penalise(proxy)
            return self._fail(params, start, str(exc))

        dur = time.monotonic() - start
        offers, no_tours, not_responding = split_operators(states)
        if operator:
            offers = [o for o in offers if operator_matches(o.operator, operator)]
            no_tours = [n for n in no_tours if operator_matches(n, operator)]
        hotel_offers = build_hotel_offers(blocks, hotels, operator_id, regions)
        operator_offers = [
            OperatorOffer(provider=self.name, operator=o.operator, price=o.price,
                          hotel_name=hotel_offers[0].hotel_name if hotel_offers else None)
            for o in offers
        ]
        # Цена оператора в справке иногда приходит нулём, пока выдача уже есть — тогда
        # берём минимум по отелям, иначе статус оператора ложно читался бы как «туров нет».
        if not offers and hotel_offers and operator:
            offers = [Offer(provider=self.name, operator=operator,
                            price=hotel_offers[0].price)]
            no_tours = []
        log.info("Tourvisor (json): отелей у «%s»: %d за %.1f с", operator,
                 len(hotel_offers), dur)
        return ProviderResult(
            # Поиск состоялся — значит успех, даже если предложений ноль. Пустой эталон
            # это законный ответ «у оператора тут ничего нет», а не сбор данных: если
            # считать его неудачей, каждое такое направление помечало бы прогон
            # недостоверным и засоряло отчёт несуществующими проблемами.
            provider=self.name, success=finished,
            duration_seconds=dur, search_mode=params.search_mode,
            offers=offers, hotel_offers=hotel_offers, operator_offers=operator_offers,
            operators_no_tours=no_tours, operators_not_responding=not_responding,
            operator_filter_verified=(
                operator_id is not None and _blocks_are_ours(blocks, operator_id)),
            param_mismatches=verify(params, offer_facts(blocks), ""),
            search_url=self._page_url(params, city_id, country_id, operator_id),
            # Недогруженный отель неотличим от отсутствующего, поэтому обрезкой считается
            # и незавершённый поиск, и упёршийся в предел страниц: и там и там мы видели
            # не всю выдачу эталона, а её начало.
            truncated=not finished or not complete,
            error=None if finished else "Поиск не завершился за отведённое время.",
        )

    def _fail(self, params: SearchParams, start: float, error: str) -> ProviderResult:
        log.warning("Tourvisor (json): %s", error)
        return ProviderResult(
            provider=self.name, success=False,
            duration_seconds=time.monotonic() - start,
            search_mode=params.search_mode, error=error,
        )

    # --- вызовы ---

    async def _get(self, client: httpx.AsyncClient, url: str, referrer: str | None = None,
                   **query: Any) -> dict:
        response = await client.get(
            url, params={**query, "referrer": referrer or REFERRER_TOURS})
        if response.status_code != 200:
            raise TourvisorApiError(f"{url}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise TourvisorApiError(f"{url}: ответ не JSON") from exc

    async def _reference(self, client: httpx.AsyncClient) -> dict:
        if "lists" not in _LIST_CACHE:
            body = await self._get(client, LIST_URL, format="json", formmode="0",
                                   type="departure,allcountry,operator")
            _LIST_CACHE["lists"] = body.get("lists") or {}
        return _LIST_CACHE["lists"]

    def _operator_id(self, lists: dict, operator: str) -> int | None:
        if not operator:
            return None
        items = _items(lists, "operators", "operator")
        # Отключённые операторы фильтр примет, но выдача будет пуста — это выглядело бы
        # как пропуск. Берём только активных.
        active = [o for o in items if o.get("active")]
        for item in active:
            if operator_matches(str(item.get("name") or ""), operator):
                return _to_int(item.get("id"))
        log.warning("Tourvisor (json): оператор «%s» не найден среди активных", operator)
        return None

    async def _start(self, client: httpx.AsyncClient, params: SearchParams, city_id: int,
                     country_id: int, operator_id: int | None) -> tuple[int, list[dict]]:
        hotels_only = params.search_mode == "hotels"
        query: dict[str, Any] = {
            "datefrom": _fmt(params.date_from), "dateto": _fmt(params.date_to),
            "nightsfrom": params.nights_min, "nightsto": params.nights_max,
            "adults": params.adults, "child": len(params.children_ages),
            "country": country_id,
            # Режим «Отели» — это тот же поиск с другой формой: `formmode=1` и особый
            # «город вылета» 99 («Без перелета»). Отдельного протокола у витрины нет,
            # хотя форма на сайте выглядит самостоятельной.
            "departure": _NO_FLIGHT_DEPARTURE if hotels_only else city_id,
            "formmode": 1 if hotels_only else 0,
            "directflight": 1 if params.direct_only else 0,
            # regular=0 не ограничивает поиск регулярными рейсами: на массовых
            # направлениях перевозка чартерная, и с regular=1 выдача была бы куцей.
            "regular": 0,
            "meal": 0, "rating": 0, "pricefrom": 0, "priceto": 0,
            "currency": 0, "pricetype": 0,
        }
        if params.children_ages:
            query["childage1"] = params.children_ages[0]
        if operator_id is not None:
            query["operators"] = operator_id
        body = await self._get(client, SEARCH_URL, referrer=self._referrer(params), **query)
        result = body.get("result") or {}
        request_id = _to_int(result.get("requestid"))
        if not request_id:
            raise TourvisorApiError("modsearch не вернул requestid")
        return request_id, list(result.get("operators") or [])

    @staticmethod
    def _page_url(params: SearchParams, city_id: int, country_id: int,
                  operator_id: int | None) -> str:
        """Адрес того же поиска на самой витрине — чтобы находку можно было открыть.

        Формат снят с их собственного сборщика (`formUri` в core.min.js) и проверен
        живьём. Ключевой параметр — `ts_dosearch=1`: без него страница показывает свои
        значения по умолчанию, и ссылка вела бы на чужой поиск. Оператор — `s_oplimit`,
        отель — `x_hotel_codes` (его добавляет вызывающий код, он знает конкретный отель).
        """
        hotels_only = params.search_mode == "hotels"
        country = _LANDING_COUNTRIES.get(params.destination_country.strip())
        city = _LANDING_CITIES.get(params.departure_city.strip())
        path = f"{country}/{city}" if country and city else _LANDING_FALLBACK

        fields = {
            "ts_dosearch": 1,
            "s_form_mode": 1 if hotels_only else 0,
            "s_flyfrom": _NO_FLIGHT_DEPARTURE if hotels_only else city_id,
            "s_country": country_id,
            "s_nights_from": params.nights_min,
            "s_nights_to": params.nights_max,
            "s_j_date_from": params.date_from.strftime("%d.%m.%Y"),
            "s_j_date_to": params.date_to.strftime("%d.%m.%Y"),
            "s_adults": params.adults,
            "s_currency": 0,
        }
        if params.children_ages:
            fields["s_child"] = len(params.children_ages)
            for i, age in enumerate(params.children_ages[:3], start=1):
                fields[f"child_age_{i}"] = age
        if operator_id is not None:
            fields["s_oplimit"] = operator_id
        return f"https://tourvisor.ru/tours/{path}?{urlencode(fields)}"

    @staticmethod
    def _referrer(params: SearchParams) -> str:
        return REFERRER_HOTELS if params.search_mode == "hotels" else REFERRER_TOURS

    async def _collect_pages(self, client: httpx.AsyncClient, request_id: int,
                             referrer: str) -> tuple[list[dict], bool, bool]:
        """Собрать выдачу целиком, а не первую страницу. Возвращает (блоки, дошли, всё).

        Витрина отдаёт результат порциями примерно по полтора десятка отелей, и то, что
        на сайте выглядит кнопкой «показать ещё», — это повторный вызов `modsearch.php`
        с `nextpage=1` и тем же `requestid`. Не `modresult.php`: там никакого параметра
        страницы нет, и именно поэтому обрезка так долго выглядела свойством площадки.

        Цена ошибки была велика. Живая сверка по Турции: первая страница — 15 отелей,
        обход страниц — 177, и на двенадцатой новые всё ещё шли. То есть мы сравнивали
        свой полный каталог с восемью процентами эталона и физически не могли увидеть
        пропуск за пределами первой страницы.

        Ответ каждой следующей страницы КУМУЛЯТИВЕН — приходит вся выдача с начала, а не
        только прирост. Поэтому блоки не склеиваются, а замещаются последним ответом;
        считать при этом надо прирост уникальных отелей, иначе остановка не наступит.
        """
        blocks, finished = await self._await_result(client, request_id, referrer)
        if not finished:
            return blocks, False, False

        seen = _hotel_codes(blocks)
        # Отелей нет вовсе — листать нечего, и это ПОЛНЫЙ ответ, а не недобор. Раньше
        # цикл выходил по break и падал в ветку «упёрлись в предел», из-за чего каждое
        # «у оператора тут туров нет» помечалось обрезанной выдачей.
        if not seen:
            return blocks, True, True

        for _ in range(MAX_PAGES - 1):
            body = await self._get(client, SEARCH_URL, referrer=referrer,
                                   nextpage=1, requestid=request_id)
            next_id = _to_int((body.get("result") or {}).get("requestid"))
            if not next_id:
                return blocks, True, True      # витрина сказала, что дальше ничего нет
            request_id = next_id
            page_blocks, finished = await self._await_result(client, request_id, referrer)
            if not finished:
                return blocks, True, False     # оборвались на середине — выдача неполная
            codes = _hotel_codes(page_blocks)
            if not codes - seen:
                return blocks, True, True      # прирост иссяк — выдача собрана целиком
            seen |= codes
            blocks = page_blocks
        log.info("Tourvisor (json): предел в %d страниц исчерпан, отелей набрано %d",
                 MAX_PAGES, len(seen))
        return blocks, True, False

    async def _await_result(self, client: httpx.AsyncClient, request_id: int,
                            referrer: str) -> tuple[list[dict], bool]:
        """Опрашивать результат до `status.finished`. Второй элемент — успели ли."""
        deadline = time.monotonic() + POLL_TIMEOUT_S
        blocks: list[dict] = []
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL_S)
            data = (await self._get(client, RESULT_URL, referrer=referrer,
                                    requestid=request_id)).get("data") or {}
            blocks = list(data.get("block") or [])
            if (data.get("status") or {}).get("finished"):
                return blocks, True
        log.warning("Tourvisor (json): поиск не завершился за %.0f с", POLL_TIMEOUT_S)
        return blocks, False

    async def _hotels(self, client: httpx.AsyncClient, country_id: int) -> dict[int, dict]:
        cached = _HOTEL_CACHE.get(country_id)
        if cached is not None:
            return cached
        body = await self._get(client, LIST_URL, type="allhotel",
                               hotcountry=country_id, format="json")
        items = _items(body.get("lists") or {}, "hotels", "hotel")
        table = {h["id"]: h for h in items if h.get("id") is not None}
        log.info("Tourvisor (json): словарь отелей страны %s — %d записей",
                 country_id, len(table))
        _HOTEL_CACHE[country_id] = table
        return table


def _items(lists: dict, group: str, key: str) -> list[dict]:
    """Достать список из `lists[group][key]` — форма ответа справочников."""
    node = lists.get(group)
    if isinstance(node, dict):
        node = node.get(key)
    return [i for i in (node or []) if isinstance(i, dict)]


def _region_names(lists: dict) -> dict[int, str]:
    out: dict[int, str] = {}
    for group, key in (("regions", "region"), ("subregions", "subregion")):
        for item in _items(lists, group, key):
            rid = _to_int(item.get("id"))
            if rid is not None and item.get("name"):
                out[rid] = str(item["name"])
    return out


def _fmt(value: date) -> str:
    """Витрина ожидает ДД.ММ.ГГГГ."""
    return value.strftime("%d.%m.%Y")
