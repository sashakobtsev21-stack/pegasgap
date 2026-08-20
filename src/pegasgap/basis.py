"""Основа сравнения цен: одинаковое питание и не противоречащие номера.

Цена тура складывается из даты, питания и номера. Даты сравнение уже прижало; без
питания и номера «на Слетать дороже на 9%» всё ещё может означать «AI против RO» или
«сюит против промо-номера» — то есть разницу состава, а не площадок.

**Питание** обе стороны отдают кодами одной системы (RO/BB/HB/FB/AI/UAI): шлюз — строкой
в каждой строке выдачи, витрина — числовым `ml`, который раскрывается её же словарём
`type=meal`. Нормализация сводит оба написания к базовому коду; плюс-варианты («HB+»)
схлопываются к базе — вторая площадка их не различает, и строгое равенство лишь
разорвало бы сравнимые пары.

**Номера** — свободный текст, и сравнивать их можно только на опровержение: снять
находку, когда категории заведомо разные (промо против стандарта), и НЕ снимать, когда
сигнала нет. «Твин» против «Standard Room» — не противоречие: твин может быть тем самым
стандартом, и слова о конфигурации кроватей (твин/двухместный) категорией не считаются.

Модуль чистый: без сети и провайдеров, чтобы правила проверялись офлайн — они решают,
что попадёт в отчёт.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher

from pegasgap.matching import translit
from pegasgap.models import DayOffer

# Базовые коды питания в порядке «от пустого к полному». Обе площадки живут в этой
# системе, расхождения только в написании.
_MEAL_ALIASES = {
    "RO": "RO", "OB": "RO", "ROOMONLY": "RO", "ONLYROOM": "RO", "БЕЗПИТАНИЯ": "RO",
    "BB": "BB", "ЗАВТРАК": "BB", "BEDANDBREAKFAST": "BB", "BEDBREAKFAST": "BB",
    "HB": "HB", "ПОЛУПАНСИОН": "HB", "HALFBOARD": "HB",
    "FB": "FB", "ПОЛНЫЙПАНСИОН": "FB", "FULLBOARD": "FB",
    "AI": "AI", "ALL": "AI", "ALLINCLUSIVE": "AI", "ВСЕВКЛЮЧЕНО": "AI",
    "ВСЁВКЛЮЧЕНО": "AI",
    "UAI": "UAI", "ULTRAALLINCLUSIVE": "UAI", "УЛЬТРАВСЕВКЛЮЧЕНО": "UAI",
    "УЛЬТРАВСЁВКЛЮЧЕНО": "UAI",
}

_NON_ALNUM = re.compile(r"[^0-9A-ZА-ЯЁ]+")


def normalize_meal(text: str | None) -> str | None:
    """Питание в базовый код. None — не распознали, такое предложение не сравниваем.

    Молчание честнее догадки: неизвестное питание, засчитанное «как-нибудь», вернуло бы
    ровно те сравнения разного состава, ради снятия которых всё и делается.
    """
    s = _NON_ALNUM.sub("", str(text or "").upper())
    if not s:
        return None
    # «HB+», «AI PLUS» и прочие плюс-варианты — к базе: вторая сторона их не различает.
    s = s.replace("PLUS", "").rstrip("+")
    return _MEAL_ALIASES.get(s)


# Стемы КАТЕГОРИЙ номера. Намеренно только категории: слова о конфигурации кроватей
# (твин, двухместный, с двумя кроватями) в списке отсутствуют — «Твин» легко оказывается
# тем же стандартом, и опровержение по нему выдумывало бы разницу.
_CATEGORY_STEMS: dict[str, tuple[str, ...]] = {
    "standard": ("standard", "std", "стандарт"),
    "promo": ("promo", "промо"),
    "economy": ("econom", "эконом"),
    "suite": ("suite", "сюит", "сьют", "люкс"),
    "studio": ("studio", "студи"),
    "family": ("family", "семейн"),
    "deluxe": ("deluxe", "делюкс", "dlx"),
    "superior": ("superior", "супериор"),
    "apartment": ("apart", "апарт"),
    "villa": ("villa", "вилл"),
    "bungalow": ("bungalow", "бунгало"),
}

_SPLIT = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)


def room_tags(name: str | None) -> frozenset[str]:
    """Категории, которые название номера сообщает о себе. Пусто — сигнала нет."""
    tokens = [t for t in _SPLIT.split(str(name or "").lower()) if t]
    # Совпадение в обе стороны: «эконом» длиннее стема «econom» не бывает, а вот «eco»
    # КОРОЧЕ него — живой номер «eco room» без обратной проверки оставался без категории.
    # Три буквы минимум, иначе «e» пометило бы всё подряд.
    return frozenset(
        category
        for category, stems in _CATEGORY_STEMS.items()
        if any(token.startswith(stem) or (len(token) >= 3 and stem.startswith(token))
               for token in tokens for stem in stems)
    )


def rooms_differ(a: str | None, b: str | None) -> bool:
    """Заведомо ли номера разных категорий. Только опровержение, без домыслов.

    True — лишь когда ОБА названия несут категорию и категории не пересекаются:
    «Promo Room» против «Стандартный номер». Нет сигнала хотя бы с одной стороны —
    противоречия нет: снять находку из-за непрочитанного названия значило бы прятать
    настоящие расхождения за бедностью словаря.
    """
    ta, tb = room_tags(a), room_tags(b)
    return bool(ta) and bool(tb) and not (ta & tb)


# Порог нечёткого совпадения названий номеров в общем алфавите. «Superior Room Sea
# View» против «superior swim up room sea view» дают 0.87, чужие названия — около нуля.
_ROOM_FUZZY = 0.8


def _room_key(name: str | None) -> str:
    return " ".join(_SPLIT.split(translit(str(name or "").lower()))).strip()


def rooms_alike(a: str | None, b: str | None) -> bool:
    """Положительное свидетельство, что это ОДИН номер, — не отсутствие противоречия.

    Разница принципиальная: `rooms_differ` умеет только опровергать, и пара «Jasmine
    Pool View» против «camelia family superior» проходила у него как «не противоречит» —
    ни одного словарного слова категории, опровергать нечем. Но это заведомо разные
    номера, и утверждение «цена расходится» на такой паре не стоит ничего.

    Один номер — это либо одинаковый НАБОР категорий (не пересечение: {family, suite}
    против {family, superior} пересекаются по family, оставаясь разными номерами), либо
    почти одинаковое написание в общем алфавите.
    """
    ta, tb = room_tags(a), room_tags(b)
    if ta and tb and ta == tb:
        return True
    ka, kb = _room_key(a), _room_key(b)
    if not ka or not kb:
        return False
    return SequenceMatcher(None, ka, kb).ratio() >= _ROOM_FUZZY


def add_day_offer(target: dict[date, list[DayOffer]], day: date | None,
                  price: Decimal | None, meal: str | None, room: str | None,
                  tour_id: str | None = None) -> None:
    """Добавить предложение в разрез, храня минимум на каждый состав.

    Ключ дедупликации — (питание, номер): двух цен на один состав в разрезе не бывает,
    остаётся меньшая. Общий помощник обоим провайдерам, чтобы правило не разъехалось.
    """
    if day is None or price is None or price <= 0:
        return
    bucket = target.setdefault(day, [])
    for i, offer in enumerate(bucket):
        if offer.meal == meal and offer.room == room:
            if price < offer.price:
                bucket[i] = DayOffer(price=price, meal=meal, room=room, tour_id=tour_id)
            return
    bucket.append(DayOffer(price=price, meal=meal, room=room, tour_id=tour_id))
