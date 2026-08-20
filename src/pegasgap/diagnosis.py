"""Разбор причины отельного пропуска по внутренним справочникам.

Превращает находку «этого отеля у нас нет» в указание, что именно делать и кому:

* **нет в справочнике Слетать** — заводить отель;
* **есть, но у оператора нет линковки** — связывать каталоги;
* **линкован, а тура не было** — справочники в порядке, смотреть наличие и логи поиска.

Модуль чистый: на вход готовый справочник и множество связанных ID, наружу — диагнозы.
Загрузку данных делают `catalog` (HTTP) и `linking` (SQL), и разделение здесь не
формальное: логика решает судьбу тикета, поэтому должна проверяться офлайн, целиком и
без доступов.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from difflib import SequenceMatcher

from pegasgap.catalog import CatalogHotel
from pegasgap.gaps import MATCH_COLLAPSE_MARKER
from pegasgap.linking import Direction, LinkSet
from pegasgap.matching import Confidence, ascii_core, compare, core, normalize, refresh_aliases, tokens
from pegasgap.models import GapKind, HotelDiagnosis, HotelGap, ScanResult

log = logging.getLogger("pegasgap.diagnosis")


class CatalogIndex:
    """Справочник, подготовленный к поиску по названию.

    Точные совпадения ищутся по индексу ядра имени, остальное — перебором. Перебор по
    одиннадцати тысячам записей на каждый пропуск был бы расточителен, а индекс закрывает
    подавляющее большинство случаев одним обращением к словарю.
    """

    def __init__(self, hotels: list[CatalogHotel]) -> None:
        refresh_aliases()
        self.hotels = hotels
        self._by_core: dict[str, list[CatalogHotel]] = defaultdict(list)
        for hotel in hotels:
            key = core(hotel.name)
            if key:
                self._by_core[key].append(hotel)

    def __bool__(self) -> bool:
        return bool(self.hotels)

    def find(self, gap: HotelGap) -> tuple[CatalogHotel | None, Confidence]:
        """Лучший кандидат справочника для отеля из находки.

        Сила совпадения дополнительно приземляется `_temper`: справочный вердикт — это
        УТВЕРЖДЕНИЕ тождества (id уходит в ссылки и в «заведён как…»), и держится оно
        на более строгой планке, чем сопоставление выдач, где лишняя пара всего лишь
        прячет находку.
        """
        probe = gap_as_offer(gap)
        key = core(gap.hotel_name)
        if not key:
            return None, Confidence.NONE

        best: tuple[CatalogHotel | None, Confidence] = (None, Confidence.NONE)
        for candidate in self._by_core.get(key, []):
            confidence, _ = compare(probe, candidate.as_offer())
            if confidence is Confidence.EXACT:
                return candidate, confidence
            best = _better(best, (candidate, confidence))
        if best[1].comparable:
            return best[0], _temper(gap.hotel_name, best[0], best[1])

        for candidate in self.hotels:
            confidence, _ = compare(probe, candidate.as_offer())
            if confidence is Confidence.STRONG:
                return candidate, _temper(gap.hotel_name, candidate, confidence)
            best = _better(best, (candidate, confidence))
        return best[0], _temper(gap.hotel_name, best[0], best[1])


_ORDER = {Confidence.NONE: 0, Confidence.WEAK: 1, Confidence.STRONG: 2, Confidence.EXACT: 3}


def _better(a: tuple[CatalogHotel | None, Confidence],
            b: tuple[CatalogHotel | None, Confidence]) -> tuple[CatalogHotel | None, Confidence]:
    return b if _ORDER[b[1]] > _ORDER[a[1]] else a


def _temper(probe_name: str, hotel: CatalogHotel | None,
            confidence: Confidence) -> Confidence:
    """Не утверждать тождество по вхождению одиночного слова.

    Живой случай: «DOGAN PARADISE BEACH HOTEL» опознался в справочнике как «Paradise
    Apart» — ядро «paradise» проходит порог длины вхождения, но это одно генерическое
    слово, и по нему в ссылку и в «заведён как…» уехал ЧУЖОЙ отель. Для сопоставления
    выдач щедрость безопасна (лишняя пара прячет находку), а справочный вердикт —
    утверждение, и вхождению нужно либо два значимых слова, либо длинное ядро.
    """
    if hotel is None or confidence is not Confidence.STRONG:
        return confidence
    ca, cb = core(probe_name), core(hotel.name)
    if not ca or not cb or ca == cb or (ca not in cb and cb not in ca):
        return confidence
    short_name = probe_name if len(ca) <= len(cb) else hotel.name
    short_core = min(ca, cb, key=len)
    if len(normalize(short_name).split()) >= 2 or len(short_core) >= 12:
        return confidence
    return Confidence.WEAK


def gap_as_offer(gap: HotelGap):
    """Находка в виде предложения — чтобы сравнивать тем же матчером, что и выдачу."""
    from decimal import Decimal

    from pegasgap.models import HotelOffer
    return HotelOffer(provider="gap", hotel_name=gap.hotel_name,
                      price=gap.reference_price or Decimal(1), stars=gap.stars)


def diagnose_gap(gap: HotelGap, index: CatalogIndex, links: LinkSet) -> None:
    """Проставить одной находке диагноз и подсказку. Меняет объект на месте."""
    if not index:
        gap.diagnosis = HotelDiagnosis.UNKNOWN
        return

    hotel, confidence = index.find(gap)

    # Вердикт живёт в `diagnosis` и печатается отдельной колонкой, поэтому комментарий его
    # НЕ повторяет — только добавляет то, чего в вердикте нет: под каким именем и под каким
    # идентификатором отель лежит у нас. С этими двумя данными находку можно открыть в
    # справочнике не переспрашивая.
    if hotel is None or confidence is Confidence.NONE:
        gap.diagnosis = HotelDiagnosis.NOT_IN_CATALOG
        gap.note = "по названию ничего похожего не нашлось"
        return

    gap.catalog_id, gap.catalog_name = hotel.id, hotel.name
    where = f"«{hotel.name}» id {hotel.id}"

    if not confidence.comparable:
        # Похожий отель нашёлся, но недостаточно уверенно. Объявить «нет линковки» на таком
        # основании нельзя: скорее всего он у нас есть под другим написанием.
        gap.diagnosis = HotelDiagnosis.UNCERTAIN
        gap.note = f"похож на {where}, но совпадение неуверенное"
        return

    if not links.available:
        gap.diagnosis = HotelDiagnosis.IN_CATALOG_UNCHECKED
        gap.note = where
        return

    if links.is_disabled(hotel.id):
        # Проверяется раньше линковки: связь может быть в порядке, но выключенный отель
        # не покажут всё равно, и чинить надо не связь.
        gap.diagnosis = HotelDiagnosis.CATALOG_DISABLED
        gap.note = f"{where} — выключен в справочнике Слетать"
    elif links.has(hotel.id):
        gap.diagnosis = HotelDiagnosis.LINKED_NO_OFFER
        gap.note = f"{where} — связан с каталогом оператора"
    else:
        gap.diagnosis = HotelDiagnosis.NOT_LINKED
        gap.note = f"{where} — связи с каталогом оператора нет"


# Какая доля пропущенных отелей должна уверенно опознаться в нашем справочнике, чтобы
# вердикт «матчинг развалился» считался опровергнутым.
MIN_CATALOG_RESOLVED_SHARE = 0.6

# Диагнозы, означающие «отель уверенно найден в справочнике Слетать». UNCERTAIN сюда не
# входит намеренно: неуверенное совпадение как раз и есть симптом слабой нормализации.
_RESOLVED = frozenset({
    HotelDiagnosis.IN_CATALOG_UNCHECKED,
    HotelDiagnosis.LINKED_NO_OFFER,
    HotelDiagnosis.NOT_LINKED,
})


def refute_match_collapse(scan: ScanResult, targets: list[HotelGap]) -> None:
    """Снять вердикт «матчинг развалился», если справочник доказывает обратное.

    Низкая доля сопоставленных отелей двусмысленна: она одинаково выглядит и когда
    развалилась нормализация имён, и когда у оператора действительно нет почти ничего из
    того, что показал эталон. `detect` выбирает осторожную версию, потому что других
    данных у него нет — справочник появляется только здесь.

    А справочник эту двусмысленность снимает. Опознание в нём идёт ТЕМ ЖЕ матчером, что и
    сопоставление выдач: если имена эталона уверенно находят свои карточки у нас, то
    нормализация заведомо жива, и объяснить нечем, кроме отсутствия предложений.

    Живой прогон по России: эталон показал 53 отеля, сопоставились 2, и прогон был
    объявлен недостоверным — а 43 из 50 пропущенных при этом нашлись в справочнике по
    имени. Полсотни настоящих находок уходили в мусор из-за осторожности, которой было
    чем возразить.

    Снимается ТОЛЬКО этот вердикт. Все прочие проблемы прогона остаются в силе, и если
    была хоть одна — прогон так и останется недостоверным.
    """
    stale = [p for p in scan.problems if MATCH_COLLAPSE_MARKER in p]
    if not stale or not targets:
        return

    resolved = sum(1 for g in targets if g.diagnosis in _RESOLVED)
    share = resolved / len(targets)
    if share < MIN_CATALOG_RESOLVED_SHARE:
        return

    for problem in stale:
        scan.problems.remove(problem)
    scan.notes.append(
        f"мало сопоставленных отелей, но {resolved} из {len(targets)} пропущенных "
        f"({share:.0%}) уверенно нашлись в справочнике Слетать — значит имена читаются "
        f"и дело не в матчинге, а в отсутствии предложений у оператора")


def reverse_index(their_hotels: dict[int, dict]) -> CatalogIndex:
    """Словарь витрины — в тот же индекс, каким разбираются наши пропуски.

    Матчер единый на весь инструмент намеренно: иначе один и тот же отель считался бы
    то тем же, то другим в зависимости от того, где его сравнивают.
    """
    hotels: list[CatalogHotel] = []
    for hid, ref in their_hotels.items():
        name = str(ref.get("name") or "").strip()
        if not name:
            continue
        try:
            stars = int(ref.get("stars")) or None
        except (TypeError, ValueError):
            stars = None
        hotels.append(CatalogHotel(id=int(hid), name=name, stars=stars, town_id=None))
    return CatalogIndex(hotels)


# Слова-места из названий отелей. Для опознания отеля общее место ничего не значит:
# «BODRUM BEACH RESORT» совпадает по «bodrum» с каждым отелем Бодрума, и на живом списке
# один такой кандидат предлагался двум разным отелям сразу.
_PLACE_TOKENS = frozenset({
    "istanbul", "стамбул", "antalya", "анталья", "alanya", "алания", "bodrum", "бодрум",
    "side", "сиде", "kemer", "кемер", "belek", "белек", "marmaris", "мармарис",
    "fethiye", "фетхие", "kusadasi", "кушадасы", "bursa", "бурса", "izmir", "измир",
    "cappadocia", "каппадокия", "sirkeci", "laleli", "sultanahmet", "fatih", "taksim",
    "harbiye",
})


def _plausible_candidate(gap: HotelGap, hotel: CatalogHotel) -> bool:
    """Стоит ли шаткое совпадение показывать человеку как кандидата.

    Матчер отдаёт WEAK и за осмысленное сходство, и за случайное вхождение трёх букв:
    живой список предлагал «ABEL» для «Annabella Park», «BIR» для «Birbey» и один
    «ALA HOTEL» сразу четырём отелям — такие кандидаты не помогают сверке, а хоронят
    доверие к колонке.

    Кандидат осмыслен, если совпала хотя бы половина значимых слов (и среди общих есть
    не-топоним), либо написания близки в общем алфавите. Нечёткое сравнение коротких ядер
    врёт («birbei» ~ «bir» даёт 0.67, «evsen» ~ «seven» — 0.8), поэтому ему дополнительно
    нужны длина от пяти и общая первая буква.
    """
    ours, theirs = tokens(gap.hotel_name), tokens(hotel.name)
    shared = (ours & theirs) - _PLACE_TOKENS
    if shared and len(ours & theirs) / len(ours | theirs) >= 0.5:
        return True
    a, b = ascii_core(gap.hotel_name), ascii_core(hotel.name)
    if min(len(a), len(b)) < 5 or not (a and b and a[0] == b[0]):
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.6


def diagnose_reverse(scan: ScanResult, index: CatalogIndex) -> None:
    """Причина «отеля нет на Турвизоре» — по словарю самой витрины.

    Живой случай, из-за которого разбор появился: Atlantis Royal Hotel значился «нет на
    Турвизоре», при этом в СЛОВАРЕ витрины он есть (ATLANTIS ROYAL, id 71351) — а её
    поиск, прижатый к отелю, честно возвращает ноль туров и на широкое окно. То есть
    находка была верной, но без причины читалась как ошибка инструмента.

    Заодно ставится их id отеля: ссылка «на Турвизоре» прижимается к отелю и открывает
    ровно ту пустую выдачу, которая находку и доказывает.
    """
    targets = scan.gaps_of(GapKind.REVERSE)
    if not targets or not index:
        return
    counts: dict[str, int] = defaultdict(int)
    for gap in targets:
        hotel, confidence = index.find(gap)
        reason = compare(gap_as_offer(gap), hotel.as_offer())[1] if hotel else ""
        if hotel is not None and confidence.comparable:
            gap.diagnosis = HotelDiagnosis.REF_LISTED_NO_TOURS
            gap.reference_hotel_id = hotel.id
            gap.note = (f"у витрины заведён как «{hotel.name}» (id {hotel.id}), "
                        f"но туров на эти даты её поиск не вернул")
        elif hotel is not None and reason.startswith("названия совпали"):
            # Имя совпало буквально, разошлась только звёздность — данные о звёздах у
            # площадок расходятся сплошь и рядом, и это почти наверняка тот же отель.
            gap.diagnosis = HotelDiagnosis.REF_MAYBE_NAMED
            gap.reference_hotel_id = hotel.id
            gap.note = (f"у витрины это же имя — «{hotel.name}» (id {hotel.id}), "
                        f"{reason.replace('названия совпали, ', '')}; "
                        f"почти наверняка тот же отель, туров у него нет")
        elif hotel is not None and _plausible_candidate(gap, hotel):
            gap.diagnosis = HotelDiagnosis.REF_MAYBE_NAMED
            gap.reference_hotel_id = hotel.id
            gap.note = (f"возможно, у витрины это «{hotel.name}» (id {hotel.id}) — "
                        f"совпадение неуверенное, сверить название")
        else:
            gap.diagnosis = HotelDiagnosis.REF_NOT_IN_DICTIONARY
            gap.note = "в словаре витрины ничего убедительно похожего — отель есть только у нас"
        counts[gap.diagnosis.value] += 1
    log.info("диагноз по %d обратным находкам: %s", len(targets), dict(counts))


def diagnose(scan: ScanResult, catalog: list[CatalogHotel], links: LinkSet,
             direction: Direction | None = None) -> ScanResult:
    """Разобрать причины всех отельных пропусков прогона.

    Диагноз ставится только классу «отельный пропуск»: для полного пропуска и для отказа
    оператора справочники ни при чём, а расхождение цены к ним отношения не имеет.
    """
    index = CatalogIndex(catalog)
    targets = scan.gaps_of(GapKind.HOTEL)
    for gap in targets:
        diagnose_gap(gap, index, links)
    if targets:
        counts: dict[str, int] = defaultdict(int)
        for gap in targets:
            counts[gap.diagnosis.value] += 1
        log.info("диагноз по %d отельным пропускам: %s", len(targets), dict(counts))
        refute_match_collapse(scan, targets)
    if not index:
        scan.notes.append("справочник отелей Слетать недоступен — причины пропусков "
                          "не разобраны")
    elif not links.available and targets:
        scan.notes.append("линковка оператора не проверялась (нет доступа к справочникам) "
                          "— различить «нет линковки» и «нет наличия» не удалось")
    _note_direction(scan, direction)
    return scan


def _note_direction(scan: ScanResult, direction: Direction) -> None:
    """Отметить, что направление у оператора вообще не заведено.

    Это не отельная причина, а причина всего прогона: правки по отелям тут бесполезны,
    пока пары «город вылета → страна» нет в справочнике направлений оператора. Молчание
    при недоступной базе обязательно — «не смотрели» не то же самое, что «не заведено».
    """
    if direction is None or not direction.available:
        return
    where = f"{scan.params.departure_city} → {scan.params.destination_country}"
    if not direction.known:
        scan.notes.append(f"направление {where} у оператора «{scan.operator}» "
                          f"не заведено в справочнике — предложений тут и не будет")
    elif not direction.serves(scan.params.search_mode):
        mode = "без перелёта" if scan.params.search_mode == "hotels" else "с перелётом"
        scan.notes.append(f"направление {where} у оператора «{scan.operator}» заведено, "
                          f"но не в режиме «{mode}»")
