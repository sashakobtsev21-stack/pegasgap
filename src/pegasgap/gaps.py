"""Классификация расхождений — ядро инструмента.

На вход результаты двух площадок по одному запросу, на выход список находок,
разложенных по вероятной причине. Модуль чистый: без браузера, сети и БД — всё
тестируется офлайн на собранных структурах.

Главный принцип: **находка должна быть проверяемой**. Каждая попадает в отчёт только
если её нельзя объяснить сбоем самого инструмента. Всё, что можно объяснить — неприменённый
фильтр оператора, развалившийся матчинг, отказ эталона — уходит в `problems`, и такой
прогон помечается недостоверным целиком. Лучше отдать пустой отчёт, чем список выдуманных
пропусков: доверие теряется один раз.
"""

from __future__ import annotations

import os
import statistics

from pegasgap.matching import MatchResult, match_hotels
from pegasgap.models import (
    PEGAS,
    GapKind,
    HotelGap,
    HotelOffer,
    OperatorStatus,
    ProviderResult,
    ScanResult,
    SearchParams,
    is_not_applicable_error,
)
from pegasgap.names import find_operator, operator_matches

# Насколько цена может отличаться от СИСТЕМАТИЧЕСКОГО сдвига, прежде чем это находка.
# Сравнивается именно отклонение от медианы, а не сырая разница: витрины работают на
# разной базе цены (агентская против розничной), и постоянная разница в несколько
# процентов — свойство площадок, а не проблема отеля. Интересен тот отель, который
# выбивается из общего сдвига.
PRICE_TOLERANCE_PCT = 7.0

# Во сколько медианных абсолютных отклонений укладывается «обычное» расхождение. Ширина
# полосы берётся по самим данным, а не только из константы выше: на живых прогонах разница
# цен расползается на 4–13% при медиане 9%, и фиксированный допуск объявлял выбросами
# отели, где цены совпали ДО РУБЛЯ, — формально верно, а по смыслу дико. Три MAD — обычный
# робастный порог (≈2σ для нормального распределения), устойчивый к самим выбросам.
MAD_MULTIPLIER = 3.0
# Меньше этого числа пар разброс оценивать не по чему — работает голый допуск.
MIN_PAIRS_FOR_SPREAD = 5

# Меньше этого числа пар не считается и сам сдвиг, а значит цену сравнивать не с чем.
# Находка по цене — это «отель выбивается из ОБЫЧНОГО для прогона расхождения», и при двух
# парах обычное расхождение определяется одной из этих же двух точек: медиана совпадает с
# наблюдением, и каждая пара оказывается выбросом относительно другой. Живой прогон по
# России дал ровно это — две сопоставленные пары и обе в находках.
MIN_PAIRS_FOR_PRICE = 3

# Ниже этой доли сопоставленных отелей матчинг считается развалившимся. Если из выдачи
# эталона удалось узнать меньше трети отелей — куда правдоподобнее, что сломалась
# нормализация имён, чем что у оператора внезапно исчезло две трети каталога.
MIN_MATCHED_SHARE = 0.34

# По этой метке диагностика узнаёт вердикт о развале матчинга, чтобы снять его, если
# справочник докажет обратное (см. diagnosis.refute_match_collapse). Строку не менять
# отдельно от той проверки — они парные.
MATCH_COLLAPSE_MARKER = "похоже на сбой матчинга"

# Во сколько раз выдачи сторон могут отличаться по размеру, прежде чем сравнение теряет
# смысл. Доля сопоставленных этого не ловит: если эталон отдал 14 отелей из сотен, они
# все могут сопоставиться прекрасно — и «отельных пропусков нет» будет означать лишь
# «мы посмотрели четырнадцать отелей», а не «всё на месте». Ограничение отдельное.
MAX_COVERAGE_RATIO = 5.0

# Систематическая разница цен, выше которой сравнение перестаёт быть сравнением. Разная
# база (агентская против розничной) даёт единицы процентов; полсотни означает, что стороны
# считают РАЗНОЕ — иной состав тура, другое число туристов, другая длительность. Живой
# прогон по Вьетнаму дал ровно −48% на каждом из сорока пяти отелей: такая равномерность
# исключает случайность и выдаёт различие в определении цены, а не в самой цене.
MAX_PLAUSIBLE_OFFSET_PCT = 25.0

# Показывать ли «обратные пропуски» — отели, которые есть у нас и отсутствуют на витрине.
# По умолчанию НЕТ. Инструмент отвечает на вопрос «чего нет у нас», а обратное
# направление — его зеркало, полезное лишь как санити-чек матчинга. На живом обходе оно
# дало 587 находок из 641, полностью похоронив настоящие: витрина показывает по оператору
# выборку, а мы отдаём каталог целиком, и каждый лишний отель у нас становился «находкой».
REPORT_REVERSE = (os.environ.get("PEGASGAP_REPORT_REVERSE") or "").strip().lower() in (
    "1", "true", "yes")

# Сколько отелей показать как пример в находке «полный пропуск».
_EXAMPLES = 3


def operator_status(result: ProviderResult | None, operator: str = PEGAS) -> OperatorStatus:
    """Что площадка сказала про оператора в этом поиске.

    Различие «туров нет» и «оператор не отвечает» — самое ценное, что даёт панель
    операторов Слетать: в первом случае плагин отработал и ответил, во втором не ответил
    вовсе. Причины и место разбора у них разные, и схлопывать их в «нет цен» нельзя.
    """
    if result is None or not result.success:
        return OperatorStatus.UNKNOWN

    if find_operator(result.operators_not_responding, operator):
        return OperatorStatus.NOT_RESPONDING
    if find_operator(result.operators_no_tours, operator):
        return OperatorStatus.NO_TOURS

    priced = [o for o in result.offers if operator_matches(o.operator, operator)]
    if priced or find_operator(result.operators_available, operator):
        return OperatorStatus.PRICED
    # Площадка не раскрыла разрез по операторам, но поиск был отфильтрован этим оператором
    # и отели вернулись — значит предложения у него есть.
    if result.hotel_offers:
        return OperatorStatus.PRICED
    return OperatorStatus.ABSENT


def _collect_problems(
    reference: ProviderResult | None,
    checked: ProviderResult | None,
    match: MatchResult | None,
) -> list[str]:
    """Причины не доверять этому прогону. Пусто = находкам можно верить."""
    problems: list[str] = []

    if reference is None:
        problems.append("эталон: результата нет")
    elif not reference.success and not is_not_applicable_error(reference.error):
        problems.append(f"эталон: поиск не удался — {reference.error}")

    # Проверяемая сторона: ЛЮБОЙ несостоявшийся поиск — проблема, включая «площадка не
    # обслуживает такой запрос». На стороне эталона такой отказ безобиден (просто нечего
    # сравнивать), а здесь он означает, что проверку выполнить не удалось. Молчать о нём
    # нельзя: пустая выдача из-за нашего же сбоя выглядит точь-в-точь как пропуск оператора.
    if checked is None:
        problems.append("проверяемая: результата нет")
    elif not checked.success:
        problems.append(f"проверяемая: проверка не выполнена — {checked.error}")

    for role, res in (("эталон", reference), ("проверяемая", checked)):
        if res is None:
            continue
        # Фильтр по оператору не подтвердился: карточки отелей не несут имени ТО, значит
        # цены в них — минимум по всем операторам, и любой вывод о пропусках будет ложным.
        if res.operator_filter_verified is False:
            problems.append(f"{role}: фильтр по оператору не применился — данные непригодны")

    # Обрезанная выдача проверяемой стороны — прямой источник выдуманных пропусков: отель
    # мог просто не догрузиться. На стороне эталона обрезка безопаснее (мы всего лишь
    # увидим меньше находок), но полнотой отчёта тоже жертвует, поэтому отмечаем обе.
    if checked is not None and checked.truncated:
        problems.append("проверяемая: выдача получена не целиком — часть «пропусков» может "
                        "оказаться просто недогруженными отелями")
    if reference is not None and reference.truncated:
        problems.append("эталон: выдача получена не целиком — часть пропусков могла не попасть в отчёт")

    if reference and checked and reference.success and checked.success:
        currencies = {o.currency for o in reference.hotel_offers} | {
            o.currency for o in checked.hotel_offers}
        if len(currencies) > 1:
            problems.append(f"валюты площадок различаются: {sorted(currencies)}")

    if match is not None and match.matched_share < MIN_MATCHED_SHARE:
        problems.append(
            f"сопоставлено лишь {match.matched_share:.0%} отелей эталона — {MATCH_COLLAPSE_MARKER}, "
            f"а не на реальные пропуски")

    return problems


def _coverage_is_lopsided(reference_count: int, checked_count: int) -> bool:
    """Различаются ли объёмы выдач настолько, что сравнивать их нельзя.

    Обе стороны читаются по-разному (шлюз отдаёт выдачу целиком, витрина — постранично),
    поэтому расхождение в объёме — обычное дело и означает недочитанную ленту, а не
    исчезнувший каталог.
    """
    if not reference_count or not checked_count:
        return False
    lo, hi = sorted((reference_count, checked_count))
    return hi > MAX_COVERAGE_RATIO * lo


def _full_gap(hotels: list[HotelOffer], status: OperatorStatus) -> HotelGap:
    """Одна находка на весь запрос: у эталона предложения есть, у нас — ни одного."""
    kind = (GapKind.NOT_RESPONDING if status is OperatorStatus.NOT_RESPONDING
            else GapKind.FULL)
    examples = ", ".join(h.hotel_name for h in sorted(hotels, key=lambda h: h.price)[:_EXAMPLES])
    cheapest = min((h.price for h in hotels), default=None)
    note = f"на эталоне {len(hotels)} отел(ей) у оператора"
    if examples:
        note += f"; например: {examples}"
    if status is OperatorStatus.NOT_RESPONDING:
        note += ". Площадка сообщила, что оператор не отвечает"
    elif status is OperatorStatus.NO_TOURS:
        note += ". Площадка ответила «туров нет»"
    elif status is OperatorStatus.ABSENT:
        note += ". Оператора нет в выдаче площадки вовсе"
    return HotelGap(
        kind=kind, hotel_name="— весь запрос —", reference_price=cheapest, note=note,
    )


def _price_gaps(match: MatchResult, tolerance_pct: float) -> tuple[list[HotelGap], float | None]:
    """Отели, чья цена выбивается из обычного для этого прогона расхождения.

    Полоса «нормального» строится по самим данным: медиана расхождений задаёт центр,
    медианное абсолютное отклонение — ширину. Иначе на широком разбросе (а он широкий:
    живой прогон дал 4–13% при медиане 9%) в находки попадали бы отели с точным
    совпадением цены — формально они дальше всех от медианы, а по сути ничем не
    примечательны.
    """
    diffs = [
        float((m.checked.price - m.reference.price) / m.reference.price * 100)
        for m in match.pairs if m.reference.price
    ]
    if not diffs:
        return [], None
    offset = statistics.median(diffs)
    # Стороны считают разное — сравнивать нечего. Сдвиг возвращаем (он и есть симптом),
    # а находок не даём: они были бы ранжированием шума.
    if abs(offset) > MAX_PLAUSIBLE_OFFSET_PCT:
        return [], offset
    # Слишком мало пар, чтобы знать «обычное» расхождение. Сдвиг возвращаем — он и сам по
    # себе сведение, — но находок не даём: они были бы сравнением пары с ней же самой.
    if len(diffs) < MIN_PAIRS_FOR_PRICE:
        return [], offset
    band = tolerance_pct
    if len(diffs) >= MIN_PAIRS_FOR_SPREAD:
        mad = statistics.median([abs(d - offset) for d in diffs])
        band = max(tolerance_pct, MAD_MULTIPLIER * mad)

    out: list[HotelGap] = []
    for m in match.pairs:
        if not m.reference.price:
            continue
        diff = float((m.checked.price - m.reference.price) / m.reference.price * 100)
        deviation = diff - offset
        if abs(deviation) <= band:
            continue
        out.append(HotelGap(
            kind=GapKind.PRICE,
            hotel_name=m.reference.hotel_name,
            stars=m.reference.stars or m.checked.stars,
            resort=m.reference.destination,
            reference_price=m.reference.price,
            checked_price=m.checked.price,
            currency=m.reference.currency,
            matched_name=m.checked.hotel_name,
            note=(f"разница {diff:+.1f}% при обычной для прогона {offset:+.1f}% "
                  f"(±{band:.1f}) — отклонение {deviation:+.1f}%"),
        ))
    out.sort(key=lambda g: abs(g.diff_pct or 0), reverse=True)
    return out, offset


def detect(
    params: SearchParams,
    reference: ProviderResult | None,
    checked: ProviderResult | None,
    operator: str = PEGAS,
    tolerance_pct: float = PRICE_TOLERANCE_PCT,
) -> ScanResult:
    """Разложить один прогон по классам находок.

    Порядок разбора:

    1. Если эталон ничего не показал — сравнивать не с чем. Это не пропуск: отсутствие
       предложений у эталона не означает, что они должны быть у нас.
    2. Если поиск на проверяемой стороне не состоялся — тоже не находка, а наша поломка.
    3. Если у нас пусто, а у эталона нет — одна находка на весь запрос, с указанием,
       ответила площадка «туров нет» или не ответила вовсе.
    4. Иначе сопоставляем отели и разбираем поштучно.
    """
    ref_status = operator_status(reference, operator)
    chk_status = operator_status(checked, operator)
    ref_hotels = reference.hotel_offers if reference else []

    result = ScanResult(
        params=params, operator=operator,
        reference=reference, checked=checked,
        reference_status=ref_status, checked_status=chk_status,
        reference_hotels=len(ref_hotels),
    )

    # (1) Эталон не показал предложений — нечего требовать от проверяемой стороны.
    if not ref_status.has_offers:
        result.problems = _collect_problems(reference, checked, None)
        return result

    # (2) У нас пусто целиком.
    if not chk_status.has_offers:
        result.problems = _collect_problems(reference, checked, None)
        # UNKNOWN — поиск на проверяемой стороне НЕ состоялся (упал, отвалился по
        # таймауту, площадка не приняла запрос). Это отказ инструмента, а не пропуск
        # оператора, и разница здесь принципиальная: выдать находку — значит отправить
        # коллегу разбирать нашу собственную поломку. Молчим, причина уже в problems.
        if chk_status is OperatorStatus.UNKNOWN:
            return result
        # Поиск состоялся и площадка ответила «пусто» — вот это находка.
        result.gaps = [_full_gap(ref_hotels, chk_status)]
        return result

    # (3) Обе стороны с предложениями — поштучный разбор.
    chk_hotels = checked.hotel_offers if checked else []
    match = match_hotels(ref_hotels, chk_hotels)
    result.problems = _collect_problems(reference, checked, match)

    gaps: list[HotelGap] = [
        HotelGap(
            kind=GapKind.HOTEL,
            hotel_name=h.hotel_name,
            stars=h.stars,
            resort=h.destination,
            reference_price=h.price,
            currency=h.currency,
            note="есть у оператора на эталоне, в нашей выдаче отеля нет",
        )
        for h in sorted(match.only_reference, key=lambda h: h.price)
    ]

    price_gaps, offset = _price_gaps(match, tolerance_pct)
    gaps += price_gaps

    # Обратные пропуски имеют смысл, только когда объёмы выдач сопоставимы. Витрина
    # эталона показывает по оператору выборку (блоки обрезаны), а шлюз отдаёт каталог
    # целиком — при таком перекосе каждый «лишний» отель у нас стал бы находкой, и
    # настоящие утонули бы в сотнях строк шума.
    #
    # Это ЗАМЕТКА, а не проблема: перекос — свойство площадок, а не сбой сбора. Прямое
    # направление (отель есть на эталоне, у нас нет) от него не страдает и остаётся
    # главным результатом. Недособранную выдачу ловит отдельно флаг `truncated`.
    if not REPORT_REVERSE:
        result.notes.append(
            "обратные пропуски не показаны: инструмент ищет, чего нет у нас, а зеркальное "
            "направление даёт на порядок больше строк и топит настоящие находки "
            "(PEGASGAP_REPORT_REVERSE=1 включает)")
    elif _coverage_is_lopsided(len(ref_hotels), len(chk_hotels)):
        result.notes.append(
            f"эталон показал {len(ref_hotels)} отел(ей) против {len(chk_hotels)} у нас — "
            f"он отдаёт по оператору выборку, а не весь каталог. Обратные пропуски скрыты "
            f"как заведомый шум; отельные пропуски ищутся среди того, что показал эталон")
    else:
        gaps += [
            HotelGap(
                kind=GapKind.REVERSE,
                hotel_name=h.hotel_name,
                stars=h.stars,
                resort=h.destination,
                checked_price=h.price,
                currency=h.currency,
                note="есть в нашей выдаче, на эталоне отеля нет",
            )
            for h in sorted(match.only_checked, key=lambda h: h.price)
        ]

    result.gaps = gaps
    result.unmatched = [
        f"{m.reference.hotel_name} ≈ {m.checked.hotel_name} ({m.reason})" for m in match.review
    ]
    result.price_offset_pct = round(offset, 2) if offset is not None else None
    if offset is not None and abs(offset) > MAX_PLAUSIBLE_OFFSET_PCT:
        # Проблема, а не заметка: при двукратной разнице в цене нельзя утверждать, что
        # стороны выполнили один и тот же поиск, — а значит и к остальным находкам этого
        # прогона доверия нет. Лучше отдать пустой прогон, чем правдоподобный вымысел.
        result.problems.append(
            f"цены сторон систематически расходятся на {offset:+.0f}% — это не разница "
            f"в цене, а разное её определение (состав тура, число туристов, длительность). "
            f"Находки по цене не показаны, к остальным относиться с подозрением")
    result.matched_hotels = len(match.pairs)
    result.reference_hotels = len(ref_hotels)
    return result
