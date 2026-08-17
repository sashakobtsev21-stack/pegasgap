"""Провайдер Слетать через JSON-шлюз поиска туров (`module.sletat.ru/Main.svc`).

Основной путь для проверяемой стороны. Он лучше браузерного не только скоростью:

* **Статус оператора приходит фактом, а не вычитывается из вёрстки.** `GetLoadState`
  отдаёт по каждому ТО `IsProcessed`, `RowsCount`, `IsError`, `IsTimeout` — то самое
  различие «отработал и вернул пусто» против «не ответил», ради которого всё затевалось.
  В браузерном пути это приходилось выуживать из подписей панели «блинчик».
* **Каждая строка выдачи несёт имя оператора.** Значит фильтр по ТО применяется
  достоверно, и главная угроза точности отчёта — «карточка отеля не привязана к
  оператору» — на этой стороне исчезает совсем.
* Нет вёрстки — нечему протухнуть. Браузерный провайдер уже сломался на редизайне формы.

Протокол: `GetTours(requestId=0)` создаёт поиск и возвращает `requestId` → опрос
`GetLoadState` до готовности всех операторов → `GetTours(requestId, updateResult=1)`
за результатом.

**Секреты.** Шлюз принимает логин и пароль GET-параметрами — это его контракт, не наш
выбор. Значения берутся только из окружения, в логи уходит URL с вырезанными кредами
(`_redact`), и нигде не печатаются целиком.
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

# Индексы значимых полей в строке `aaData`. Шлюз отдаёт массив почти на сотню позиций без
# имён, поэтому позиции вынесены в константы: иначе по коду расползутся числа, а при
# сдвиге формата отчёт молча наполнится мусором вместо явной ошибки.
IDX_OPERATOR_ID = 1
IDX_HOTEL_ID = 3
IDX_HOTEL_NAME = 7
IDX_STARS = 8
IDX_PRICE = 15
IDX_RESORT = 19
IDX_OPERATOR_NAME = 25
IDX_RATING = 35
_MIN_ROW_LEN = IDX_RATING + 1

# Шлюз пагинирует выдачу и по умолчанию отдаёт 20 строк. Для поиска пропусков этого
# категорически мало: недостающие отели оказались бы «пропущенными» просто потому, что не
# попали на первую страницу.
PAGE_SIZE = int(os.environ.get("PEGASGAP_API_PAGE_SIZE") or 1000)

POLL_INTERVAL_S = 1.5   # рекомендация документации шлюза
POLL_TIMEOUT_S = float(os.environ.get("PEGASGAP_API_POLL_TIMEOUT_S") or 120)

_SECRET_RE = re.compile(r"(login|password)=[^&]*", re.IGNORECASE)


def _redact(url: str) -> str:
    """URL без значений логина и пароля — единственная форма, пригодная для логов."""
    return _SECRET_RE.sub(r"\1=***", url)


class SletatApiError(RuntimeError):
    """Шлюз ответил ошибкой или неожиданной структурой."""


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_hotel_offers(rows: list[list], operator: str) -> list[HotelOffer]:
    """Строки `aaData` → предложения по отелям ТОЛЬКО указанного оператора, мин. цена на отель.

    Фильтрация здесь достоверна (в отличие от браузерного пути): имя оператора лежит в
    самой строке, поэтому «цена этого отеля у этого ТО» — факт, а не допущение.
    """
    best: dict[str, HotelOffer] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < _MIN_ROW_LEN:
            continue
        if not operator_matches(str(row[IDX_OPERATOR_NAME] or ""), operator):
            continue
        name = str(row[IDX_HOTEL_NAME] or "").strip()
        price = _to_decimal(row[IDX_PRICE])
        if not name or price is None or price <= 0:
            continue
        rating = _to_decimal(row[IDX_RATING])
        offer = HotelOffer(
            provider="sletat",
            hotel_name=name,
            price=price,
            stars=_to_int(row[IDX_STARS]),
            rating=float(rating) if rating is not None else None,
            destination=str(row[IDX_RESORT] or "").strip() or None,
            raw_label=str(row[IDX_HOTEL_ID] or ""),
        )
        seen = best.get(name)
        if seen is None or offer.price < seen.price:
            best[name] = offer
    return sorted(best.values(), key=lambda h: h.price)


def split_load_state(states: list[dict]) -> tuple[list[Offer], list[str], list[str]]:
    """`GetLoadState` → (офферы с ценой, «туров нет», «не отвечает»).

    Это прямая замена разбора панели «блинчик», только сведения приходят фактом:

    * `IsError` / `IsTimeout` — оператор не ответил (таймаут, бан, падение плагина);
    * `IsProcessed` и `RowsCount == 0` — отработал и честно вернул пусто;
    * `RowsCount > 0` — есть предложения.

    `IsSkipped` не относится ни к одному из случаев: оператор в этом поиске не опрашивался,
    и записывать ему пропуск было бы враньём.
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
            price = _to_decimal(state.get("MinPrice"))
            if price and price > 0:
                priced.append(Offer(provider="sletat", operator=name, price=price))
        elif state.get("IsProcessed"):
            no_tours.append(name)
    return priced, no_tours, not_responding


@register_provider("sletat_api")
class SletatApiProvider:
    """Поиск на Слетать через JSON-шлюз."""

    name = "sletat"  # роль в отчёте та же, что у браузерного провайдера

    def __init__(self, headless: bool = True, timeout_ms: int = 20_000,
                 login: str | None = None, password: str | None = None) -> None:
        # `headless` игнорируется — параметр в сигнатуре, чтобы провайдер оставался
        # взаимозаменяемым с браузерным по протоколу SearchProvider.
        self.timeout_ms = timeout_ms
        self._login = login or os.environ.get("SLETAT_LOGIN") or ""
        self._password = password or os.environ.get("SLETAT_PASSWORD") or ""
        self.on_frame = None

    @property
    def configured(self) -> bool:
        return bool(self._login and self._password)

    async def search(self, params: SearchParams) -> ProviderResult:
        start = time.monotonic()
        if not self.configured:
            return self._fail(params, start,
                              "Не заданы SLETAT_LOGIN и SLETAT_PASSWORD — шлюз недоступен")
        operator = params.operators[0] if params.operators else ""
        try:
            async with httpx.AsyncClient(timeout=self.timeout_ms / 1000) as client:
                city_id = await self._resolve_city(client, params.departure_city)
                country_id = await self._resolve_country(client, city_id,
                                                         params.destination_country)
                request_id = await self._start_search(client, params, city_id, country_id)
                states = await self._await_completion(client, request_id)
                rows = await self._fetch_rows(client, request_id)
        except NotApplicableError:
            raise
        except httpx.HTTPError as exc:
            return self._fail(params, start, f"Сеть/шлюз: {type(exc).__name__}: {exc}")
        except SletatApiError as exc:
            return self._fail(params, start, str(exc))

        dur = time.monotonic() - start
        priced, no_tours, not_responding = split_load_state(states)
        hotels = build_hotel_offers(rows, operator) if operator else []
        offers = [o for o in priced if not operator or operator_matches(o.operator, operator)]
        operator_offers = [
            OperatorOffer(provider=self.name, operator=o.operator, price=o.price,
                          hotel_name=hotels[0].hotel_name if hotels else None)
            for o in offers
        ]
        log.info("Слетать (шлюз): %d операторов в ответе, отелей у «%s»: %d, за %.1f с",
                 len(states), operator, len(hotels), dur)
        return ProviderResult(
            provider=self.name, success=True, duration_seconds=dur,
            search_mode=params.search_mode,
            offers=offers, hotel_offers=hotels, operator_offers=operator_offers,
            operators_no_tours=no_tours, operators_not_responding=not_responding,
            # Имя оператора приходит в каждой строке выдачи — фильтрация достоверна.
            operator_filter_verified=True,
            # Забираем страницу заведомо больше выдачи; если упёрлись — честно отмечаем.
            truncated=len(rows) >= PAGE_SIZE,
        )

    def _fail(self, params: SearchParams, start: float, error: str) -> ProviderResult:
        log.warning("Слетать (шлюз): %s", error)
        return ProviderResult(
            provider=self.name, success=False,
            duration_seconds=time.monotonic() - start,
            search_mode=params.search_mode, error=error,
        )

    # --- вызовы шлюза ---

    async def _call(self, client: httpx.AsyncClient, method: str, **query: Any) -> dict:
        """Вызвать метод шлюза и вернуть содержимое `<Method>Result.Data`."""
        payload = {"login": self._login, "password": self._password, **query}
        url = f"{BASE_URL}/{method}"
        response = await client.get(url, params=payload)
        log.debug("шлюз %s → %s", _redact(str(response.url)), response.status_code)
        if response.status_code != 200:
            raise SletatApiError(f"{method}: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SletatApiError(f"{method}: ответ не JSON") from exc
        result = body.get(f"{method}Result") or {}
        if result.get("Error"):
            raise SletatApiError(f"{method}: {result['Error']}")
        data = result.get("Data")
        if data is None:
            raise SletatApiError(f"{method}: в ответе нет Data")
        return data

    async def _resolve_city(self, client: httpx.AsyncClient, city: str) -> int:
        data = await self._call(client, "GetDepartCities")
        found = _find_by_name(data, city)
        if found is None:
            raise NotApplicableError(f"город вылета «{city}» не найден в справочнике Слетать")
        return found

    async def _resolve_country(self, client: httpx.AsyncClient, city_id: int, country: str) -> int:
        data = await self._call(client, "GetCountries", cityFromId=city_id)
        found = _find_by_name(data, country)
        if found is None:
            # Детерминированный отказ: страна не обслуживается из этого города. Не сбой и
            # не пропуск — сравнивать по такому запросу нечего.
            raise NotApplicableError(
                f"направление «{country}» недоступно из города «{city_id}» на Слетать")
        return found

    async def _start_search(self, client: httpx.AsyncClient, params: SearchParams,
                            city_id: int, country_id: int) -> int:
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
            "requestId": 0,
            "pageSize": PAGE_SIZE,
            # Режим «Отели» = проживание без перелёта.
            "s_ticketsIncluded": "false" if params.search_mode == "hotels" else "true",
        }
        if params.children_ages:
            query["s_kids_ages"] = ",".join(str(a) for a in params.children_ages)
        if params.hotel_stars:
            query["stars"] = ",".join(str(s) for s in params.hotel_stars)
        data = await self._call(client, "GetTours", **query)
        request_id = _to_int(data.get("requestId"))
        if not request_id:
            raise SletatApiError("GetTours не вернул requestId")
        return request_id

    async def _await_completion(self, client: httpx.AsyncClient, request_id: int) -> list[dict]:
        """Опрашивать состояние, пока все операторы не завершатся или не выйдет время.

        По таймауту возвращаем последнее состояние, а не падаем: часть операторов уже
        ответила, и это осмысленные данные. Незавершённые попадут в «не отвечает» —
        что и произошло на самом деле.
        """
        deadline = time.monotonic() + POLL_TIMEOUT_S
        states: list[dict] = []
        while time.monotonic() < deadline:
            data = await self._call(client, "GetLoadState", requestId=request_id)
            states = data if isinstance(data, list) else data.get("Data") or []
            if states and all(s.get("IsProcessed") or s.get("IsSkipped") for s in states):
                return states
            await asyncio.sleep(POLL_INTERVAL_S)
        log.warning("Слетать (шлюз): не все операторы завершились за %.0f с", POLL_TIMEOUT_S)
        return states

    async def _fetch_rows(self, client: httpx.AsyncClient, request_id: int) -> list[list]:
        data = await self._call(client, "GetTours", requestId=request_id,
                                updateResult=1, pageSize=PAGE_SIZE)
        rows = data.get("aaData")
        return rows if isinstance(rows, list) else []


def _fmt_date(value: date) -> str:
    """Шлюз ожидает ДД/ММ/ГГГГ."""
    return value.strftime("%d/%m/%Y")


def _find_by_name(items: Any, wanted: str) -> int | None:
    """ID записи справочника по имени: точное совпадение, затем вхождение.

    Справочники шлюза приходят списком словарей с `Id`/`Name`; регистр и хвосты вроде
    «(Турция)» на разных методах отличаются, поэтому матчинг терпимый, но не нечёткий:
    угадывать направление нельзя, лучше честно не найти.
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
