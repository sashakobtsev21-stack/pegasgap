"""Утилиты сверки фактических значений формы с заданными параметрами.

Принцип: если значение поля прочитать НЕ удалось — считаем шаг непроверяемым
(пропускаем), а не расхождением. Это исключает ложные блокировки валидного поиска.
"""

from __future__ import annotations

import re

# Сентинел «значение не прочитано» — такой шаг не проверяется.
UNKNOWN: object = object()


def norm(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def text_contains(expected, actual) -> bool:
    """Ожидаемое содержится в фактическом тексте поля (терпимо к лишним подписям)."""
    return norm(expected) in norm(actual)


def exact(expected, actual) -> bool:
    return norm(expected) == norm(actual)


def set_equal(expected: set[str], actual: set[str]) -> bool:
    """Множества совпадают — ни пропущенных, ни ЛИШНИХ (защита от случайных значений)."""
    return {norm(e) for e in expected} == {norm(a) for a in actual}


class FormVerificationError(RuntimeError):
    """Форма заполнена не теми значениями — поиск не запускаем."""

    def __init__(self, problems: list[tuple[str, object, object]]) -> None:
        self.problems = problems
        details = "; ".join(
            f"{field}: ожидали {exp!r}, на форме {act!r}" for field, exp, act in problems
        )
        super().__init__(f"проверка формы не пройдена — {details}")
