"""Справочник отелей Слетать через шлюз.

`GetHotels?countryId=<id>&all=-1` отдаёт **полный каталог страны** (по Турции — свыше
одиннадцати тысяч записей) анонимно, одним запросом. Это принципиально шире, чем выдача
поиска: там видно только то, по чему сейчас есть туры, а здесь — всё, что вообще заведено.

Разница и даёт диагноз. Отель, которого нет в выдаче, может отсутствовать по двум разным
причинам: его нет в справочнике вовсе (тогда заводить отель) или он есть, но по нему нет
предложения (тогда смотреть линковку и наличие). Без каталога эти случаи неразличимы, и
в отчёте они сливались бы в бесполезное «отеля нет».
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal

import httpx

from pegasgap.models import HotelOffer

log = logging.getLogger("pegasgap.catalog")

BASE_URL = os.environ.get("SLETAT_API_URL") or "https://module.sletat.ru/Main.svc"
REFERER = "https://sletat.ru/"

# Каталог страны меняется редко, а нужен на каждый прогон. При обходе матрицы направлений
# это разница между одной загрузкой на страну и десятком одинаковых.
_CACHE: dict[int, list[CatalogHotel]] = {}


@dataclass(frozen=True)
class CatalogHotel:
    """Запись справочника отелей Слетать."""

    id: int
    name: str
    stars: int | None
    town_id: int | None

    def as_offer(self) -> HotelOffer:
        """Обернуть в HotelOffer, чтобы сопоставлять тем же матчером, что и выдачу.

        Отдельный алгоритм сравнения имён для каталога был бы прямой ошибкой: он бы
        разошёлся с основным, и один и тот же отель считался бы то тем же, то другим
        в зависимости от того, где его сравнивают.
        """
        return HotelOffer(provider="catalog", hotel_name=self.name,
                          price=Decimal(1), stars=self.stars, raw_label=str(self.id))


def _stars_from_name(value: str | None) -> int | None:
    """«3*» → 3. Нечисловые категории («Apts», «Без звезд») звёзд не имеют."""
    text = (value or "").strip()
    return int(text[0]) if text[:1].isdigit() else None


async def fetch_catalog(country_id: int, client: httpx.AsyncClient | None = None,
                        ) -> list[CatalogHotel]:
    """Полный справочник отелей страны. Пустой список = получить не удалось."""
    cached = _CACHE.get(country_id)
    if cached is not None:
        return cached

    owned = client is None
    client = client or httpx.AsyncClient(timeout=180, headers={"Referer": REFERER})
    try:
        response = await client.get(f"{BASE_URL}/GetHotels",
                                    params={"countryId": country_id, "all": -1})
        if response.status_code != 200:
            log.warning("справочник отелей: HTTP %s", response.status_code)
            return []
        result = response.json().get("GetHotelsResult") or {}
        if result.get("IsError"):
            log.warning("справочник отелей: %s", result.get("ErrorMessage"))
            return []
        rows = result.get("Data")
        if not isinstance(rows, list):
            return []
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("справочник отелей недоступен: %s: %s", type(exc).__name__, exc)
        return []
    finally:
        if owned:
            await client.aclose()

    hotels = [
        CatalogHotel(
            id=int(row["Id"]),
            name=str(row.get("Name") or "").strip(),
            stars=_stars_from_name(row.get("StarName")),
            town_id=row.get("TownId"),
        )
        for row in rows
        if row.get("Id") is not None and str(row.get("Name") or "").strip()
    ]
    log.info("справочник отелей страны %s: %d записей", country_id, len(hotels))
    _CACHE[country_id] = hotels
    return hotels


async def resolve_country_id(country: str, client: httpx.AsyncClient | None = None,
                             ) -> int | None:
    """Имя страны → её id в справочнике Слетать."""
    owned = client is None
    client = client or httpx.AsyncClient(timeout=60, headers={"Referer": REFERER})
    try:
        response = await client.get(f"{BASE_URL}/GetCountries")
        rows = ((response.json().get("GetCountriesResult") or {}).get("Data")) or []
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("справочник стран недоступен: %s: %s", type(exc).__name__, exc)
        return None
    finally:
        if owned:
            await client.aclose()

    target = (country or "").strip().casefold()
    exact = [r for r in rows if str(r.get("Name") or "").strip().casefold() == target]
    partial = [r for r in rows if target in str(r.get("Name") or "").strip().casefold()]
    for candidate in (exact, partial):
        if candidate:
            try:
                return int(candidate[0]["Id"])
            except (KeyError, TypeError, ValueError):
                return None
    return None
