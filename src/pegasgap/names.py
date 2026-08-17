"""Сопоставление имён туроператоров между площадками.

Один и тот же оператор пишется на площадках по-разному: разный алфавит
(`Intourist` ↔ `Интурист`), `&` против `and` (`FUN and SUN` ↔ `FUN&SUN (TUI)`),
слитно-раздельно (`Biblio Globus` ↔ `Biblioglobus`), региональные клоны в скобках
(`Coral` против `Coral Travel (BY)`).

Модуль **чистый** — без браузера и сети. Это сознательно: классификация пропусков
опирается на сравнение имён операторов, и она должна тестироваться офлайн, а не
требовать установленного Playwright ради одной регулярки.

Порядок сопоставления: алиас → регион → ядро имени. Регион проверяется ДО ядра, иначе
`Coral Travel` подтянуло бы `Coral Travel (BY)` — это разные операторы.
"""

from __future__ import annotations

import re

from pegasgap.models import Offer

# Случаи, которые нечёткий матчинг не покрывает (разные алфавиты/написание):
# ключ = нормализованное имя на Слетать → значение = имя на Турвизоре.
OPERATOR_ALIASES = {
    "спектрум": "Spectrum",
    "amigos": "Амиго-С",
    "amigotours": "Амиго-Турс",
    "пакс": "Paks",
    "русскийэкспресс": "Russian Express",
    "intourist": "Интурист",
    "travelluxekz": "Space Travel KZ (Travel Luxe)",
}


def operator_norm(s: str) -> str:
    """Имя оператора → ключ для словаря алиасов."""
    return re.sub(r"[^a-zа-я0-9]", "", (s or "").lower())


def op_core(s: str) -> str:
    """Ядро имени: нижний регистр, без скобок, без «and»/«и», только буквы и цифры.

    Зеркало JS-нормализации из `_select_operators` в провайдере Турвизора — матчинг при
    выборе оператора на форме и при разборе выдачи обязан быть одинаковым, иначе выберем
    одно, а отфильтруем другое.
    """
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"\band\b|\bи\b", "", s)
    return re.sub(r"[^a-zа-я0-9]", "", s, flags=re.IGNORECASE)


def op_region(s: str) -> str:
    """Региональный суффикс (BY/KZ/UZ) — чтобы не путать клонов оператора."""
    m = re.search(r"\((BY|KZ|UZ)\)", s or "", re.IGNORECASE)
    return m.group(1).lower() if m else ""


def operator_matches(candidate: str, wanted: str) -> bool:
    """True, если `candidate` — тот же оператор, что и `wanted` (с учётом алиасов и региона)."""
    target = OPERATOR_ALIASES.get(operator_norm(wanted), wanted)
    wc, wr = op_core(target), op_region(target)
    if not wc:
        return False
    bc, br = op_core(candidate), op_region(candidate)
    if br != wr:
        return False
    return bc == wc or (bool(bc) and (wc in bc or bc in wc))


def find_operator(names: list[str], wanted: str) -> str | None:
    """Первое имя из списка, означающее нужного оператора (или None)."""
    return next((n for n in names if operator_matches(n, wanted)), None)


def filter_offers_by_operators(offers: list[Offer], operators: list[str]) -> list[Offer]:
    """Оставить только офферы запрошенных операторов. Пустой запрос = без фильтра.

    Подстраховка корректности: на Турвизоре фильтр операторов best-effort и на самой
    форме не верифицируется. Если он не применился, в выдаче остаются лишние операторы
    (в т.ч. промо-дефолт Biblioglobus) — и «самое дешёвое» пришло бы от незапрошенного
    оператора. Поэтому отбрасываем всё, что не входит в запрос.
    """
    if not operators:
        return offers
    return [o for o in offers if any(operator_matches(o.operator, w) for w in operators)]
