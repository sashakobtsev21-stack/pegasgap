"""Сверка: то ли искали, что просили.

Весь инструмент держится на допущении, что обе площадки выполнили ОДИН И ТОТ ЖЕ поиск.
Допущение это ничем не проверялось: мы отправляли параметры и верили, что они приняты.
А если однажды в одну площадку уйдут двое взрослых, а в другую трое, отели всё равно
сойдутся — и отчёт спокойно покажет «пропуски», которых нет.

Косвенным сторожем был систематический сдвиг цен, но он теперь всего лишь заметка, да и
ловил бы только различие, влияющее на цену. Прямая сверка надёжнее: обе площадки
возвращают в КАЖДОМ предложении дату заезда, число ночей и оператора — то есть не эхо
запроса, а свойства того, что реально нашлось.

Модуль чистый: без сети и провайдеров, чтобы проверялся офлайн.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pegasgap.models import SearchParams
from pegasgap.names import operator_matches

# Какая доля предложений должна соответствовать запросу. Не 100%: площадки иногда
# подмешивают соседние даты (например, «±3 дня» в рекомендациях), и падать из-за
# единичной строки значило бы браковать здоровые прогоны. Но уход ниже этой доли
# означает, что искали не то.
MIN_MATCHING_SHARE = 0.9


@dataclass(frozen=True)
class OfferFacts:
    """Что предложение говорит о себе само. Неизвестное поле — None, оно не проверяется."""

    checkin: date | None = None
    nights: int | None = None
    operator: str | None = None


def verify(params: SearchParams, facts: list[OfferFacts],
           operator: str = "") -> list[str]:
    """Расхождения между запросом и тем, что вернулось. Пусто — поиск был тот самый.

    Проверяется только то, что предложение о себе сообщило: пустая выдача или выдача без
    подробностей расхождений не даёт. Молчание здесь честнее выдуманной уверенности.
    """
    if not facts:
        return []

    problems: list[str] = []
    _check(problems, facts, "дата заезда",
           lambda f: f.checkin is None or params.date_from <= f.checkin <= params.date_to,
           f"{params.date_from:%d.%m}–{params.date_to:%d.%m}",
           lambda f: f"{f.checkin:%d.%m}" if f.checkin else "?")
    _check(problems, facts, "ночей",
           lambda f: f.nights is None or params.nights_min <= f.nights <= params.nights_max,
           f"{params.nights_min}–{params.nights_max}",
           lambda f: str(f.nights))
    if operator:
        _check(problems, facts, "оператор",
               lambda f: f.operator is None or operator_matches(f.operator, operator),
               operator, lambda f: str(f.operator))
    return problems


def _check(problems: list[str], facts: list[OfferFacts], field: str,
           ok, expected: str, actual) -> None:
    """Добавить расхождение, если несоответствующих предложений слишком много."""
    judged = [f for f in facts if ok(f) is not None]
    if not judged:
        return
    bad = [f for f in judged if not ok(f)]
    if len(bad) / len(judged) <= 1 - MIN_MATCHING_SHARE:
        return
    # В сообщении и ожидаемое, и пример полученного: без примера непонятно, что чинить.
    examples = ", ".join(sorted({actual(f) for f in bad})[:3])
    problems.append(
        f"поиск выполнен не по запросу — {field}: просили {expected}, "
        f"вернулось {examples} ({len(bad)} из {len(judged)} предложений)")
