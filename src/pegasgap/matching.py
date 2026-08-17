"""Сопоставление отелей между площадками.

Самый ответственный модуль инструмента. Вывод «этого отеля у нас нет» имеет смысл
ровно настолько, насколько мы уверены, что речь об одном и том же отеле.

**Асимметрия ошибок.** Матчинг может ошибиться в две стороны, и цена ошибок разная:

* сматчили лишнего → реальный пропуск не заметили. Плохо, но безопасно;
* не сматчили своё → выдумали пропуск, которого нет. Коллега идёт разбирать
  несуществующую проблему, и после двух-трёх таких случаев отчёту перестают верить.

Вторая ошибка дороже, поэтому матчинг намеренно **щедрый**, а всё сомнительное уходит в
корзину `review` и в пропуски не попадает.

Названия отелей на площадках расходятся предсказуемо: разный порядок слов, суффиксы
`Hotel/Resort/SPA`, приписка `(ex. Старое Имя)` после ребрендинга, `&` против `and`,
маркер звёзд прямо в названии. Всё это снимается нормализацией — тем же приёмом, каким
в провайдере Турвизора уже сопоставляются имена операторов.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from pegasgap.models import HotelOffer

# Слова, не несущие различительной силы в названии отеля: они то есть, то нет, и по ним
# нельзя отличить один отель от другого. Убираем до сравнения.
_NOISE = {
    "hotel", "hotels", "resort", "resorts", "spa", "and", "the", "by", "de", "el",
    "club", "apartments", "apart", "aparthotel", "villas", "suites", "beach",
    "отель", "гостиница", "апартаменты", "клуб", "пляж",
    "adults", "only", "all", "inclusive", "ultra",
}

# «(ex. Старое имя)», «ex Старое имя» — приписка о прежнем названии. На площадках она то
# есть, то нет, и то у разного имени; сравнивать надо ТЕКУЩЕЕ название, приписку срезаем.
_EX_SUFFIX = re.compile(r"[\(\[]?\s*\bex\b\.?\s.*$", re.IGNORECASE)

# Маркер звёзд внутри названия: «5*», «4 *», «5 звёзд», «3 star(s)».
_STARS_IN_NAME = re.compile(r"\b\d\s*(?:\*+|звезд\w*|star\w*)", re.IGNORECASE)

# Всё, кроме букв (латиница + кириллица), цифр и пробела.
_NON_WORD = re.compile(r"[^0-9a-zA-Zа-яА-ЯёЁ ]+")
_SPACES = re.compile(r"\s+")

# Ниже этой длины «ядро» названия слишком короткое, чтобы вхождение одной строки в другую
# что-то доказывало: «sun» входит в «sunrise», но это разные отели.
_MIN_CORE_FOR_CONTAINMENT = 7

# Порог схожести множеств значимых слов для неуверенного совпадения.
_WEAK_JACCARD = 0.6


class Confidence(StrEnum):
    """Насколько мы уверены, что это один и тот же отель."""

    EXACT = "exact"    # ядра названий совпали, звёзды не противоречат
    STRONG = "strong"  # одно название содержит другое, звёзды не противоречат
    WEAK = "weak"      # похоже, но недостаточно — в корзину проверки, НЕ в пропуски
    NONE = "none"      # кандидата нет

    @property
    def comparable(self) -> bool:
        """Можно ли считать пару одним отелем и сравнивать цены."""
        return self in (Confidence.EXACT, Confidence.STRONG)


def normalize(name: str) -> str:
    """Название отеля → сравнимая форма: без приписки ex, звёзд, пунктуации и шумовых слов."""
    s = (name or "").strip()
    s = _EX_SUFFIX.sub(" ", s)
    s = _STARS_IN_NAME.sub(" ", s)
    s = s.replace("&", " and ")
    s = _NON_WORD.sub(" ", s).lower()
    s = _SPACES.sub(" ", s).strip()
    words = [w for w in s.split() if w not in _NOISE]
    # Если после чистки не осталось ничего (название целиком состояло из шумовых слов) —
    # возвращаем очищенную строку как есть, иначе потеряли бы отель совсем.
    return " ".join(words) if words else s


def core(name: str) -> str:
    """Ядро названия: нормализованная форма без пробелов — устойчива к порядку букв в стыках."""
    return normalize(name).replace(" ", "")


def tokens(name: str) -> set[str]:
    """Множество значимых слов названия."""
    return set(normalize(name).split())


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _stars_conflict(a: HotelOffer, b: HotelOffer) -> bool:
    """True, если звёздность явно противоречит. Неизвестная звёздность не противоречит ничему."""
    return bool(a.stars and b.stars and a.stars != b.stars)


def compare(a: HotelOffer, b: HotelOffer) -> tuple[Confidence, str]:
    """Насколько уверенно два предложения относятся к одному отелю."""
    ca, cb = core(a.hotel_name), core(b.hotel_name)
    if not ca or not cb:
        return Confidence.NONE, "пустое название после нормализации"

    conflict = _stars_conflict(a, b)

    if ca == cb:
        # Название то же, а звёзды разные — либо ошибка данных на одной из площадок, либо
        # всё-таки разные объекты сети. Пропуском такое объявлять нельзя, только в проверку.
        if conflict:
            return Confidence.WEAK, f"названия совпали, звёзды разошлись ({a.stars} и {b.stars})"
        return Confidence.EXACT, "названия совпали"

    contains = ca in cb or cb in ca
    if contains and min(len(ca), len(cb)) >= _MIN_CORE_FOR_CONTAINMENT:
        if conflict:
            return Confidence.WEAK, f"название вложено, звёзды разошлись ({a.stars} и {b.stars})"
        return Confidence.STRONG, "одно название содержится в другом"

    sim = _jaccard(tokens(a.hotel_name), tokens(b.hotel_name))
    if sim >= _WEAK_JACCARD or contains:
        return Confidence.WEAK, f"частичное совпадение слов ({sim:.0%})"

    return Confidence.NONE, ""


@dataclass
class HotelMatch:
    """Пара «отель на эталоне ↔ отель на проверяемой площадке»."""

    reference: HotelOffer
    checked: HotelOffer
    confidence: Confidence
    reason: str = ""


@dataclass
class MatchResult:
    """Итог сопоставления двух выдач."""

    pairs: list[HotelMatch] = field(default_factory=list)
    # Неуверенные совпадения: НЕ пропуски и НЕ сравнимые пары. Отдельная секция отчёта.
    review: list[HotelMatch] = field(default_factory=list)
    # Есть на эталоне, кандидата на проверяемой не нашлось — кандидаты в отельные пропуски.
    only_reference: list[HotelOffer] = field(default_factory=list)
    # Есть на проверяемой, нет на эталоне — кандидаты в обратные пропуски.
    only_checked: list[HotelOffer] = field(default_factory=list)

    @property
    def matched_share(self) -> float:
        """Доля отелей эталона, для которых нашлась пара. Низкая доля — повод не верить
        отчёту целиком: скорее всего сломался матчинг, а не пропало полкаталога."""
        total = len(self.pairs) + len(self.review) + len(self.only_reference)
        return len(self.pairs) / total if total else 1.0


def match_hotels(reference: list[HotelOffer], checked: list[HotelOffer]) -> MatchResult:
    """Сопоставить выдачу эталона с выдачей проверяемой площадки.

    Жадно в три прохода — сначала все точные совпадения, потом сильные, потом слабые.
    Проходы важны: иначе первый же отель мог бы «съесть» по слабому совпадению кандидата,
    который точно подходит другому. Один отель проверяемой площадки используется один раз,
    поэтому остаток честно означает «пары не нашлось».
    """
    result = MatchResult()
    free = list(checked)
    pending = list(reference)

    for level in (Confidence.EXACT, Confidence.STRONG, Confidence.WEAK):
        still: list[HotelOffer] = []
        for ref in pending:
            best: tuple[HotelOffer, str] | None = None
            for cand in free:
                conf, reason = compare(ref, cand)
                if conf is level:
                    best = (cand, reason)
                    break
            if best is None:
                still.append(ref)
                continue
            cand, reason = best
            free.remove(cand)
            match = HotelMatch(reference=ref, checked=cand, confidence=level, reason=reason)
            (result.review if level is Confidence.WEAK else result.pairs).append(match)
        pending = still

    result.only_reference = pending
    result.only_checked = free
    return result
