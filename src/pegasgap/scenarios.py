"""Матрица регулярного обхода: файл сценариев → список запросов.

Окна дат задаются **смещением от дня запуска**, а не абсолютными датами. Иначе
конфиг протухал бы: через месяц регулярный обход искал бы туры в прошлом и стабильно
рапортовал, что предложений нет ни там, ни там.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import yaml

from pegasgap.models import PEGAS, SearchParams

DEFAULT_CONFIG = Path("scenarios.yaml")


@dataclass
class Window:
    """Окно вылета: через сколько дней от запуска и какой длины."""

    offset_days: int
    length_days: int = 7

    def dates(self, today: date) -> tuple[date, date]:
        start = today + timedelta(days=self.offset_days)
        return start, start + timedelta(days=self.length_days)


@dataclass
class Nights:
    """Длительность одного кейса: сколько ночей искать."""

    minimum: int = 7
    maximum: int = 7

    @property
    def label(self) -> str:
        return f"{self.minimum}" if self.minimum == self.maximum \
            else f"{self.minimum}-{self.maximum}"


@dataclass
class Pax:
    """Состав туристов одного кейса."""

    adults: int = 2
    children: list[int] = field(default_factory=list)

    @property
    def label(self) -> str:
        kids = f" + {len(self.children)} реб." if self.children else ""
        return f"{self.adults} взр.{kids}"


@dataclass
class Matrix:
    """Разобранный файл сценариев."""

    # Операторы. Измерение кейса: один и тот же поиск по Pegas и по Coral даёт разную
    # выдачу и разные пропуски. Единственный оператор в конфиге читается по-прежнему.
    operators: list[str] = field(default_factory=lambda: [PEGAS])
    departure_cities: list[str] = field(default_factory=lambda: ["Москва"])
    countries: list[str] = field(default_factory=list)
    # Явные пары «откуда → куда». Заданы — перекрёстное произведение не используется.
    # Нужны, потому что объём оператора живёт именно на паре: из Москвы в Турцию его
    # тысячи, из того же города в соседнюю страну может не быть вовсе, и перемножать
    # города на страны значит гарантированно намолотить пустых сценариев.
    routes: list[tuple[str, str]] = field(default_factory=list)
    modes: list[str] = field(default_factory=lambda: ["tours"])
    windows: list[Window] = field(default_factory=list)
    # Составы туристов. Это измерение кейса, а не глобальная настройка: «двое взрослых»
    # и «трое взрослых с ребёнком двенадцати лет» — разные поиски с разной выдачей, и
    # находка по одному ничего не говорит о другом.
    pax: list[Pax] = field(default_factory=lambda: [Pax()])
    # Длительности. Тоже измерение кейса, а не одно число на весь обход: оператор часто
    # отваливается не «по стране», а на конкретной длительности — неделя есть, десять
    # ночей уже нет. На единственной длительности такой пропуск невидим.
    durations: list[Nights] = field(default_factory=lambda: [Nights()])
    adults: int = 2

    def pairs(self) -> list[tuple[str, str]]:
        """Направления как список пар — из `routes` либо перекрёстным произведением."""
        if self.routes:
            return list(self.routes)
        return [(city, country)
                for city in self.departure_cities
                for country in self.countries]

    def build(self, today: date | None = None) -> list[SearchParams]:
        """Развернуть матрицу в конкретные запросы."""
        today = today or date.today()
        combos = itertools.product(self.operators, self.pairs(), self.modes,
                                   self.windows, self.durations, self.pax)
        out: list[SearchParams] = []
        seen: set[tuple] = set()
        for operator, (city, country), mode, window, nights, pax in combos:
            # «Без перелёта» — проживание, и город вылета там не значит ничего: витрина
            # подставляет псевдогород «Без перелета» независимо от того, что мы просили.
            # Десять городов дали бы десять побайтово одинаковых поисков и десять копий
            # одной находки, поэтому в этом режиме город схлопывается в один.
            if mode == "hotels":
                city = self.departure_cities[0] if self.departure_cities else city
            date_from, date_to = window.dates(today)
            fingerprint = (operator, city, country, mode, window.offset_days,
                           nights.minimum, nights.maximum, pax.adults,
                           tuple(pax.children))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            out.append(SearchParams(
                departure_city=city,
                destination_country=country,
                date_from=date_from,
                date_to=date_to,
                nights_min=nights.minimum,
                nights_max=nights.maximum,
                adults=pax.adults,
                children_ages=list(pax.children),
                search_mode=mode,  # type: ignore[arg-type]
                operators=[operator],
            ))
        return out


def _read_operators(raw: dict) -> list[str]:
    """Операторы из конфига: список `operators` либо одиночный `operator`.

    Одиночный ключ читается по-прежнему — по нему написан прежний конфиг, и ломать его
    ради нового измерения незачем.
    """
    many = raw.get("operators")
    if many:
        return [str(o).strip() for o in many if str(o).strip()]
    return [str(raw.get("operator") or PEGAS).strip()]


def _read_durations(defaults: dict, path: Path) -> list[Nights]:
    """Длительности из конфига: список `nights` либо старая пара nights_min/nights_max.

    Пару оставляем читаемой намеренно — по ней написаны и конфиги, и примеры в README,
    и молча переставать её понимать значит сломать обход у того, кто просто не обновил
    файл. Список, если он задан, побеждает.
    """
    raw = defaults.get("nights")
    if raw is None:
        return [Nights(minimum=int(defaults.get("nights_min") or 7),
                       maximum=int(defaults.get("nights_max")
                                   or defaults.get("nights_min") or 7))]
    if not isinstance(raw, list):
        raise ValueError(f"В {path} ключ `nights` должен быть списком длительностей")

    out: list[Nights] = []
    for item in raw:
        # Число — фиксированная длительность, пара — диапазон.
        if isinstance(item, dict):
            low = int(item.get("min") or item.get("nights_min") or 7)
            high = int(item.get("max") or item.get("nights_max") or low)
        else:
            low = high = int(item)
        if high < low:
            raise ValueError(f"В {path} длительность задом наперёд: {low}-{high}")
        out.append(Nights(minimum=low, maximum=high))
    if not out:
        raise ValueError(f"В {path} список `nights` пуст")
    return out


def load_matrix(path: Path | str = DEFAULT_CONFIG) -> Matrix:
    """Прочитать файл сценариев."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Файл сценариев не найден: {path}. Скопируйте scenarios.yaml из репозитория "
            f"или укажите путь через --config.")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}
    routes: list[tuple[str, str]] = []
    for item in raw.get("routes") or []:
        city, country = (item or {}).get("from"), (item or {}).get("country")
        if not city or not country:
            raise ValueError(f"В {path} маршрут без `from` или `country`: {item!r}")
        routes.append((str(city).strip(), str(country).strip()))
    matrix = Matrix(
        operators=_read_operators(raw),
        departure_cities=list(defaults.get("departure_cities") or ["Москва"]),
        countries=list(raw.get("countries") or []),
        routes=routes,
        modes=list(defaults.get("modes") or ["tours"]),
        pax=[Pax(adults=int(p.get("adults") or 2),
                 children=[int(a) for a in (p.get("children") or [])])
             for p in (defaults.get("pax") or [{}])],
        adults=int(defaults.get("adults") or 2),
        durations=_read_durations(defaults, path),
        windows=[Window(offset_days=int(w["offset_days"]),
                        length_days=int(w.get("length_days") or 7))
                 for w in (raw.get("windows") or [])],
    )
    if not matrix.pairs():
        raise ValueError(
            f"В {path} не задано ни одного направления: нужен либо список `countries`, "
            f"либо явные пары `routes`")
    if not matrix.windows:
        raise ValueError(f"В {path} не задано ни одного окна дат (ключ `windows`)")
    bad = [m for m in matrix.modes if m not in ("tours", "hotels")]
    if bad:
        raise ValueError(f"Недопустимые режимы в {path}: {bad}; разрешены tours, hotels")
    return matrix
