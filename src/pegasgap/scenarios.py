"""Матрица регулярного обхода: файл сценариев → список запросов.

Окна дат задаются **смещением от дня запуска**, а не абсолютными датами. Иначе
конфиг протухал бы: через месяц регулярный обход искал бы туры в прошлом и стабильно
рапортовал, что предложений нет ни там, ни там.
"""

from __future__ import annotations

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
class Matrix:
    """Разобранный файл сценариев."""

    operator: str = PEGAS
    departure_cities: list[str] = field(default_factory=lambda: ["Москва"])
    countries: list[str] = field(default_factory=list)
    modes: list[str] = field(default_factory=lambda: ["tours"])
    windows: list[Window] = field(default_factory=list)
    adults: int = 2
    nights_min: int = 7
    nights_max: int = 7

    def build(self, today: date | None = None) -> list[SearchParams]:
        """Развернуть матрицу в конкретные запросы."""
        today = today or date.today()
        out: list[SearchParams] = []
        for city in self.departure_cities:
            for country in self.countries:
                for mode in self.modes:
                    for window in self.windows:
                        date_from, date_to = window.dates(today)
                        out.append(SearchParams(
                            departure_city=city,
                            destination_country=country,
                            date_from=date_from,
                            date_to=date_to,
                            nights_min=self.nights_min,
                            nights_max=self.nights_max,
                            adults=self.adults,
                            search_mode=mode,  # type: ignore[arg-type]
                            operators=[self.operator],
                        ))
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
    matrix = Matrix(
        operator=raw.get("operator") or PEGAS,
        departure_cities=list(defaults.get("departure_cities") or ["Москва"]),
        countries=list(raw.get("countries") or []),
        modes=list(defaults.get("modes") or ["tours"]),
        adults=int(defaults.get("adults") or 2),
        nights_min=int(defaults.get("nights_min") or 7),
        nights_max=int(defaults.get("nights_max") or defaults.get("nights_min") or 7),
        windows=[Window(offset_days=int(w["offset_days"]),
                        length_days=int(w.get("length_days") or 7))
                 for w in (raw.get("windows") or [])],
    )
    if not matrix.countries:
        raise ValueError(f"В {path} не задано ни одной страны (ключ `countries`)")
    if not matrix.windows:
        raise ValueError(f"В {path} не задано ни одного окна дат (ключ `windows`)")
    bad = [m for m in matrix.modes if m not in ("tours", "hotels")]
    if bad:
        raise ValueError(f"Недопустимые режимы в {path}: {bad}; разрешены tours, hotels")
    return matrix
