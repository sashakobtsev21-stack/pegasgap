"""Кандидаты в словарь синонимов — по почерку парных находок.

Приём снят с живого разбора: самые устойчивые проблемы ходят ПАРАМИ «отеля нет на
Слетать» + «отеля нет на Турвизоре» с похожими именами на одном направлении — это
почерк одного отеля, названного площадками по-разному (Kaftans City ↔ KAFTANS BY RRH&R,
MC Beach Park ↔ MC BEACH RESORT, Beso Beach ↔ ELITE LIFE (ex. Beso Beach)). Человек
находил такие пары глазами в истории повторов; команда `pegasgap candidates` ищет их
сама и печатает со свидетельствами (курорты, звёзды) для сверки.

Команда ТОЛЬКО предлагает: в словарь пары вносит человек, сверив. Автоматическое
внесение противоречило бы правилу словаря — он сильнее всех правил матчинга, и ошибка
в нём молча склеивает два разных отеля.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher

# Слова-места: общее «bodrum» роднит каждый отель Бодрума, свидетельством не является.
from pegasgap.diagnosis import _PLACE_TOKENS
from pegasgap.matching import ascii_core, tokens

# Ниже этого сходства пара — совпадение случайных букв, не кандидат.
MIN_SCORE = 0.6


@dataclass(frozen=True)
class Candidate:
    """Пара находок, похожая на один отель под разными именами."""

    operator: str
    route: str
    theirs: str          # имя в листинге витрины (находка «нет у нас»)
    ours: str            # имя в нашей выдаче (находка «нет у них»)
    score: float
    theirs_stars: int | None
    ours_stars: int | None
    ours_resort: str | None

    @property
    def stars_agree(self) -> bool:
        return (self.theirs_stars is None or self.ours_stars is None
                or abs(self.theirs_stars - self.ours_stars) <= 1)


def _identity_core(name: str) -> str:
    """Ядро имени БЕЗ топонимов: город роднит все отели города и тождества не несёт."""
    kept = [t for t in tokens(name) if t not in _PLACE_TOKENS]
    return ascii_core(" ".join(sorted(kept)))


def _similarity(a: str, b: str) -> float:
    """Сходство идентичностей: без шумовых слов и топонимов, с поощрением общих слов.

    Топонимы вырезаны из ядра (город роднит чужие отели: «RIXOS SHARM» и «Savoy
    Sharm»), но РАЗНЫЕ топонимы в паре — штраф: у сетевых отелей город как раз
    различитель, и «AKRA ANTALYA» с «Akra Kemer» — разные объекты одной сети.
    """
    ca, cb = _identity_core(a), _identity_core(b)
    if not ca or not cb:
        return 0.0
    score = SequenceMatcher(None, ca, cb).ratio()
    shared = (tokens(a) & tokens(b)) - _PLACE_TOKENS
    if shared:
        score = min(1.0, score + 0.2)
    pa, pb = tokens(a) & _PLACE_TOKENS, tokens(b) & _PLACE_TOKENS
    if pa and pb and not (pa & pb):
        score *= 0.5
    return score


def find_candidates(conn: sqlite3.Connection, days: int = 7) -> list[Candidate]:
    """Парные находки ОДНОГО прогона за период.

    Пара ищется внутри прогона намеренно: настоящий промах матчера рождает обе стороны
    в одном сравнении. Пары «через прогоны» — исторические артефакты уже починенных
    ошибок, и первый живой запуск утонул именно в них.
    """
    rows = conn.execute(
        """SELECT g.run_id, g.kind, g.hotel_name, g.stars, g.resort,
                  r.operator, r.departure_city || ' → ' || r.destination_country AS route
           FROM gaps g JOIN runs r ON r.id = g.run_id
           WHERE r.trustworthy = 1 AND g.kind IN ('hotel', 'reverse')
             AND r.run_at >= datetime('now', ?)""", (f"-{days} days",)).fetchall()

    theirs: dict[int, list] = {}
    ours: dict[int, list] = {}
    for run_id, kind, name, stars, resort, operator, route in rows:
        bucket = theirs if kind == "hotel" else ours
        bucket.setdefault(run_id, []).append((name, stars, resort, operator, route))

    seen_pairs: set[tuple[str, str]] = set()
    out: list[Candidate] = []
    for run_id in theirs.keys() & ours.keys():
        for t_name, t_stars, _, operator, route in theirs[run_id]:
            best, score = None, 0.0
            for o in ours[run_id]:
                s = _similarity(t_name, o[0])
                if s > score:
                    best, score = o, s
            if best is None or score < MIN_SCORE:
                continue
            if (t_name, best[0]) in seen_pairs:
                continue
            seen_pairs.add((t_name, best[0]))
            out.append(Candidate(operator=operator, route=route, theirs=t_name,
                                 ours=best[0], score=round(score, 2),
                                 theirs_stars=t_stars, ours_stars=best[1],
                                 ours_resort=best[2]))
    out.sort(key=lambda c: -c.score)
    return out
