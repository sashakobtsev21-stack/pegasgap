"""Провайдер Слетать через JSON-шлюз поиска туров (`module.sletat.ru/Main.svc`).

Основной путь для проверяемой стороны. Он лучше браузерного не только скоростью:

* **Статус оператора приходит фактом.** `GetLoadState` отдаёт по каждому ТО
  `IsProcessed`, `RowsCount`, `IsError`, `IsTimeout` — то самое различие «отработал и
  вернул пусто» против «не ответил», ради которого всё затевалось. В браузерном пути его
  приходилось вычитывать из подписей панели «блинчик».
* **Фильтр по оператору применяет сервер.** `filter=1&f_to_id=<id>` возвращает строки
  ровно одного ТО, и это проверено: выдача по Pegas состоит из Pegas на 100%. Главная
  угроза точности отчёта — «цена относится не к тому оператору» — здесь исчезает.
* Нет вёрстки — нечему протухнуть от редизайна.

Протокол: `GetTours(requestId=0)` создаёт поиск → опрос `GetLoadState` до готовности всех
операторов → `GetTours(requestId, updateResult=1)` постранично за результатом.

**Что сверено с живым шлюзом (документация в вики местами расходится с ответом).**

* Авторизация не требуется, но **обязателен заголовок `Referer`** — без него любой вызов
  возвращает `IsError` с просьбой включить передачу HTTP REFERER.
* Ошибка лежит в `IsError` / `ErrorMessage`, а не в поле `Error`.
* Имя оператора — индекс **18**; документированный индекс 25 в ответе пуст.
* Цена приходит строкой `«12015 RUB»`, звёздность — строкой `«2*»`.

Логин и пароль поддержаны на случай, если шлюз когда-нибудь потребует их для расширенной
выдачи: значения берутся только из окружения, а URL в логи уходит с вырезанными кредами.
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
from pegasgap.providers.base import register_provider

log = logging.getLogger("pegasgap.providers.sletat_api")

BASE_URL = os.environ.get("SLETAT_API_URL") or "https://module.sletat.ru/Main.svc"

# Шлюз отбивает вызовы без Referer сообщением «Ваш браузер не настроен для передачи HTTP
# REFERER». Это не антибот-обход, а обязательный элемент его контракта: без заголовка
# метод возвращает IsError и пустой Data.
REFERER = os.environ.get("SLETAT_API_REFERER") or "https://sletat.ru/"

# Индексы значимых полей в строке `aaData` (строка длиной ~100 позиций, без имён).
# Значения СВЕРЕНЫ с живым ответом, а не взяты из документации: она указывает имя
# оператора на позиции 25, тогда как реально там пусто, а имя лежит на 18.
IDX_PRICE_ID = 0
IDX_OPERATOR_ID = 1
IDX_HOTEL_ID = 3
IDX_HOTEL_NAME = 7
IDX_STARS = 8
IDX_ROOM = 9
IDX_MEAL = 10
IDX_DATE_FROM = 12
IDX_NIGHTS = 14
IDX_PRICE = 15
IDX_OPERATOR_NAME = 18
IDX_RESORT = 19
IDX_COUNTRY = 31
IDX_RATING = 35
_MIN_ROW_LEN = IDX_RATING + 1

PAGE_SIZE = int(os.environ.get("PEGASGAP_API_PAGE_SIZE") or 1000)
# Предохранитель от бесконечной пагинации. У крупного оператора на популярном направлении
# бывает под десять тысяч строк, поэтому запас нужен ощутимый; о его срабатывании
# вызывающий код узнаёт через `truncated` — молча обрезанная выдача породила бы пропуски.
MAX_PAGES = int(os.environ.get("PEGASGAP_API_MAX_PAGES") or 15)

POLL_INTERVAL_S = 1.5   # рекомендация документации шлюза
POLL_TIMEOUT_S = float(os.environ.get("PEGASGAP_API_POLL_TIMEOUT_S") or 120)

# Город вылета по умолчанию — для CLI и для режима «без перелёта», где город не значит
# ничего. Ограничением он больше не является.
#
# Раньше здесь стоял запрет на всё, кроме Москвы: считалось, что шлюз не применяет
# `cityFromId`. Это оказалось неверно — вывод был сделан по слишком грубому сравнению.
# Перепроверка с разбором самих выдач: Москва 983 отеля, Петербург 899, Екатеринбург 906,
# Казань 703, Новосибирск 690 (Турция, одно окно, Pegas), отпечатки состава и цен у всех
# разные. Города различаются, и измерение городов вылета открыто.
GATEWAY_CITY = os.environ.get("PEGASGAP_GATEWAY_CITY") or "Москва"

_SECRET_RE = re.compile(r"(login|password)=[^&]*", re.IGNORECASE)
_LEADING_NUMBER_RE = re.compile(r"\d[\d\s]*")


def _redact(url: str) -> str:
    """URL без значений логина и пароля — единственная форма, пригодная для логов."""
    return _SECRET_RE.sub(r"\1=***", url)


class SletatApiError(RuntimeError):
    """Шлюз ответил ошибкой или неожиданной структурой."""


def parse_price(value: Any) -> Decimal | None:
    """Цена из строки вида «12015 RUB» (шлюз отдаёт её с валютой, а не числом)."""
    if value is None:
        return None
    if isinstance(value, int | float | Decimal):
        return Decimal(str(value))
    match = _LEADING_NUMBER_RE.search(str(value))
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(" ", ""))
    except InvalidOperation:
        return None


def parse_stars(value: Any) -> int | None:
    """Звёздность из строки вида «2*»."""
    if value is None:
        return None
    match = re.search(r"\d", str(value))
    return int(match.group(0)) if match else None


def parse_rating(value: Any) -> float | None:
    """Рейтинг отеля (0–10) из строки вида «8.4».

    Отдельно от `parse_price`: там дробная часть намеренно отбрасывается (цены целые, а
    пробел — разделитель тысяч), и рейтинг через неё превращался бы в «8» вместо «8.4».
    """
    if value is None:
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", str(value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_hotel_offers(rows: list[list], operator: str) -> list[HotelOffer]:
    """Строки `aaData` → предложения по отелям указанного оператора, мин. цена на отель.

    Фильтр по оператору уже применён сервером, но проверяем и здесь: если параметр
    когда-нибудь перестанет действовать, отчёт должен опустеть, а не наполниться чужими
    ценами под видом наших.
    """
    best: dict[str, HotelOffer] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < _MIN_ROW_LEN:
            continue
        if operator and not operator_matches(str(row[IDX_OPERATOR_NAME] or ""), operator):
            continue
        name = str(row[IDX_HOTEL_NAME] or "").strip()
        price = parse_price(row[IDX_PRICE])
        if not name or price is None or price <= 0:
            continue
        rating = parse_rating(row[IDX_RATING])
        offer = HotelOffer(
            provider="sletat",
            hotel_name=name,
            price=price,
            stars=parse_stars(row[IDX_STARS]),
            rating=rating or None,
            destination=str(row[IDX_RESORT] or "").strip() or None,
            raw_label=str(row[IDX_HOTEL_ID] or ""),
        )
        seen = best.get(name)
        if seen is None or offer.price < seen.price:
            best[name] = offer
    return sorted(best.values(), key=lambda h: h.price)


def split_load_state(states: list[dict]) -> tuple[list[Offer], list[str], list[str]]:
    """`GetLoadState` → (офферы с ценой, «туров нет», «не отвечает»).

    Прямая замена разбора панели «блинчик», только сведения приходят фактом:

    * `IsError` / `IsTimeout` — оператор не ответил (таймаут, бан, падение плагина);
    * `IsProcessed` и `RowsCount == 0` — отработал и честно вернул пусто;
    * `RowsCount > 0` — есть предложения.

    `IsSkipped` не относится ни к одному случаю: оператор в этом поиске не опрашивался,
    и записывать ему пропуск было бы враньём. Незавершённый (`IsProcessed=False` без
    ошибки) — тоже: он ещё считает.
    """
    priced: list[Offer] = []
    no_tours: list[str] = []
    not_responding: list[str] = []
    for state in states:
        name = str(state.get("Name") or "").strip()
        if not name or state.get("IsSkipped"):
            continue
        if state.get("IsError") or state.get("IsTimeout"):
            not_responding.append(name)
            continue
        rows = _to_int(state.get("RowsCount")) or 0
        if rows > 0:
            price = parse_price(state.get("MinPrice"))
            if price and price > 0:
                priced.append(Offer(provider="sletat", operator=name, price=price))
        elif state.get("IsProcessed"):
            no_tours.append(name)
    return priced, no_tours, not_responding


@register_provider("sletat_api")
class SletatApiProvider:
    """Поиск на Слетать через JSON-шлюз."""

    name = "sletat"  # роль в отчёте та же, что у браузерного провайдера

    def __init__(self, headless: bool = True, timeout_ms: int = 90_000,
                 login: str | None = None, password: str | None = None) -> None:
        # `headless` игнорируется — параметр в сигнатуре, чтобы провайдер оставался
        # взаимозаменяемым с браузерным по протоколу SearchProvider.
        self.timeout_ms = timeout_ms
        self._login = login or os.environ.get("SLETAT_LOGIN") or ""
        self._password = password or os.environ.get("SLETAT_PASSWORD") or ""
        self.on_frame = None

    async def search(self, params: SearchParams) -> ProviderResult:
        start = time.monotonic()
        operator = params.operators[0] if params.operators else ""
        try:
            async with httpx.AsyncClient(timeout=self.timeout_ms / 1000,
                                         headers={"Referer": REFERER}) as client:
                city_id = await self._resolve_city(client, params.departure_city)
                country_id = await self._resolve_country(client, city_id,
                                                         params.destination_country)
                operator_id = await self._resolve_operator(client, country_id, operator)
                request_id = await self._start_search(
                    client, params, city_id, country_id, operator_id)
                states = await self._await_completion(client, request_id)
                rows, truncated = await self._fetch_rows(
                    client, params, city_id, country_id, operator_id, request_id)
        except NotApplicableError as exc:
            return self._fail(params, start, str(exc))
        except httpx.HTTPError as exc:
            return self._fail(params, start, f"Сеть/шлюз: {type(exc).__name__}: {exc}")
        except SletatApiError as exc:
            return self._fail(params, start, str(exc))

        dur = time.monotonic() - start
        priced, no_tours, not_responding = split_load_state(states)
        hotels = build_hotel_offers(rows, operator)
        offers = [o for o in priced if not operator or operator_matches(o.operator, operator)]
        operator_offers = [
            OperatorOffer(provider=self.name, operator=o.operator, price=o.price,
                          hotel_name=hotels[0].hotel_name if hotels else None)
            for o in offers
        ]
        log.info("Слетать (шлюз): операторов %d, строк %d, отелей у «%s»: %d, за %.1f с",
                 len(states), len(rows), operator, len(hotels), dur)
        return ProviderResult(
            provider=self.name, success=True, duration_seconds=dur,
            search_mode=params.search_mode,
            offers=offers, hotel_offers=hotels, operator_offers=operator_offers,
            operators_no_tours=no_tours, operators_not_responding=not_responding,
            # Фильтр применяет сервер (filter=1&f_to_id), а состав выдачи дополнительно
            # проверяется по имени оператора в каждой строке.
            operator_filter_verified=operator_id is not None,
            truncated=truncated,
        )

    def _fail(self, params: SearchParams, start: float, error: str) -> ProviderResult:
        log.warning("Слетать (шлюз): %s", error)
        return ProviderResult(
            provider=self.name, success=False,
            duration_seconds=time.monotonic() - start,
            search_mode=params.search_mode, error=error,
        )

    # --- вызовы шлюза ---

    async def _call(self, client: httpx.AsyncClient, method: str, **query: Any) -> Any:
        """Вызвать метод шлюза и вернуть содержимое `<Method>Result.Data`."""
        payload: dict[str, Any] = dict(query)
        # Шлюз работает анонимно; доступы шлём, только если они заданы.
        if self._login and self._password:
            payload |= {"login": self._login, "password": self._password}
        response = await client.get(f"{BASE_URL}/{method}", params=payload)
        log.debug("шлюз %s → %s", _redact(str(response.url)), response.status_code)
        if response.status_code != 200:
            raise SletatApiError(f"{method}: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SletatApiError(f"{method}: ответ не JSON") from exc
        result = body.get(f"{method}Result") or {}
        # Ошибка лежит именно в IsError/ErrorMessage — сверено с живым ответом.
        if result.get("IsError"):
            raise SletatApiError(f"{method}: {result.get('ErrorMessage') or 'ошибка шлюза'}")
        data = result.get("Data")
        if data is None:
            raise SletatApiError(f"{method}: в ответе нет Data")
        return data

    async def _resolve_city(self, client: httpx.AsyncClient, city: str) -> int:
        found = _find_by_name(await self._call(client, "GetDepartCities"), city)
        if found is None:
            raise NotApplicableError(f"город вылета «{city}» не найден в справочнике Слетать")
        return found

    async def _resolve_country(self, client: httpx.AsyncClient, city_id: int,
                               country: str) -> int:
        data = await self._call(client, "GetCountries", cityFromId=city_id)
        found = _find_by_name(data, country)
        if found is None:
            # Детерминированный отказ: направление не обслуживается из этого города.
            # Не сбой и не пропуск — сравнивать по такому запросу нечего.
            raise NotApplicableError(
                f"направление «{country}» недоступно из «{city_id}» на Слетать")
        return found

    async def _resolve_operator(self, client: httpx.AsyncClient, country_id: int,
                                operator: str) -> int | None:
        """ID оператора для серверного фильтра. None = ищем без фильтра по ТО."""
        if not operator:
            return None
        data = await self._call(client, "GetTourOperators", countryId=country_id)
        if not isinstance(data, list):
            return None
        # Отключённые операторы (`Enabled=False`) фильтр примет, но выдача будет пуста —
        # это выглядело бы как пропуск. Берём только включённых.
        enabled = [o for o in data if o.get("Enabled")]
        for item in enabled:
            if operator_matches(str(item.get("Name") or ""), operator):
                return _to_int(item.get("Id"))
        log.warning("Слетать (шлюз): оператор «%s» не найден среди включённых — "
                    "ищем без фильтра по ТО", operator)
        return None

    def _query(self, params: SearchParams, city_id: int, country_id: int,
               operator_id: int | None) -> dict[str, Any]:
        query: dict[str, Any] = {
            "cityFromId": city_id,
            "countryId": country_id,
            "s_departFrom": _fmt_date(params.date_from),
            "s_departTo": _fmt_date(params.date_to),
            "s_nightsMin": params.nights_min,
            "s_nightsMax": params.nights_max,
            "s_adults": params.adults,
            "s_kids": len(params.children_ages),
            "currencyAlias": params.currency,
            "pageSize": PAGE_SIZE,
            # Режим «Отели» = проживание без перелёта.
            "s_ticketsIncluded": "false" if params.search_mode == "hotels" else "true",
        }
        if params.children_ages:
            query["s_kids_ages"] = ",".join(str(a) for a in params.children_ages)
        if params.hotel_stars:
            query["stars"] = ",".join(str(s) for s in params.hotel_stars)
        if operator_id is not None:
            query |= {"filter": 1, "f_to_id": operator_id}
        return query

    async def _start_search(self, client: httpx.AsyncClient, params: SearchParams,
                            city_id: int, country_id: int, operator_id: int | None) -> int:
        data = await self._call(client, "GetTours",
                                **self._query(params, city_id, country_id, operator_id),
                                requestId=0)
        request_id = _to_int(data.get("requestId"))
        if not request_id:
            raise SletatApiError("GetTours не вернул requestId")
        return request_id

    async def _await_completion(self, client: httpx.AsyncClient, request_id: int) -> list[dict]:
        """Опрашивать состояние, пока все операторы не завершатся или не выйдет время.

        По таймауту возвращаем последнее состояние, а не падаем: часть операторов уже
        ответила, и это осмысленные данные. Незавершённые в находки не попадут —
        `split_load_state` относит их ни к «туров нет», ни к «не отвечает».
        """
        deadline = time.monotonic() + POLL_TIMEOUT_S
        states: list[dict] = []
        while time.monotonic() < deadline:
            data = await self._call(client, "GetLoadState", requestId=request_id)
            states = data if isinstance(data, list) else (data.get("Data") or [])
            if states and all(s.get("IsProcessed") or s.get("IsSkipped") for s in states):
                return states
            await asyncio.sleep(POLL_INTERVAL_S)
        log.warning("Слетать (шлюз): не все операторы завершились за %.0f с", POLL_TIMEOUT_S)
        return states

    async def _fetch_rows(self, client: httpx.AsyncClient, params: SearchParams,
                          city_id: int, country_id: int, operator_id: int | None,
                          request_id: int) -> tuple[list[list], bool]:
        """Собрать выдачу постранично. Второй элемент — упёрлись ли в лимит страниц."""
        base = self._query(params, city_id, country_id, operator_id)
        rows: list[list] = []
        for page in range(1, MAX_PAGES + 1):
            data = await self._call(client, "GetTours", **base, requestId=request_id,
                                    updateResult=1, pageNumber=page)
            chunk = data.get("aaData")
            if not isinstance(chunk, list) or not chunk:
                return rows, False
            rows += chunk
            if len(chunk) < PAGE_SIZE:      # последняя страница
                return rows, False
        log.warning("Слетать (шлюз): выдача обрезана на %d страницах по %d строк",
                    MAX_PAGES, PAGE_SIZE)
        return rows, True


def _fmt_date(value: date) -> str:
    """Шлюз ожидает ДД/ММ/ГГГГ."""
    return value.strftime("%d/%m/%Y")


def _find_by_name(items: Any, wanted: str) -> int | None:
    """ID записи справочника по имени: точное совпадение, затем вхождение.

    Матчинг терпимый, но не нечёткий: угадывать направление нельзя, честно не найти —
    лучше, чем подставить соседнюю страну и построить на ней отчёт.
    """
    if not isinstance(items, list):
        return None
    target = (wanted or "").strip().casefold()
    if not target:
        return None
    exact = [i for i in items if str(i.get("Name") or "").strip().casefold() == target]
    partial = [i for i in items if target in str(i.get("Name") or "").strip().casefold()]
    for candidate in (exact, partial):
        if candidate:
            return _to_int(candidate[0].get("Id"))
    return None
