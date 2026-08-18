"""Очередь кейсов: что проверять и в каком порядке.

Разница с матрицей принципиальная. Матрица разворачивается заново на каждый запуск и
ничего не помнит: прогнали десять сценариев, завтра прогнали те же десять. Очередь живёт
между запусками — знает, что уже проверено и когда, и поэтому поверх неё можно поставить
воркер, который работает круглосуточно и сам решает, за что взяться следующим.

**Кейс** — это конкретная проверка целиком: откуда, куда, в какие даты, каким составом
туристов и в каком режиме. Состав входит в кейс, а не в глобальные настройки: «двое
взрослых» и «трое взрослых с ребёнком двенадцати лет» — это разные поиски с разной
выдачей, и находка по одному ничего не говорит о другом.

**Порядок** — по приоритету, а внутри него по давности проверки. Приоритет берётся из
объёма оператора на направлении (см. `ranking`): пропуск там, где у него девять тысяч
предложений, стоит дороже, чем там, где полторы сотни. Давность не даёт очереди
залипнуть на верхних кейсах: раз проверенный уходит в конец своей группы.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from pegasgap.models import PEGAS, SearchParams

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key        TEXT    NOT NULL UNIQUE,
    operator        TEXT    NOT NULL DEFAULT 'Pegas Touristik',
    departure_city  TEXT    NOT NULL,
    country         TEXT    NOT NULL,
    search_mode     TEXT    NOT NULL,
    date_from       TEXT    NOT NULL,
    date_to         TEXT    NOT NULL,
    nights          INTEGER NOT NULL,
    adults          INTEGER NOT NULL,
    children_ages   TEXT    NOT NULL DEFAULT '[]',
    priority        INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_checked    TEXT,
    last_run_id     INTEGER,
    checks          INTEGER NOT NULL DEFAULT 0,
    gaps_found      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cases_order
    ON cases(enabled, priority DESC, last_checked);
"""


@dataclass(frozen=True)
class Case:
    """Строка очереди."""

    id: int
    operator: str
    departure_city: str
    country: str
    search_mode: str
    date_from: date
    date_to: date
    nights: int
    adults: int
    children_ages: list[int]
    priority: int
    last_checked: datetime | None
    checks: int
    gaps_found: int

    def to_params(self, operator: str | None = None) -> SearchParams:
        """Параметры поиска. Оператор берётся из самого кейса — он его измерение.

        Аргумент оставлен для принудительной подмены (разовая сверка одного направления
        по другому ТО), но по умолчанию не нужен и не используется.
        """
        return SearchParams(
            departure_city=self.departure_city,
            destination_country=self.country,
            date_from=self.date_from, date_to=self.date_to,
            nights_min=self.nights, nights_max=self.nights,
            adults=self.adults, children_ages=list(self.children_ages),
            search_mode=self.search_mode,  # type: ignore[arg-type]
            operators=[operator or self.operator],
        )

    @property
    def title(self) -> str:
        """Человекочитаемое описание кейса — то, что видно в логе и в отчёте."""
        kids = (f" + {len(self.children_ages)} реб. "
                f"({', '.join(str(a) for a in self.children_ages)})"
                if self.children_ages else "")
        mode = "отели" if self.search_mode == "hotels" else "туры"
        return (f"{self.operator}: {self.departure_city} → {self.country}, "
                f"{self.date_from:%d.%m.%Y}–{self.date_to:%d.%m.%Y}, "
                f"{self.nights} ноч., {self.adults} взр.{kids}, {mode}")


def case_key(departure_city: str, country: str, mode: str, date_from: date, date_to: date,
             nights: int, adults: int, children_ages: list[int],
             operator: str = PEGAS) -> str:
    """Стабильный ключ кейса.

    Даты входят в ключ как есть: кейс на конкретное окно — это конкретная проверка, и
    завтрашнее окно с тем же смещением от «сегодня» будет уже другим кейсом.

    Оператор — тоже часть ключа: один и тот же поиск по Pegas и по Coral даёт разную
    выдачу и разные пропуски, и схлопывать их в один кейс нельзя. Он идёт ПОСЛЕДНИМ и со
    значением по умолчанию, чтобы ключи прежних, ещё однооператорных кейсов совпали и
    очередь не потеряла историю проверок.
    """
    kids = ",".join(str(a) for a in sorted(children_ages))
    base = (f"{mode}|{departure_city}|{country}|{date_from:%Y-%m-%d}..{date_to:%Y-%m-%d}"
            f"|{nights}|{adults}+{kids}")
    return base if operator == PEGAS else f"{base}|{operator}"


def _row_to_case(row: sqlite3.Row) -> Case:
    return Case(
        id=row["id"],
        operator=row["operator"],
        departure_city=row["departure_city"],
        country=row["country"],
        search_mode=row["search_mode"],
        date_from=date.fromisoformat(row["date_from"]),
        date_to=date.fromisoformat(row["date_to"]),
        nights=row["nights"],
        adults=row["adults"],
        children_ages=json.loads(row["children_ages"] or "[]"),
        priority=row["priority"],
        last_checked=(datetime.fromisoformat(row["last_checked"])
                      if row["last_checked"] else None),
        checks=row["checks"],
        gaps_found=row["gaps_found"],
    )


def add_case(conn: sqlite3.Connection, *, departure_city: str, country: str,
             search_mode: str, date_from: date, date_to: date, nights: int,
             adults: int = 2, children_ages: list[int] | None = None,
             priority: int = 0, operator: str = PEGAS) -> int:
    """Добавить кейс. Существующий не дублируется, но приоритет обновляется.

    Обновляем именно приоритет: объёмы у оператора меняются, и повторный посев не должен
    оставлять очередь с прошлогодним порядком. А вот историю проверок (`last_checked`,
    `checks`) трогать нельзя — иначе каждый посев обнулял бы память очереди.
    """
    children_ages = children_ages or []
    key = case_key(departure_city, country, search_mode, date_from, date_to,
                   nights, adults, children_ages, operator)
    cur = conn.execute(
        """INSERT INTO cases (case_key, operator, departure_city, country, search_mode,
                              date_from, date_to, nights, adults, children_ages, priority)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(case_key) DO UPDATE SET
               priority = excluded.priority,
               enabled = 1""",
        (key, operator, departure_city, country, search_mode, date_from.isoformat(),
         date_to.isoformat(), nights, adults,
         json.dumps(children_ages), priority),
    )
    if cur.lastrowid:
        return int(cur.lastrowid)
    row = conn.execute("SELECT id FROM cases WHERE case_key = ?", (key,)).fetchone()
    return int(row["id"])


def next_case(conn: sqlite3.Connection, min_age_hours: float = 0.0) -> Case | None:
    """Следующий кейс: сначала ни разу не проверенные, потом самые давние.

    `min_age_hours` защищает от бессмысленного перепрохода: если весь список уже
    проверен час назад, гонять его заново незачем — цены столько не меняются, а квота
    на поиски конечна.
    """
    cutoff = (datetime.now() - timedelta(hours=min_age_hours)).isoformat(timespec="seconds")
    row = conn.execute(
        """SELECT * FROM cases
            WHERE enabled = 1 AND (last_checked IS NULL OR last_checked <= ?)
            ORDER BY (last_checked IS NULL) DESC, priority DESC, last_checked ASC
            LIMIT 1""",
        (cutoff,),
    ).fetchone()
    return _row_to_case(row) if row else None


def mark_checked(conn: sqlite3.Connection, case_id: int, run_id: int | None,
                 gaps: int, when: datetime | None = None) -> None:
    """Отметить кейс проверенным и подвинуть его в конец очереди."""
    conn.execute(
        """UPDATE cases
              SET last_checked = ?, last_run_id = ?,
                  checks = checks + 1, gaps_found = gaps_found + ?
            WHERE id = ?""",
        ((when or datetime.now()).isoformat(timespec="seconds"), run_id, gaps, case_id),
    )


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Сводка по очереди — то, что показывают счётчики сверху."""
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN last_checked IS NOT NULL THEN 1 ELSE 0 END) AS checked,
                  SUM(checks) AS runs,
                  SUM(gaps_found) AS gaps
             FROM cases WHERE enabled = 1"""
    ).fetchone()
    return {
        "total": row["total"] or 0,
        "checked": row["checked"] or 0,
        "pending": (row["total"] or 0) - (row["checked"] or 0),
        "runs": row["runs"] or 0,
        "gaps": row["gaps"] or 0,
    }


def list_cases(conn: sqlite3.Connection, limit: int = 200) -> list[Case]:
    rows = conn.execute(
        """SELECT * FROM cases WHERE enabled = 1
            ORDER BY (last_checked IS NULL) DESC, priority DESC, last_checked ASC
            LIMIT ?""",
        (limit,),
    ).fetchall()
    return [_row_to_case(r) for r in rows]


def clear(conn: sqlite3.Connection) -> int:
    """Очистить очередь. Возвращает, сколько кейсов удалено."""
    cur = conn.execute("DELETE FROM cases")
    return cur.rowcount or 0


def seed_from_matrix(conn: sqlite3.Connection, matrix,
                    today: date | None = None) -> tuple[int, int]:
    """Привести очередь в соответствие конфигу. Возвращает (актуальных, отключённых).

    Именно ПРИВЕСТИ, а не дополнить. Первая версия только добавляла, и очередь копила
    мёртвые кейсы: направления, убранные из конфига, продолжали проверяться и жечь квоту
    поисков. Живой пример — ОАЭ, Кипр и Индонезия остались в работе после того, как их
    исключили за отсутствием оператора на эталоне.

    Лишние именно ОТКЛЮЧАЮТСЯ, а не удаляются: с ними связана история находок, и стирать
    её из-за правки конфига нельзя. Вернут направление обратно — вернётся и его прошлое.

    Приоритет берётся из ПОРЯДКА маршрутов: `pegasgap top` выписывает их по убыванию
    объёма оператора, и полагаться на порядок дешевле, чем хранить объёмы отдельной
    таблицей и следить за их свежестью. Правишь порядок руками — меняешь приоритет,
    и это видно в диффе конфига.
    """
    routes = matrix.pairs()
    weight = {pair: len(routes) - i for i, pair in enumerate(routes)}
    wanted: list[str] = []
    for params in matrix.build(today):
        operator = params.operators[0] if params.operators else PEGAS
        wanted.append(case_key(
            params.departure_city, params.destination_country, params.search_mode,
            params.date_from, params.date_to, params.nights_min, params.adults,
            list(params.children_ages), operator))
        add_case(
            conn,
            operator=operator,
            departure_city=params.departure_city,
            country=params.destination_country,
            search_mode=params.search_mode,
            date_from=params.date_from,
            date_to=params.date_to,
            nights=params.nights_min,
            adults=params.adults,
            children_ages=list(params.children_ages),
            priority=weight.get((params.departure_city, params.destination_country), 0),
        )
    placeholders = ",".join("?" * len(wanted)) or "NULL"
    cur = conn.execute(
        f"UPDATE cases SET enabled = 0 WHERE enabled = 1 AND case_key NOT IN ({placeholders})",
        wanted)
    return len(wanted), cur.rowcount or 0


def dimensions(conn: sqlite3.Connection) -> dict:
    """Из чего сложена очередь — по самой очереди, а не по конфигу.

    Считаем по засеянному, потому что важно то, что реально проверяется: конфиг могли
    поправить и не пересобрать очередь, и тогда его числа рассказывали бы о другом.
    """
    row = conn.execute(
        """SELECT COUNT(DISTINCT operator)                    AS operators,
                  COUNT(DISTINCT departure_city)              AS cities,
                  COUNT(DISTINCT country)                     AS countries,
                  COUNT(DISTINCT date_from || '..' || date_to) AS windows,
                  COUNT(DISTINCT nights)                      AS durations,
                  COUNT(DISTINCT search_mode)                 AS modes,
                  COUNT(DISTINCT adults || '+' || children_ages) AS pax
             FROM cases WHERE enabled = 1"""
    ).fetchone()
    return dict(row) if row else {}


def composition(conn: sqlite3.Connection) -> list[dict]:
    """Очередь сводкой: оператор → маршруты с числом кейсов и сколько из них пройдено.

    Списком кейсы читать бессмысленно: их тысячи, и подряд идут почти одинаковые строки,
    различающиеся датой. Человеку нужно другое — «по Sunmar 1320 кейсов, из них
    Москва → Турция 24». Даты внутри маршрута сворачиваются в число: конкретное окно
    видно в отчёте по находке, а здесь важен объём работы.
    """
    rows = conn.execute(
        """SELECT operator, departure_city, country,
                  COUNT(*)                                        AS cases,
                  SUM(CASE WHEN last_checked IS NOT NULL THEN 1 ELSE 0 END) AS checked
             FROM cases
            WHERE enabled = 1
            GROUP BY operator, departure_city, country
            ORDER BY operator, cases DESC, departure_city, country"""
    ).fetchall()

    by_operator: dict[str, dict] = {}
    for row in rows:
        block = by_operator.setdefault(row["operator"], {
            "operator": row["operator"], "cases": 0, "checked": 0, "routes": [],
        })
        block["cases"] += row["cases"]
        block["checked"] += row["checked"] or 0
        block["routes"].append({
            "route": f'{row["departure_city"]} → {row["country"]}',
            "cases": row["cases"],
            "checked": row["checked"] or 0,
        })
    return list(by_operator.values())
