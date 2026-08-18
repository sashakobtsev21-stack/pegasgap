"""Сопоставление отелей между площадками.

Самый ответственный модуль инструмента. Вывод «этого отеля у нас нет» имеет смысл
ровно настолько, насколько мы уверены, что речь об одном и том же отеле.

**Асимметрия ошибок.** Матчинг может ошибиться в две стороны, и цена ошибок разная:

* сматчили лишнего → реальный пропуск не заметили. Плохо, но безопасно;
* не сматчили своё → выдумали пропуск, которого нет. Коллега идёт разбирать
  несуществующую проблему, и после двух-трёх таких случаев отчёту перестают верить.

Вторая ошибка дороже, поэтому матчинг намеренно **щедрый**, а всё сомнительное уходит в
корзину `review` и в пропуски не попадает.

**Что расходится в названиях.** Порядок слов, суффиксы `Hotel/Resort/SPA`, приписка
`(ex. Старое Имя)` после ребрендинга, `&` против `and`, маркер звёзд в названии. На
русских направлениях добавляется своё:

* **гомоглифы** — в «ЭРА CПА» буква `C` латинская посреди кириллицы, и посимвольное
  сравнение молча считает слово другим;
* **альтернативное написание в скобках** — «SALVE (САЛЬВЭ)», «БЭСЭДЭР (BESEDER)»: то же
  имя вторым алфавитом, и совпасть может любая из половин;
* **свой набор шумовых слов** — «гостевой дом», «пансионат», «база отдыха».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from pegasgap.models import HotelOffer

# Слова, не несущие различительной силы: они то есть, то нет, и по ним нельзя отличить
# один отель от другого.
_NOISE = {
    "hotel", "hotels", "resort", "resorts", "spa", "and", "the", "by", "de", "el",
    "club", "apartments", "apart", "aparthotel", "villas", "suites", "beach", "inn",
    "отель", "гостиница", "апартаменты", "апарт", "клуб", "пляж", "спа",
    "гостевой", "дом", "пансионат", "санаторий", "база", "отдыха", "мини",
    "adults", "only", "all", "inclusive", "ultra",
}

# «(ex. Старое имя)», «ex Старое имя» — приписка о прежнем названии. Она то есть, то нет,
# и относится к разным именам; сравнивать надо ТЕКУЩЕЕ название.
_EX_SUFFIX = re.compile(r"[\(\[]?\s*\bex\b\.?\s.*$", re.IGNORECASE)

# Любая скобка: её содержимое может оказаться тем же именем на другом алфавите.
_PARENS = re.compile(r"[\(\[]([^\)\]]{2,})[\)\]]")

_STARS_IN_NAME = re.compile(r"\b\d\s*(?:\*+|звезд\w*|star\w*)", re.IGNORECASE)
_NON_WORD = re.compile(r"[^0-9a-zA-Zа-яА-ЯёЁ ]+")
_SPACES = re.compile(r"\s+")

_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
_LATIN = re.compile(r"[a-z]", re.IGNORECASE)

# Латинские буквы, неотличимые на вид от кириллических. Внутри смешанного слова такая
# буква — почти наверняка опечатка раскладки, а не настоящая латиница.
_LOOKALIKE_TO_CYRILLIC = str.maketrans({
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х",
})

# Ниже этой длины «ядро» слишком короткое, чтобы вхождение одной строки в другую
# что-то доказывало: «sun» входит в «sunrise», но это разные отели.
_MIN_CORE_FOR_CONTAINMENT = 7
# И вовсе бессмысленное ядро: «А-Отель» после вычистки шума превращается в «а», а один
# символ входит в половину названий мира. Такое ядро в сравнении не участвует.
_MIN_MEANINGFUL_CORE = 3

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


def fix_homoglyphs(text: str) -> str:
    """В смешанном слове привести буквы-двойники к преобладающему алфавиту.

    «ЭРА CПА» пишется с латинской C посреди кириллицы: на вид не отличить, а для
    сравнения это другое слово. Правим только СМЕШАННЫЕ слова — иначе честно латинское
    название вроде «SALVE» превратилось бы в кашу из двух алфавитов.
    """
    out: list[str] = []
    for word in (text or "").split(" "):
        cyr = len(_CYRILLIC.findall(word))
        lat = len(_LATIN.findall(word))
        out.append(word.translate(_LOOKALIKE_TO_CYRILLIC) if cyr and lat else word)
    return " ".join(out)


def normalize(name: str) -> str:
    """Название → сравнимая форма: без ex-приписки, звёзд, пунктуации и шумовых слов."""
    s = (name or "").strip()
    s = _EX_SUFFIX.sub(" ", s)
    s = _STARS_IN_NAME.sub(" ", s)
    s = s.replace("&", " and ")
    s = _NON_WORD.sub(" ", s).lower()
    s = _SPACES.sub(" ", s).strip()
    s = fix_homoglyphs(s)
    words = [w for w in s.split() if w not in _NOISE]
    # Если после чистки не осталось ничего осмысленного, возвращаем очищенную строку
    # целиком: лучше сравнивать по шумному имени, чем по одной букве.
    joined = " ".join(words)
    return joined if len(joined.replace(" ", "")) >= _MIN_MEANINGFUL_CORE else s


def variants(name: str) -> list[str]:
    """Формы названия, любая из которых может совпасть с другой площадкой.

    «SALVE (САЛЬВЭ)» и «БЭСЭДЭР (BESEDER)» — одно имя двумя алфавитами, и площадки
    выбирают из них по-разному. Сравнивать надо все формы, иначе половина русских
    отелей не сойдётся ни с чем.
    """
    raw = (name or "").strip()
    forms = [raw]
    inner = _PARENS.findall(raw)
    if inner:
        outer = _PARENS.sub(" ", raw).strip()
        # Приписку «(ex. …)» в альтернативы не берём: это ПРОШЛОЕ имя отеля, а не второе
        # написание нынешнего, и по нему можно сойтись с чужим объектом.
        forms += [f for f in [outer, *inner] if f and not _EX_SUFFIX.match(f.strip())]
    seen: set[str] = set()
    out: list[str] = []
    for form in forms:
        key = normalize(form)
        if key and key not in seen:
            seen.add(key)
            out.append(form)
    return out


def core(name: str) -> str:
    """Ядро названия: нормализованная форма без пробелов."""
    return normalize(name).replace(" ", "")


def cores(name: str) -> list[str]:
    """Ядра всех форм названия."""
    return [c for c in (core(v) for v in variants(name)) if c]


def tokens(name: str) -> set[str]:
    return set(normalize(name).split())


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# На сколько звёзд площадки могут разойтись, не вызывая подозрений. Живой обход по
# Грузии показал «ORBI BEACH TOWER ≈ Orbi Beach Tower (4 и 3)» и «IVERIA INN ≈ Iveria Inn
# (4 и 3)»: имя совпадает буквально, а звёзды отличаются на одну — это расхождение в
# данных площадок, а не разные объекты. Две звезды — уже повод присмотреться.
_STARS_TOLERANCE = 1


def _stars_conflict(a: HotelOffer, b: HotelOffer) -> bool:
    """True, если звёздность противоречит. Неизвестная не противоречит ничему."""
    if not (a.stars and b.stars):
        return False
    return abs(a.stars - b.stars) > _STARS_TOLERANCE


def _pair_confidence(ca: str, cb: str) -> Confidence:
    """Сила совпадения двух ядер, без учёта звёзд."""
    if len(ca) < _MIN_MEANINGFUL_CORE or len(cb) < _MIN_MEANINGFUL_CORE:
        # Ядро в один-два символа входит куда угодно и не доказывает ничего.
        return Confidence.EXACT if ca == cb else Confidence.NONE
    if ca == cb:
        return Confidence.EXACT
    contains = ca in cb or cb in ca
    if contains and min(len(ca), len(cb)) >= _MIN_CORE_FOR_CONTAINMENT:
        return Confidence.STRONG
    if contains:
        # Вхождение короткого ядра — только повод присмотреться, не более.
        return Confidence.WEAK
    return Confidence.NONE


_ORDER = {Confidence.NONE: 0, Confidence.WEAK: 1, Confidence.STRONG: 2, Confidence.EXACT: 3}


def compare(a: HotelOffer, b: HotelOffer) -> tuple[Confidence, str]:
    """Насколько уверенно два предложения относятся к одному отелю."""
    cores_a, cores_b = cores(a.hotel_name), cores(b.hotel_name)
    if not cores_a or not cores_b:
        return Confidence.NONE, "пустое название после нормализации"

    best = Confidence.NONE
    for ca in cores_a:
        for cb in cores_b:
            level = _pair_confidence(ca, cb)
            if _ORDER[level] > _ORDER[best]:
                best = level

    if best is Confidence.NONE:
        # Последний шанс — совпадение по значимым словам. Помогает там, где порядок слов
        # разный, а ядра из-за этого не вкладываются друг в друга.
        sim = _jaccard(tokens(a.hotel_name), tokens(b.hotel_name))
        if sim >= _WEAK_JACCARD:
            return Confidence.WEAK, f"частичное совпадение слов ({sim:.0%})"
        return Confidence.NONE, ""

    if _stars_conflict(a, b):
        # Название сошлось, а звёзды разные — либо ошибка данных, либо разные объекты
        # сети. Пропуском такое объявлять нельзя, только в проверку.
        return Confidence.WEAK, f"названия совпали, звёзды разошлись ({a.stars} и {b.stars})"

    reason = {
        Confidence.EXACT: "названия совпали",
        Confidence.STRONG: "одно название содержится в другом",
        Confidence.WEAK: "названия похожи, но совпадение короткое",
    }[best]
    return best, reason


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
    review: list[HotelMatch] = field(default_factory=list)
    only_reference: list[HotelOffer] = field(default_factory=list)
    only_checked: list[HotelOffer] = field(default_factory=list)

    @property
    def matched_share(self) -> float:
        """Доля отелей эталона, для которых нашлась пара. Низкая доля — повод не верить
        отчёту целиком: скорее сломался матчинг, чем пропало полкаталога."""
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
                confidence, reason = compare(ref, cand)
                if confidence is level:
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
