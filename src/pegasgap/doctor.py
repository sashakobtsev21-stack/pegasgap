"""Инварианты базы и двойная бухгалтерия счётчиков — блок Ж плана проверки.

Каждое число на экране живёт дважды: раз в счётчике, раз в строках, из которых оно
посчитано. Пока они сходятся — отчёту можно верить; разъехались — где-то врут. Этот
модуль сверяет пары и проверяет структурные инварианты, которые не может нарушить
ни один ЗДОРОВЫЙ прогон: осиротевшие находки, цены на разные заезды, недостоверные
прогоны без причин.

Запуск: `pegasgap doctor`. Ноль проблем — выход 0; иначе список нарушений и выход 1,
чтобы команду можно было ставить в конвейер.

Только чтение. Нарушение — не всегда «чинить базу»: чаще это симптом бага в коде,
который записал такое, и разбираться надо с ним.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pegasgap import storage
from pegasgap.models import SearchParams


@dataclass(frozen=True)
class Check:
    """Итог одной проверки: имя, прошла ли, и что именно не так."""

    name: str
    ok: bool
    detail: str = ""


def _count(conn: sqlite3.Connection, sql: str, *args) -> int:
    return conn.execute(sql, args).fetchone()[0]


def run_checks(conn: sqlite3.Connection) -> list[Check]:
    """Все инварианты разом. Порядок — от структурных к бухгалтерии."""
    out: list[Check] = []

    # Ж1. Сироты и битые параметры: находка без прогона бессмысленна, прогон с
    # нечитаемыми параметрами невозможно ни показать, ни перепроверить.
    orphans = _count(conn, "SELECT COUNT(*) FROM gaps g LEFT JOIN runs r ON r.id = g.run_id "
                           "WHERE r.id IS NULL")
    out.append(Check("Ж1а: находки без прогона", orphans == 0, f"осиротевших строк: {orphans}"))
    broken = 0
    for row in conn.execute("SELECT id, params_json FROM runs"):
        try:
            SearchParams.model_validate_json(row["params_json"])
        except Exception:
            broken += 1
    out.append(Check("Ж1б: params_json читается", broken == 0, f"битых прогонов: {broken}"))

    # Ж2. Цены сравниваются только на общий заезд и только положительные.
    bad_days = _count(conn, "SELECT COUNT(*) FROM gaps WHERE kind='price' AND "
                            "(reference_checkin IS NULL OR checked_checkin IS NULL "
                            " OR reference_checkin <> checked_checkin)")
    out.append(Check("Ж2а: price — один заезд с обеих сторон", bad_days == 0,
                     f"находок с разными заездами: {bad_days}"))
    bad_price = _count(conn, "SELECT COUNT(*) FROM gaps WHERE kind='price' AND "
                             "(CAST(reference_price AS REAL) <= 0 "
                             " OR CAST(checked_price AS REAL) <= 0)")
    out.append(Check("Ж2б: price — обе цены положительные", bad_price == 0,
                     f"находок с пустой ценой: {bad_price}"))

    # Ж3. Обратная находка обязана нести наш id отеля — иначе ссылка «на Слетать»
    # снова поведёт на общий поиск (живой регресс Atlantis Royal).
    no_id = _count(conn, "SELECT COUNT(*) FROM gaps WHERE kind='reverse' AND catalog_id IS NULL")
    out.append(Check("Ж3: reverse — есть наш id отеля", no_id == 0,
                     f"находок без catalog_id: {no_id}"))

    # Ж4. История возраста: times_seen — счётчик, а не мусор.
    bad_hist = _count(conn, "SELECT COUNT(*) FROM gap_history WHERE times_seen < 1 "
                            "OR first_seen > last_seen")
    out.append(Check("Ж4: gap_history согласована", bad_hist == 0,
                     f"битых строк истории: {bad_hist}"))

    # Ж5. Отметки «разобрано» ссылаются на существующие проблемы. Осиротевшая отметка —
    # предупреждение, а не поломка: после ручной чистки находок это ожидаемо.
    orphan_reviews = _count(
        conn,
        f"SELECT COUNT(*) FROM gap_review v WHERE NOT EXISTS ("
        f"  SELECT 1 FROM gaps g JOIN runs r ON r.id = g.run_id "
        f"  WHERE {storage._PROBLEM_KEY} = v.problem_key)")
    out.append(Check("Ж5: «разобрано» ссылается на живые проблемы", orphan_reviews == 0,
                     f"осиротевших отметок: {orphan_reviews} (после чистки данных — норма)"))

    # Ж6. Достоверность и причины ходят парой.
    lying = _count(conn, "SELECT COUNT(*) FROM runs WHERE "
                         "(trustworthy = 1 AND problems <> '[]') "
                         "OR (trustworthy = 0 AND problems = '[]')")
    out.append(Check("Ж6: trustworthy ↔ problems согласованы", lying == 0,
                     f"противоречивых прогонов: {lying}"))

    # Бухгалтерия Б1: очередь. Счётчики API считаются из cases — сверяем с сырыми
    # строками той же таблицы другим запросом.
    from pegasgap import queue as case_queue
    stats = case_queue.stats(conn)
    raw_total = _count(conn, "SELECT COUNT(*) FROM cases WHERE enabled = 1")
    raw_checked = _count(conn, "SELECT COUNT(*) FROM cases WHERE enabled = 1 "
                               "AND last_checked IS NOT NULL")
    ok = stats["total"] == raw_total and stats["checked"] == raw_checked
    out.append(Check("Б1: счётчики очереди = строки cases", ok,
                     f"API {stats['total']}/{stats['checked']}, "
                     f"сырые {raw_total}/{raw_checked}"))

    # Бухгалтерия Б2: свод отчёта. findings_summary против независимого пересчёта
    # уникальных проблем тем же определением ключа.
    since = datetime.now() - timedelta(days=7)
    summary = storage.findings_summary(conn, since)
    raw_unique = _count(
        conn,
        f"SELECT COUNT(DISTINCT {storage._PROBLEM_KEY}) FROM gaps g "
        f"JOIN runs r ON r.id = g.run_id "
        f"WHERE r.run_at >= ? AND r.trustworthy = 1 AND {storage._reported_kinds()}",
        since.isoformat(timespec="seconds"))
    out.append(Check("Б2: уникальные проблемы свода = пересчёт", summary["unique"] == raw_unique,
                     f"свод {summary['unique']}, пересчёт {raw_unique}"))

    # Бухгалтерия Б3: панель непроверенного.
    failed_api = len(storage.failed_runs(conn, since))
    failed_raw = _count(conn, "SELECT COUNT(*) FROM runs WHERE trustworthy = 0 "
                              "AND run_at >= ?", since.isoformat(timespec="seconds"))
    out.append(Check("Б3: панель непроверенного = прогоны", failed_api == failed_raw,
                     f"API {failed_api}, сырые {failed_raw}"))

    # Бухгалтерия Б4: возраст находок. «Держится N прогонов» на экране берётся из
    # gap_history — у показанных строк история обязана существовать.
    missing_hist = _count(
        conn,
        "SELECT COUNT(*) FROM gaps g JOIN runs r ON r.id = g.run_id "
        "WHERE r.trustworthy = 1 AND NOT EXISTS ("
        "  SELECT 1 FROM gap_history h WHERE h.scenario_key = r.scenario_key "
        "  AND h.gap_key = g.gap_key)")
    out.append(Check("Б4: у находок есть история возраста", missing_hist == 0,
                     f"находок без истории: {missing_hist}"))

    return out


def report(checks: list[Check]) -> str:
    """Человекочитаемый итог: одна строка на проверку, шапка с вердиктом."""
    failed = [c for c in checks if not c.ok]
    lines = [f"{'✗' if not c.ok else '✓'} {c.name} — {c.detail}" for c in checks]
    verdict = ("все проверки прошли" if not failed
               else f"НАРУШЕНИЙ: {len(failed)} из {len(checks)}")
    return "\n".join([verdict, *lines])


async def live_refdata_checks(config_path: str = "scenarios.yaml") -> list[Check]:
    """А9 плана: вся матрица резолвится в справочниках ОБЕИХ площадок — без поиска.

    Ловит тихую смерть кейса: город переименовали, страну сняли с витрины, оператора
    убрали из направления — и кейс молча возвращает «не найден» на каждом круге, сжигая
    квоту. Дешёвая проверка словарями, поиск не запускается.
    """
    import httpx

    from pegasgap.providers import tourvisor_api as tv
    from pegasgap.providers.sletat_api import SletatApiProvider
    from pegasgap.proxies import pool
    from pegasgap.scenarios import load_matrix

    matrix = load_matrix(Path(config_path))
    cities = sorted({c for c in matrix.departure_cities}
                    | {c for c, _ in matrix.routes})
    countries = sorted({c for c in matrix.countries} | {c for _, c in matrix.routes})
    operators = sorted(matrix.operators)
    out: list[Check] = []

    # --- Турвизор: один вызов справочников на всё -----------------------------------
    provider = tv.TourvisorApiProvider()
    proxy = pool().acquire()
    async with httpx.AsyncClient(timeout=60, proxy=proxy.url if proxy else None,
                                 headers={"Referer": tv.REFERER,
                                          "User-Agent": tv.DESKTOP_UA}) as client:
        lists = await provider._reference(client)
        miss_c = [c for c in cities
                  if tv._find_id(tv._items(lists, "departures", "departure"), c) is None]
        miss_s = [c for c in countries
                  if tv._find_id(tv._items(lists, "allcountry", "country"), c) is None]
        miss_o = [o for o in operators if provider._operator_id(lists, o) is None]
    out.append(Check("А9а: города в словаре Турвизора", not miss_c, f"нет: {miss_c or '—'}"))
    out.append(Check("А9б: страны в словаре Турвизора", not miss_s, f"нет: {miss_s or '—'}"))
    out.append(Check("А9в: операторы в словаре Турвизора", not miss_o, f"нет: {miss_o or '—'}"))

    # --- Слетать: город + страны на каждый город --------------------------------------
    sl = SletatApiProvider()
    proxy = pool().acquire()
    bad: list[str] = []
    async with httpx.AsyncClient(timeout=60, headers={"Referer": "https://sletat.ru/"},
                                 proxy=proxy.url if proxy else None) as client:
        for city in cities:
            try:
                city_id = await sl._resolve_city(client, city)
            except Exception:
                bad.append(f"{city}: город не резолвится")
                continue
            for country in countries:
                try:
                    await sl._resolve_country(client, city_id, country)
                except Exception:
                    bad.append(f"{city} → {country}")
    out.append(Check("А9г: направления в справочнике Слетать", not bad,
                     "; ".join(bad[:8]) or "все пары резолвятся"))
    return out
