"""Хранилище прогонов и находок — SQLite без ORM.

Историю ведём ради одного вопроса, на который снимок одного прогона ответить не может:
**этот пропуск новый или висит давно?** Он меняет приоритет разбора сильнее всего.
Новый пропуск на вчерашнем направлении — вероятная регрессия и повод смотреть сегодня;
пропуск, живущий третью неделю, — известная дыра в справочниках.

Недостоверные прогоны (`problems` непусты) сохраняются целиком, но **в историю находок не
попадают**: иначе один прогон с неприменившимся фильтром оператора засеял бы историю
сотней выдуманных пропусков, и «висит давно» перестало бы что-либо значить.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pegasgap.models import GapKind, HotelGap, ScanResult
from pegasgap.queue import SCHEMA as QUEUE_SCHEMA

DEFAULT_DB = Path("pegasgap.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at              TEXT    NOT NULL,
    scenario_key        TEXT    NOT NULL,
    operator            TEXT    NOT NULL,
    search_mode         TEXT    NOT NULL,
    departure_city      TEXT    NOT NULL,
    destination_country TEXT    NOT NULL,
    date_from           TEXT    NOT NULL,
    date_to             TEXT    NOT NULL,
    reference_status    TEXT    NOT NULL,
    checked_status      TEXT    NOT NULL,
    reference_hotels    INTEGER NOT NULL DEFAULT 0,
    checked_hotels      INTEGER NOT NULL DEFAULT 0,
    matched_hotels      INTEGER NOT NULL DEFAULT 0,
    price_offset_pct    REAL,
    trustworthy         INTEGER NOT NULL,
    problems            TEXT    NOT NULL DEFAULT '[]',
    notes               TEXT    NOT NULL DEFAULT '[]',
    unmatched           TEXT    NOT NULL DEFAULT '[]',
    params_json         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS gaps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    gap_key         TEXT    NOT NULL,
    kind            TEXT    NOT NULL,
    hotel_name      TEXT    NOT NULL,
    stars           INTEGER,
    resort          TEXT,
    reference_price TEXT,
    checked_price   TEXT,
    currency        TEXT    NOT NULL DEFAULT 'RUB',
    matched_name    TEXT,
    note            TEXT    NOT NULL DEFAULT '',
    diagnosis       TEXT    NOT NULL DEFAULT 'unknown',
    catalog_id      INTEGER,
    catalog_name    TEXT,
    checked_checkin TEXT,
    checked_meal    TEXT,
    checked_room    TEXT,
    -- Триаж: находку кто-то посмотрел и закрыл вопрос. Отдельно от самой находки,
    -- потому что это состояние РАЗБОРА, а не результата проверки: перепроверка
    -- направления не должна сбрасывать то, что человек уже отсмотрел.
    reviewed        INTEGER NOT NULL DEFAULT 0,
    reviewed_at     TEXT
);

-- Возраст находки: когда впервые увидели и сколько прогонов подряд она держится.
CREATE TABLE IF NOT EXISTS gap_history (
    scenario_key TEXT    NOT NULL,
    gap_key      TEXT    NOT NULL,
    first_seen   TEXT    NOT NULL,
    last_seen    TEXT    NOT NULL,
    times_seen   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (scenario_key, gap_key)
);

CREATE INDEX IF NOT EXISTS idx_runs_at       ON runs(run_at);
CREATE INDEX IF NOT EXISTS idx_runs_scenario ON runs(scenario_key);
CREATE INDEX IF NOT EXISTS idx_gaps_run      ON gaps(run_id);
CREATE INDEX IF NOT EXISTS idx_gaps_kind     ON gaps(kind);
"""


# Колонки, добавленные после первых прогонов. CREATE TABLE их уже содержит, но базы,
# созданные раньше, о них не знают — SQLite не умеет «добавить, если нет», поэтому
# сверяемся с PRAGMA. Без этого обновление кода роняло бы накопленную историю.
_MIGRATIONS = {
    "runs": {
        "notes": "TEXT NOT NULL DEFAULT '[]'",
        "checked_hotels": "INTEGER NOT NULL DEFAULT 0",
    },
    "gaps": {
        "diagnosis": "TEXT NOT NULL DEFAULT 'unknown'",
        "catalog_id": "INTEGER",
        "catalog_name": "TEXT",
        "reviewed": "INTEGER NOT NULL DEFAULT 0",
        "reviewed_at": "TEXT",
        "checked_checkin": "TEXT",
        "checked_meal": "TEXT",
        "checked_room": "TEXT",
    },
    # Оператор стал измерением кейса. Прежние кейсы все были по Pegas — значение по
    # умолчанию проставляет им его же, поэтому история проверок переживает обновление.
    "cases": {
        "operator": "TEXT NOT NULL DEFAULT 'Pegas Touristik'",
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, ddl in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    """Открыть (при необходимости создав) базу."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    conn.executescript(QUEUE_SCHEMA)
    _migrate(conn)
    return conn


@contextmanager
def session(path: Path | str = DEFAULT_DB) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_scan(conn: sqlite3.Connection, scan: ScanResult) -> int:
    """Сохранить прогон со всеми находками. Возвращает id прогона.

    Пишется одной транзакцией: наполовину сохранённый прогон (запись есть, находок нет)
    в отчёте выглядел бы как «всё чисто» — худший вид тихой потери данных.
    """
    p = scan.params
    with conn:  # атомарно
        cur = conn.execute(
            """INSERT INTO runs (
                   run_at, scenario_key, operator, search_mode, departure_city,
                   destination_country, date_from, date_to, reference_status,
                   checked_status, reference_hotels, checked_hotels,
                   matched_hotels, price_offset_pct,
                   trustworthy, problems, notes, unmatched, params_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                scan.run_at.isoformat(timespec="seconds"),
                p.scenario_key(), scan.operator, p.search_mode, p.departure_city,
                p.destination_country, p.date_from.isoformat(), p.date_to.isoformat(),
                scan.reference_status.value, scan.checked_status.value,
                scan.reference_hotels, scan.checked_hotels,
                scan.matched_hotels, scan.price_offset_pct,
                int(scan.trustworthy),
                json.dumps(scan.problems, ensure_ascii=False),
                json.dumps(scan.notes, ensure_ascii=False),
                json.dumps(scan.unmatched, ensure_ascii=False),
                p.model_dump_json(),
            ),
        )
        run_id = int(cur.lastrowid or 0)

        conn.executemany(
            """INSERT INTO gaps (
                   run_id, gap_key, kind, hotel_name, stars, resort,
                   reference_price, checked_price, currency, matched_name, note,
                   diagnosis, catalog_id, catalog_name,
                   checked_checkin, checked_meal, checked_room)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (run_id, g.key(), g.kind.value, g.hotel_name, g.stars, g.resort,
                 str(g.reference_price) if g.reference_price is not None else None,
                 str(g.checked_price) if g.checked_price is not None else None,
                 g.currency, g.matched_name, g.note,
                 g.diagnosis.value, g.catalog_id, g.catalog_name,
                 g.checked_checkin.isoformat() if g.checked_checkin else None,
                 g.checked_meal, g.checked_room)
                for g in scan.gaps
            ],
        )

        # История — только по достоверным прогонам. Иначе одна неудачная выгрузка засеет
        # её выдуманными находками, и «висит давно» перестанет что-либо значить.
        if scan.trustworthy:
            stamp = scan.run_at.isoformat(timespec="seconds")
            conn.executemany(
                """INSERT INTO gap_history (scenario_key, gap_key, first_seen, last_seen)
                   VALUES (?,?,?,?)
                   ON CONFLICT(scenario_key, gap_key) DO UPDATE SET
                       last_seen  = excluded.last_seen,
                       times_seen = times_seen + 1""",
                [(p.scenario_key(), g.key(), stamp, stamp) for g in scan.gaps],
            )
    return run_id


def gap_age(conn: sqlite3.Connection, scenario_key: str, gap: HotelGap) -> tuple[str, int] | None:
    """(когда впервые увидели, сколько раз) для находки — или None, если она новая."""
    row = conn.execute(
        "SELECT first_seen, times_seen FROM gap_history WHERE scenario_key=? AND gap_key=?",
        (scenario_key, gap.key()),
    ).fetchone()
    return (row["first_seen"], row["times_seen"]) if row else None


def new_gaps(conn: sqlite3.Connection, scan: ScanResult) -> list[HotelGap]:
    """Находки, которых раньше по этому сценарию не было. Их разбирают первыми.

    Вызывать ДО `save_scan`, иначе текущий прогон сам себя запишет в историю и всё
    станет «уже виденным».
    """
    key = scan.params.scenario_key()
    known = {
        r["gap_key"] for r in conn.execute(
            "SELECT gap_key FROM gap_history WHERE scenario_key=?", (key,))
    }
    return [g for g in scan.gaps if g.key() not in known]


def summary_since(conn: sqlite3.Connection, since: datetime) -> dict[str, int]:
    """Сколько находок каждого класса в достоверных прогонах с указанного момента."""
    rows = conn.execute(
        """SELECT g.kind AS kind, COUNT(*) AS n
             FROM gaps g JOIN runs r ON r.id = g.run_id
            WHERE r.run_at >= ? AND r.trustworthy = 1
            GROUP BY g.kind""",
        (since.isoformat(timespec="seconds"),),
    ).fetchall()
    counts = {k.value: 0 for k in GapKind}
    for row in rows:
        counts[row["kind"]] = row["n"]
    return counts


def standing_gaps(conn: sqlite3.Connection, min_times: int = 3) -> list[sqlite3.Row]:
    """Находки, повторившиеся не меньше `min_times` раз — застарелые, системные."""
    return conn.execute(
        """SELECT scenario_key, gap_key, first_seen, last_seen, times_seen
             FROM gap_history
            WHERE times_seen >= ?
            ORDER BY times_seen DESC, first_seen ASC""",
        (min_times,),
    ).fetchall()


def runs_since(conn: sqlite3.Connection, since: datetime) -> list[sqlite3.Row]:
    """Прогоны с указанного момента, свежие первыми."""
    return conn.execute(
        "SELECT * FROM runs WHERE run_at >= ? ORDER BY run_at DESC",
        (since.isoformat(timespec="seconds"),),
    ).fetchall()


def gaps_of_run(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM gaps WHERE run_id = ? ORDER BY kind, hotel_name", (run_id,)
    ).fetchall()


def set_reviewed(conn: sqlite3.Connection, gap_id: int, reviewed: bool = True) -> bool:
    """Отметить находку разобранной. False — такой находки нет."""
    cur = conn.execute(
        "UPDATE gaps SET reviewed = ?, reviewed_at = ? WHERE id = ?",
        (int(reviewed),
         datetime.now().isoformat(timespec="seconds") if reviewed else None,
         gap_id),
    )
    return bool(cur.rowcount)


# Отчёт отвечает на вопрос «чего нет у нас», и обратное направление в нём не участвует
# (см. gaps.REPORT_REVERSE). Фильтр стоит на ЧТЕНИИ, а не удалением строк: прогоны,
# сделанные до этого решения, остаются в базе целиком и доступны для разбора запросом.
_REPORTED_KINDS = "g.kind <> 'reverse'"


def findings(conn: sqlite3.Connection, since: datetime, only_open: bool = False,
             limit: int = 500) -> list[sqlite3.Row]:
    """Находки за период вместе с параметрами прогона — то, что показывает отчёт.

    Недостоверные прогоны исключены: их находки нельзя ни разбирать, ни считать.
    """
    return conn.execute(
        f"""SELECT g.*, r.run_at, r.departure_city, r.destination_country,
                   r.date_from AS run_date_from, r.date_to AS run_date_to,
                   r.search_mode, r.params_json, r.operator
              FROM gaps g JOIN runs r ON r.id = g.run_id
             WHERE r.run_at >= ? AND r.trustworthy = 1 AND {_REPORTED_KINDS}
                   {"AND g.reviewed = 0" if only_open else ""}
             ORDER BY g.reviewed ASC, r.run_at DESC
             LIMIT ?""",
        (since.isoformat(timespec="seconds"), limit),
    ).fetchall()


def findings_summary(conn: sqlite3.Connection, since: datetime) -> dict:
    """Счётчики для шапки: сколько найдено и сколько из этого уже разобрано."""
    row = conn.execute(
        f"""SELECT COUNT(*) AS total,
                   SUM(CASE WHEN g.reviewed = 1 THEN 1 ELSE 0 END) AS reviewed
              FROM gaps g JOIN runs r ON r.id = g.run_id
             WHERE r.run_at >= ? AND r.trustworthy = 1 AND {_REPORTED_KINDS}""",
        (since.isoformat(timespec="seconds"),),
    ).fetchone()
    total = row["total"] or 0
    reviewed = row["reviewed"] or 0
    return {"total": total, "reviewed": reviewed, "open": total - reviewed}
