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
from datetime import date

from pegasgap.matching import MatchResult, match_hotels
from pegasgap.models import (
    PEGAS,
    DayOffer,
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

# По этой метке диагностика узнаёт вердикт, чтобы снять его, если справочник докажет
# обратное (см. diagnosis.refute_match_collapse). Строку не менять отдельно от той
# проверки — они парные.
MATCH_COLLAPSE_MARKER = "не удалось сопоставить названия отелей"

# Во сколько раз выдачи сторон могут отличаться по размеру, прежде чем сравнение теряет
# смысл. Доля сопоставленных этого не ловит: если эталон отдал 14 отелей из сотен, они
# все могут сопоставиться прекрасно — и «отельных пропусков нет» будет означать лишь
# «мы посмотрели четырнадцать отелей», а не «всё на месте». Ограничение отдельное.
# Постраничный сбор эталона сделал такой перекос редким, но не исключил: программа
# оператора на площадках может отличаться в разы в обе стороны.
MAX_COVERAGE_RATIO = 5.0

# Систематическая разница цен, выше которой сравнение перестаёт быть сравнением. Разная
# база (агентская против розничной) даёт единицы процентов; полсотни означает, что стороны
# считают РАЗНОЕ — иной состав тура, другое число туристов, другая длительность. Живой
# прогон по Вьетнаму дал ровно −48% на каждом из сорока пяти отелей: такая равномерность
# исключает случайность и выдаёт различие в определении цены, а не в самой цене.
MAX_PLAUSIBLE_OFFSET_PCT = 25.0

# Показывать ли пропуски на стороне Турвизора — отели, которые есть у нас и которых нет
# у них. ПО УМОЛЧАНИЮ ДА: витрина здесь не эталон, а вторая площадка, и вопрос стоит в обе
# стороны. Отсутствие у них — такой же факт, как отсутствие у нас, и иногда более
# тревожный: это либо их неполнота, либо наша выдача показывает то, чего на рынке нет.
#
# Раньше сторона была выключена, потому что давала 587 находок из 641. Причина была не в
# том, что находки ложные, а в том, что мы читали витрину на десять страниц, а свою
# выдачу целиком: «есть только у нас» было артефактом недочитанного. Теперь прячет её
# ровно этот признак — обрезанная выдача, — а не догадка про объёмы.
REPORT_REVERSE = (os.environ.get("PEGASGAP_REPORT_REVERSE") or "1").strip().lower() in (
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


# Роль в сравнении → название площадки. В сообщениях нужны именно названия: «эталон» и
# «проверяемая» понятны тому, кто держит в голове устройство инструмента, а читающему лог
# нужно знать, НА КАКОМ САЙТЕ проблема, чтобы идти разбираться в правильное место.
_PLATFORM = {"tourvisor": "Турвизор", "sletat": "Слетать"}


def _who(result: ProviderResult | None, fallback: str) -> str:
    """Название площадки для сообщения; роль — только если провайдер неизвестен."""
    if result is None:
        return fallback
    return _PLATFORM.get(result.provider, result.provider or fallback)


def _collect_problems(
    reference: ProviderResult | None,
    checked: ProviderResult | None,
    match: MatchResult | None,
) -> tuple[list[str], list[str]]:
    """Разбор надёжности прогона: (проблемы, заметки).

    Проблемы обесценивают находки — с любой из них прогон недостоверен целиком. Заметки
    лишь описывают условия сбора и на доверие не влияют. Граница между ними одна: может
    ли это породить ЛОЖНУЮ находку, или всего лишь укрыть настоящую.
    """
    problems: list[str] = []
    notes: list[str] = []

    # Пояснения площадок к состоявшемуся ответу — контекст, а не поломка.
    for res, fallback in ((reference, "эталон"), (checked, "проверяемая")):
        if res is not None and res.note:
            notes.append(f"{_who(res, fallback)}: {res.note}")

    if reference is None:
        problems.append("Турвизор (эталон): результата нет")
    elif not reference.success and not is_not_applicable_error(reference.error):
        problems.append(f"{_who(reference, 'эталон')} (эталон): поиск не удался — "
                        f"{reference.error}")

    # Проверяемая сторона: ЛЮБОЙ несостоявшийся поиск — проблема, включая «площадка не
    # обслуживает такой запрос». На стороне эталона такой отказ безобиден (просто нечего
    # сравнивать), а здесь он означает, что проверку выполнить не удалось. Молчать о нём
    # нельзя: пустая выдача из-за нашего же сбоя выглядит точь-в-точь как пропуск оператора.
    if checked is None:
        problems.append("Слетать: результата нет")
    elif not checked.success:
        problems.append(f"{_who(checked, 'проверяемая')}: проверка не выполнена — "
                        f"{checked.error}")

    for res, fallback in ((reference, "эталон"), (checked, "проверяемая")):
        if res is None:
            continue
        role = _who(res, fallback)
        # Фильтр по оператору не подтвердился: карточки отелей не несут имени ТО, значит
        # цены в них — минимум по всем операторам, и любой вывод о пропусках будет ложным.
        # Площадка искала не то, что просили: сравнивать такие выдачи нельзя вовсе.
        for mismatch in res.param_mismatches:
            problems.append(f"{role}: {mismatch}")
        if res.operator_filter_verified is False:
            problems.append(f"{role}: фильтр по оператору не применился — данные непригодны")

    # Обрезанная выдача проверяемой стороны — прямой источник выдуманных пропусков: отель
    # мог просто не догрузиться, а выглядит это как «у оператора его нет».
    if checked is not None and checked.truncated:
        problems.append(f"{_who(checked, 'проверяемая')}: выдача получена не целиком — "
                        f"часть «пропусков» может оказаться просто недогруженными отелями")

    # А обрезка выдачи ТУРВИЗОРА ложных находок в нашу сторону не даёт: отель, который мы
    # увидели, мы увидели, и пропуск по нему настоящий независимо от того, сколько ещё
    # осталось за кадром. Это потеря полноты, а не корректности, — то есть заметка.
    #
    # На обратную сторону она влияет иначе, и там ответ не «пометить прогон недостоверным»,
    # а «не показывать эту сторону»: прямое направление остаётся полноценным результатом, и
    # выбрасывать его вместе с обратным было бы разменом наоборот. Само подавление и
    # объяснение — в разборе находок ниже.
    if reference is not None and reference.truncated:
        notes.append(f"{_who(reference, 'Турвизор')}: выдача получена не целиком — "
                     f"часть пропусков могла не попасть в отчёт")

    if reference and checked and reference.success and checked.success:
        currencies = {o.currency for o in reference.hotel_offers} | {
            o.currency for o in checked.hotel_offers}
        if len(currencies) > 1:
            problems.append(f"валюты площадок различаются: {sorted(currencies)}")

    if match is not None and match.matched_share < MIN_MATCHED_SHARE:
        # Формулировка называет ОБЕ стороны намеренно: «сбой матчинга» ничего не говорит
        # тому, кто читает лог, — непонятно, что с чем не сошлось и чья это поломка.
        problems.append(
            f"{MATCH_COLLAPSE_MARKER}: из показанных эталоном узнали в нашей выдаче лишь "
            f"{match.matched_share:.0%}. Вероятнее расхождение в написании имён, чем "
            f"исчезновение стольких туров разом")

    return problems, notes


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


def _as_int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _same_basis_prices(m) -> tuple[date, str, DayOffer, DayOffer] | None:
    """Общий состав, на котором цены сравнимы: (заезд, питание, у витрины, у нас).

    Сначала сравнение прижали к общему заезду — и «на Слетать дороже на 9.9%» перестало
    измерять разницу дат. Но на одной дате всё ещё сравнивались минимумы РАЗНОГО состава:
    AI против RO дороже на треть безо всякой разницы площадок. Поэтому пара цен обязана
    совпадать и по заезду, и по базовому коду питания.

    Из сравнимых составов берётся самый дешёвый по стороне витрины: это предложение,
    которое человек увидит первым, открыв обе ссылки, и по нему расхождение проверяется
    глазами. Предложения с нераспознанным питанием не участвуют — молчание честнее
    сравнения вслепую.
    """
    best: tuple[date, str, DayOffer, DayOffer] | None = None
    for day in set(m.reference.day_offers) & set(m.checked.day_offers):
        ours_by_meal: dict[str, DayOffer] = {}
        for offer in m.checked.day_offers[day]:
            if offer.meal and (offer.meal not in ours_by_meal
                               or offer.price < ours_by_meal[offer.meal].price):
                ours_by_meal[offer.meal] = offer
        for ref in m.reference.day_offers[day]:
            ours = ours_by_meal.get(ref.meal or "")
            if ours is None:
                continue
            if best is None or ref.price < best[2].price:
                best = (day, ref.meal or "", ref, ours)
    return best


def _price_gaps(match: MatchResult, tolerance_pct: float) -> tuple[list[HotelGap], float | None]:
    """Отели, чья цена выбивается из обычного для этого прогона расхождения.

    Сравнение идёт ТОЛЬКО по общему заезду (см. `_same_day_prices`). Пара без общей даты
    из расчёта выпадает целиком — и из полосы «обычного», и из находок: сравнить её
    честно нельзя, а сравнить нечестно хуже, чем промолчать.

    Полоса «нормального» строится по самим данным: медиана расхождений задаёт центр,
    медианное абсолютное отклонение — ширину. Иначе на широком разбросе (а он широкий:
    живой прогон дал 4–13% при медиане 9%) в находки попадали бы отели с точным
    совпадением цены — формально они дальше всех от медианы, а по сути ничем не
    примечательны.
    """
    comparable = [(m, same) for m in match.pairs if (same := _same_basis_prices(m))]
    diffs = [float((ours.price - ref.price) / ref.price * 100)
             for _, (_, _, ref, ours) in comparable if ref.price]
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
    for m, (day, meal, ref, ours) in comparable:
        if not ref.price:
            continue
        diff = float((ours.price - ref.price) / ref.price * 100)
        deviation = diff - offset
        if abs(deviation) <= band:
            continue
        out.append(HotelGap(
            kind=GapKind.PRICE,
            hotel_name=m.reference.hotel_name,
            stars=m.reference.stars or m.checked.stars,
            resort=m.reference.destination,
            reference_price=ref.price,
            checked_price=ours.price,
            currency=m.reference.currency,
            matched_name=m.checked.hotel_name,
            # Идентификатор отеля в нашем каталоге приходит в самой выдаче. Он нужен
            # ссылке на поиск, чтобы прижать её к конкретному отелю; у отельных пропусков
            # его проставляет диагностика, а здесь отель у нас есть — берём как есть.
            reference_hotel_id=_as_int(m.reference.raw_label),
            catalog_id=_as_int(m.checked.raw_label),
            # Обе даты — один и тот же заезд: сравнение по определению одинодневное.
            reference_checkin=day,
            checked_checkin=day,
            # Питание — общий знаменатель сравнения, номер — нашей стороны; номер
            # витрины появится после точечной сверки (см. roomcheck).
            checked_meal=meal,
            checked_room=ours.room,
            reference_tour_id=ref.tour_id,
            note=(f"разница {diff:+.1f}% при обычной для прогона {offset:+.1f}% "
                  f"(±{band:.1f}) — отклонение {deviation:+.1f}%"),
        ))
    out.sort(key=lambda g: abs(g.diff_pct or 0), reverse=True)
    return out, offset


# Во сколько раз разница в рублях должна быть ровнее самих цен, чтобы считать её ОДНОЙ
# составляющей тура. Тур — это перелёт плюс проживание, и перевозка одна на все отели:
# разойдись стороны в ней, разница у каждого отеля будет одинаковой до рубля, тогда как
# цены отелей между собой различаются заметно. Живой замер по Грузии: 209262/134195,
# 212956/137888, 198182/123115 — разница 75067, 75068, 75067 на все двадцать семь пар.
_FLAT_DIFF_RATIO = 5.0

# Ниже этого разброса самих цен отеля вопрос неразрешим: если все отели стоят одинаково,
# постоянная разница в рублях и постоянная в процентах — одно и то же, и утверждать
# что-то одно значит гадать. Регресс по Вьетнаму как раз такой: цены различались на рубли.
_MIN_PRICE_SPREAD = 0.01


def _relative_spread(values: list[float]) -> float | None:
    """Разброс относительно середины. None — середина в нуле, мерить не от чего."""
    middle = statistics.median(values)
    if not middle:
        return None
    return statistics.median([abs(v - middle) for v in values]) / abs(middle)


def _is_flat_component(deltas: list[float], prices: list[float]) -> bool:
    """Разница ровнее самих цен во много раз — значит это одна составляющая тура."""
    delta_spread = _relative_spread(deltas)
    price_spread = _relative_spread(prices)
    if delta_spread is None or price_spread is None:
        return False
    # Цены отелей сами почти не различаются — постоянная разница в рублях и постоянная
    # в процентах неотличимы, и выбирать между ними значило бы гадать.
    if price_spread < _MIN_PRICE_SPREAD:
        return False
    return delta_spread * _FLAT_DIFF_RATIO <= price_spread


def _systematic_gap_verdict(match: MatchResult, offset: float) -> str:
    """Объяснить крупный систематический сдвиг: он бывает двух разных природ.

    Постоянная разница В РУБЛЯХ означает несовпадение отдельной составляющей тура —
    практически всегда перевозки. Это НАСТОЯЩАЯ разница цен, просто не про отели, и
    разбирать её надо один раз на прогон, а не по каждому отелю.

    Пропорциональная разница означает, что стороны считают разное: другой состав тура,
    другое число туристов, другая длительность.
    """
    pairs = [p for p in match.pairs if p.reference.price]
    tail = "Находки по цене не показаны; на отельные пропуски это не влияет"
    if len(pairs) >= MIN_PAIRS_FOR_SPREAD:
        deltas = [float(p.checked.price - p.reference.price) for p in pairs]
        prices = [float(p.reference.price) for p in pairs]
        if _is_flat_component(deltas, prices):
            middle = statistics.median(deltas)
            side = "дороже" if middle > 0 else "дешевле"
            return (f"у нас {side} на {abs(middle):,.0f} на КАЖДОМ отеле одинаково "
                    f"({offset:+.0f}%) — расходится не цена отелей, а общая составляющая "
                    f"тура, почти наверняка перевозка. {tail}").replace(",", " ")
    return (f"цены сторон систематически расходятся на {offset:+.0f}% — это не разница "
            f"в цене, а разное её определение (состав тура, число туристов, длительность). "
            f"{tail}")


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
        # Ссылку ставим сразу, а не в конце: разбор выходит раньше на пустом эталоне и на
        # несостоявшейся проверке, и такие прогоны оставались без ссылки — хотя открыть
        # витрину как раз и хочется, когда у оператора там пусто.
        reference_url=reference.search_url if reference else None,
        checked_request_id=checked.request_id if checked else None,
    )

    # (1) Эталон не показал предложений — нечего требовать от проверяемой стороны.
    if not ref_status.has_offers:
        result.problems, extra = _collect_problems(reference, checked, None)
        result.notes += extra
        return result

    # (2) У нас пусто целиком.
    if not chk_status.has_offers:
        result.problems, extra = _collect_problems(reference, checked, None)
        result.notes += extra
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
    result.problems, extra = _collect_problems(reference, checked, match)
    result.notes += extra

    gaps: list[HotelGap] = [
        HotelGap(
            kind=GapKind.HOTEL,
            hotel_name=h.hotel_name,
            stars=h.stars,
            resort=h.destination,
            reference_price=h.price,
            currency=h.currency,
            reference_hotel_id=_as_int(h.raw_label),
            note="есть у оператора на эталоне, в нашей выдаче отеля нет",
        )
        for h in sorted(match.only_reference, key=lambda h: h.price)
    ]

    price_gaps, offset = _price_gaps(match, tolerance_pct)
    gaps += price_gaps

    # Обратные пропуски имеют смысл, только когда объёмы выдач сопоставимы, а они редко
    # сопоставимы: у Pegas по Египту наша сторона отдаёт 347 отелей против 115 у эталона,
    # у Coral по Таиланду наоборот — 49 против 87. При перекосе каждый «лишний» отель у
    # нас стал бы находкой, и настоящие утонули бы в сотнях строк шума.
    #
    # Это ЗАМЕТКА, а не проблема: разный объём программы — свойство площадок, а не сбой
    # сбора. Прямое направление (отель есть на эталоне, у нас нет) от него не страдает и
    # остаётся главным результатом. Недособранную выдачу ловит отдельно флаг `truncated`.
    if not REPORT_REVERSE:
        result.notes.append(
            "пропуски на стороне Турвизора не показаны (PEGASGAP_REPORT_REVERSE=0)")
    elif reference is not None and reference.truncated:
        # Единственная честная причина промолчать: их выдачу мы прочитали не до конца, и
        # «у них этого нет» неотличимо от «мы до этой страницы не дошли». Обратная сторона
        # требует ПОЛНОТЫ ОБЕИХ выдач — в отличие от прямой, где увиденный отель остаётся
        # увиденным независимо от того, сколько осталось за кадром.
        result.notes.append(
            f"пропуски на стороне Турвизора не показаны: их выдача прочитана не до конца "
            f"({len(ref_hotels)} отел(ей)), и «у них нет» неотличимо от «мы не дочитали». "
            f"Полная выдача включается настройкой PEGASGAP_TOURVISOR_PAGES")
    else:
        if _coverage_is_lopsided(len(ref_hotels), len(chk_hotels)):
            # Уже не повод молчать: обе выдачи полные, и разница объёмов — это и есть
            # результат, а не помеха. Но сказать о ней стоит, иначе список на сотни строк
            # читается как авария.
            result.notes.append(
                f"объёмы площадок расходятся: у нас {len(chk_hotels)} отел(ей), "
                f"на Турвизоре {len(ref_hotels)} — разница ожидаемо большая")
        gaps += [
            HotelGap(
                kind=GapKind.REVERSE,
                hotel_name=h.hotel_name,
                stars=h.stars,
                resort=h.destination,
                checked_price=h.price,
                currency=h.currency,
                # Наш id и заезд — иначе ссылка «на Слетать» вела на общий поиск из
                # сотен строк, в котором находку ещё надо разыскать.
                catalog_id=_as_int(h.raw_label),
                checked_checkin=h.checkin,
                note="есть в нашей выдаче, на Турвизоре отеля нет",
            )
            for h in sorted(match.only_checked, key=lambda h: h.price)
        ]

    result.gaps = gaps
    result.unmatched = [
        f"{m.reference.hotel_name} ≈ {m.checked.hotel_name} ({m.reason})" for m in match.review
    ]
    result.price_offset_pct = round(offset, 2) if offset is not None else None
    if offset is not None and abs(offset) > MAX_PLAUSIBLE_OFFSET_PCT:
        # ЗАМЕТКА, а не проблема. Раньше здесь стояла проблема, и прогон браковался
        # целиком — вместе с отельными пропусками, которые к ценам отношения не имеют:
        # «этого отеля у нас нет» устанавливается по наличию, а не по цене. Живой счёт:
        # 74 прогона выброшено только из-за сдвига, и с ними ушли 1834 отельных находки.
        #
        # Сами находки по цене при этом не показываются — их обнуляет `_price_gaps`,
        # потому что ранжировать отклонения от заведомо чужой базы бессмысленно.
        # А если вместе со сдвигом развалился и матчинг, это ловит отдельная проверка,
        # и вот она прогон действительно бракует.
        result.notes.append(_systematic_gap_verdict(match, offset))
    result.matched_hotels = len(match.pairs)
    result.reference_hotels = len(ref_hotels)
    result.checked_hotels = len(chk_hotels)
    return result
