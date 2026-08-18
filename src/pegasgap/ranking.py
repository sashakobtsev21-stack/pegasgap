"""Ранжирование направлений по фактическому объёму оператора.

Отвечает на вопрос «какие направления вообще стоит мониторить». Обходить всё подряд
бессмысленно: у оператора из сотни стран активны единицы, а объём между ними различается
на порядки — и пропуск на направлении, где у него полторы сотни предложений, стоит совсем
не столько же, сколько на направлении с девятью тысячами.

**Почему меряем, а не берём флаг популярности площадки.** Флаги есть у обеих витрин, но
они про рынок в целом, а не про оператора: у Турвизора в «популярных» лежат Китай и Куба,
где у Pegas ровно ноль предложений. Единственный честный критерий здесь — сколько у
оператора реально есть на этом направлении.

**Почему это дёшево.** Объём виден в `GetLoadState` сразу после того, как операторы
отработали, — саму выдачу тянуть не нужно. Замер одного направления занимает секунды.

**Почему по странам, а не по парам «город → страна».** Пара была бы точнее, но шлюз
`cityFromId` не применяет: для Москвы, Петербурга, Казани и Тюмени он возвращает
побайтово одинаковые выдачу, цены и счётчики. Ранжировать по неразличимому признаку
нельзя, а делать вид, что различаем, — значит выдавать один и тот же замер за четыре
разных. Пока шлюз таков, направление здесь — это страна.

Ранжирование НЕ применяется автоматически: `pegasgap top` печатает готовый блок для
`scenarios.yaml`, а решение, что мониторить, остаётся за человеком и видно в истории
изменений. Список направлений, который меняется сам, — плохое свойство для мониторинга:
направление может тихо выпасть из наблюдения, и никто этого не заметит.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from pegasgap.providers.sletat_api import BASE_URL, REFERER, _find_by_name, _to_int

log = logging.getLogger("pegasgap.ranking")

# Одно направление обычно отвечает за несколько секунд, но встречаются и такие, что не
# укладываются в минуту (живой пример — Таиланд). Ждать их дольше незачем: если поиск
# настолько медленный, обход по нему будет мучительным, и это само по себе аргумент
# против включения направления в регулярный список.
PROBE_TIMEOUT_S = float(os.environ.get("PEGASGAP_PROBE_TIMEOUT_S") or 45)
POLL_INTERVAL_S = 1.5

# Шлюз считает поисковые запросы по IP и при превышении отвечает мгновенным отказом
# («превышен лимит кол-ва поисковых запросов»). При восьми параллельных зондах хвост
# списка стабильно упирался в квоту, хотя поодиночке те же направления меряются нормально.
# Гнаться тут не за чем: замер и на трёх потоках занимает минуты, а не часы.
CONCURRENCY = int(os.environ.get("PEGASGAP_PROBE_CONCURRENCY") or 3)
# Отказ по частоте — состояние временное, поэтому повторяем с паузой. Без этого одно
# направление молча выпадало бы из ранжирования и выглядело как «у оператора там пусто».
PROBE_RETRIES = int(os.environ.get("PEGASGAP_PROBE_RETRIES") or 3)
RETRY_PAUSE_S = 4.0

# Шлюз считает поиски по IP и при превышении отвечает мгновенным отказом. Это не наша
# ошибка и не свойство направления, а квота — ждать надо ощутимо дольше обычного отказа,
# иначе повторы только сжигают остаток лимита.
RATE_LIMIT_PAUSE_S = 20.0
_RATE_LIMIT_MARK = "превышен лимит"


def is_rate_limited(error: str) -> bool:
    """Отказ по квоте поисков, а не поломка направления."""
    return _RATE_LIMIT_MARK in (error or "").lower()


@dataclass(frozen=True)
class RouteVolume:
    """Объём оператора на одном направлении.

    Город вылета хранится, хотя шлюз его и не различает: он попадает в готовый конфиг,
    и подменять его на «какой-то» было бы хуже, чем честно возить известное значение.
    """

    departure_city: str
    country: str
    rows: int | None          # None = замерить не удалось (таймаут или отказ)
    seconds: float = 0.0
    error: str = ""           # чем именно ответил шлюз, если не получилось

    @property
    def measured(self) -> bool:
        return self.rows is not None

    @property
    def has_volume(self) -> bool:
        return bool(self.rows)


class VolumeProbe:
    """Замер объёма оператора. Один экземпляр на прогон — держит справочники в памяти."""

    def __init__(self, operator_id: int, nights: int = 7, adults: int = 2,
                 offset_days: int = 30) -> None:
        self.operator_id = operator_id
        self.nights = nights
        self.adults = adults
        self.offset_days = offset_days
        self._cities: list[dict] = []
        self._countries: list[dict] = []

    async def load_refdata(self, client: httpx.AsyncClient) -> None:
        self._cities = await self._data(client, "GetDepartCities")
        self._countries = await self._data(client, "GetCountries")

    async def _data(self, client: httpx.AsyncClient, method: str, **query) -> list[dict]:
        response = await client.get(f"{BASE_URL}/{method}", params=query)
        result = response.json().get(f"{method}Result") or {}
        data = result.get("Data")
        return data if isinstance(data, list) else []

    def city_id(self, name: str) -> int | None:
        return _find_by_name(self._cities, name)

    def country_id(self, name: str) -> int | None:
        return _find_by_name(self._countries, name)

    @property
    def city_names(self) -> list[str]:
        return [str(c["Name"]).strip() for c in self._cities if c.get("Name")]

    async def active_countries(self, client: httpx.AsyncClient) -> list[str]:
        """Страны, где оператор включён.

        Признак берётся из справочника операторов страны и совпадает с замером: там, где
        `Enabled` ложно, объём ровно нулевой. Сотня мелких запросов уходит за считаные
        секунды и отсекает четыре пятых работы следующего шага.
        """
        sem = asyncio.Semaphore(CONCURRENCY * 2)

        async def one(country: dict) -> str | None:
            async with sem:
                try:
                    data = await self._data(client, "GetTourOperators",
                                            countryId=country["Id"])
                except (httpx.HTTPError, ValueError):
                    return None
            active = any(_to_int(o.get("Id")) == self.operator_id and o.get("Enabled")
                         for o in data)
            return str(country["Name"]).strip() if active else None

        found = await asyncio.gather(*(one(c) for c in self._countries))
        return sorted(n for n in found if n)

    async def probe(self, client: httpx.AsyncClient, city: str, country: str) -> RouteVolume:
        """Объём оператора на направлении, с повтором при отказе по частоте."""
        attempt = 0
        while True:
            volume = await self._probe_once(client, city, country)
            if volume.measured or attempt >= PROBE_RETRIES:
                return volume
            attempt += 1
            pause = (RATE_LIMIT_PAUSE_S if is_rate_limited(volume.error)
                     else RETRY_PAUSE_S * attempt)
            log.info("замер «%s» не удался (%s) — повтор %d через %.0f с",
                     country, volume.error, attempt, pause)
            await asyncio.sleep(pause)

    async def _probe_once(self, client: httpx.AsyncClient, city: str,
                          country: str) -> RouteVolume:
        """Одна попытка. Выдачу не тянем — хватает состояния поиска."""
        start = time.monotonic()
        city_id, country_id = self.city_id(city), self.country_id(country)
        if city_id is None or country_id is None:
            return RouteVolume(city, country, None, 0.0, "нет в справочнике")

        depart = date.today() + timedelta(days=self.offset_days)
        query = {
            "cityFromId": city_id, "countryId": country_id,
            "s_departFrom": depart.strftime("%d/%m/%Y"),
            "s_departTo": (depart + timedelta(days=self.nights)).strftime("%d/%m/%Y"),
            "s_nightsMin": self.nights, "s_nightsMax": self.nights,
            "s_adults": self.adults, "currencyAlias": "RUB",
            "filter": 1, "f_to_id": self.operator_id,
            "pageSize": 1,          # строки не нужны, важен только счётчик в состоянии
        }
        try:
            response = await client.get(f"{BASE_URL}/GetTours",
                                        params={**query, "requestId": 0})
            result = response.json().get("GetToursResult") or {}
            if result.get("IsError"):
                return RouteVolume(city, country, None, time.monotonic() - start,
                                   str(result.get("ErrorMessage") or "отказ шлюза")[:120])
            request_id = _to_int((result.get("Data") or {}).get("requestId"))
            if not request_id:
                return RouteVolume(city, country, None, time.monotonic() - start,
                                   "шлюз не вернул requestId")

            deadline = time.monotonic() + PROBE_TIMEOUT_S
            while time.monotonic() < deadline:
                await asyncio.sleep(POLL_INTERVAL_S)
                states = await self._data(client, "GetLoadState", requestId=request_id)
                if states and all(s.get("IsProcessed") or s.get("IsSkipped") for s in states):
                    rows = sum(_to_int(s.get("RowsCount")) or 0 for s in states)
                    return RouteVolume(city, country, rows, time.monotonic() - start)
        except (httpx.HTTPError, ValueError) as exc:
            return RouteVolume(city, country, None, time.monotonic() - start,
                               f"{type(exc).__name__}: {exc}"[:120])
        # Не уложились в отведённое время — направление слишком медленное.
        return RouteVolume(city, country, None, time.monotonic() - start,
                           f"поиск не завершился за {PROBE_TIMEOUT_S:.0f} с")

    async def rank(self, client: httpx.AsyncClient,
                   routes: list[tuple[str, str]]) -> list[RouteVolume]:
        """Замерить набор направлений параллельно и отсортировать по убыванию объёма.

        Неизмеренные уходят в конец, а не выбрасываются: «мы не смогли посмотреть» — это
        не то же самое, что «там пусто», и решать по ним должен человек.
        """
        sem = asyncio.Semaphore(CONCURRENCY)

        async def one(pair: tuple[str, str]) -> RouteVolume:
            async with sem:
                return await self.probe(client, *pair)

        measured = await asyncio.gather(*(one(p) for p in routes))
        return sorted(measured, key=lambda v: (v.rows is None, -(v.rows or 0)))


def client_factory(timeout: float = 180) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, headers={"Referer": REFERER})


def to_yaml_routes(routes: list[RouteVolume]) -> str:
    """Готовый блок `routes:` для scenarios.yaml — чтобы результат можно было вставить."""
    lines = ["routes:"]
    for route in routes:
        lines.append(f"  - {{from: {route.departure_city}, country: {route.country}}}"
                     f"    # предложений у оператора: {route.rows}")
    return "\n".join(lines)
