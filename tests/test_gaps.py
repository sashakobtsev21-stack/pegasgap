"""Тесты классификации находок.

Ключевое, что здесь проверяется, — что инструмент молчит, когда не уверен, и что
различает «плагин ответил „туров нет“» и «плагин не ответил». Ошибки в эту сторону
дороже всего: они отправляют людей разбирать несуществующие проблемы.
"""

from datetime import date
from decimal import Decimal

from pegasgap.gaps import detect, operator_status
from pegasgap.models import (
    PEGAS,
    GapKind,
    HotelOffer,
    Offer,
    OperatorStatus,
    ProviderResult,
    SearchParams,
)

PARAMS = SearchParams(
    departure_city="Москва", destination_country="Турция",
    date_from=date(2026, 9, 10), date_to=date(2026, 9, 20),
    nights_min=7, nights_max=7, adults=2,
)


def hotel(name: str, price: str, provider: str, stars: int | None = None) -> HotelOffer:
    return HotelOffer(provider=provider, hotel_name=name, price=Decimal(price), stars=stars)


def result(provider: str, hotels: list[HotelOffer] | None = None, **kw) -> ProviderResult:
    hotels = hotels or []
    offers = kw.pop("offers", None)
    if offers is None and hotels:
        offers = [Offer(provider=provider, operator=PEGAS, price=min(h.price for h in hotels))]
    return ProviderResult(
        provider=provider, success=kw.pop("success", True), duration_seconds=1.0,
        hotel_offers=hotels, offers=offers or [], **kw,
    )


# --------------------------------- статус оператора ---------------------------------


def test_status_not_responding_beats_everything():
    r = result("sletat", operators_not_responding=["Pegas Touristik"])
    assert operator_status(r) is OperatorStatus.NOT_RESPONDING


def test_status_no_tours():
    r = result("sletat", operators_no_tours=["Pegas Touristik"])
    assert operator_status(r) is OperatorStatus.NO_TOURS


def test_status_priced_from_hotels_when_no_operator_breakdown():
    r = result("tourvisor", [hotel("A Palace", "100000", "tourvisor")], offers=[])
    assert operator_status(r) is OperatorStatus.PRICED


def test_status_absent_when_nothing_at_all():
    assert operator_status(result("sletat")) is OperatorStatus.ABSENT


def test_status_unknown_on_failed_search():
    assert operator_status(result("sletat", success=False)) is OperatorStatus.UNKNOWN
    assert operator_status(None) is OperatorStatus.UNKNOWN


def test_status_matches_operator_across_spellings():
    # На Турвизоре оператор пишется иначе — статус обязан определиться всё равно.
    r = result("tourvisor", offers=[
        Offer(provider="tourvisor", operator="Pegas Touristik", price=Decimal("90000"))])
    assert operator_status(r) is OperatorStatus.PRICED


# --------------------------------- находки уровня запроса ---------------------------------


def test_no_gap_when_reference_has_nothing():
    """Отсутствие предложений у эталона не означает, что они должны быть у нас."""
    scan = detect(PARAMS, result("tourvisor"), result("sletat"))
    assert scan.gaps == []


def test_full_gap_when_checked_says_no_tours():
    ref = result("tourvisor", [hotel("A Palace", "100000", "tourvisor")])
    chk = result("sletat", operators_no_tours=[PEGAS])
    scan = detect(PARAMS, ref, chk)
    assert [g.kind for g in scan.gaps] == [GapKind.FULL]
    assert "туров нет" in scan.gaps[0].note


def test_not_responding_is_its_own_class():
    """Разные корни: «ответил, что пусто» и «не ответил» нельзя схлопывать."""
    ref = result("tourvisor", [hotel("A Palace", "100000", "tourvisor")])
    chk = result("sletat", operators_not_responding=[PEGAS])
    scan = detect(PARAMS, ref, chk)
    assert [g.kind for g in scan.gaps] == [GapKind.NOT_RESPONDING]


def test_failed_check_is_not_a_gap():
    """Регресс живого прогона: Слетать упал с «направление не предлагается», а инструмент
    отрапортовал «Полный пропуск» и счёл прогон достоверным. Несостоявшаяся проверка —
    это наша поломка, а не пропуск оператора; выдавать её за находку значит отправить
    коллегу разбирать чужую проблему."""
    ref = result("tourvisor", [hotel("A Palace", "100000", "tourvisor")])
    chk = result("sletat", success=False, error="направление «Турция» не предлагается на Sletat")
    scan = detect(PARAMS, ref, chk)
    assert scan.gaps == []
    assert not scan.trustworthy
    assert any("проверка не выполнена" in p for p in scan.problems)


def test_crashed_check_is_not_a_gap():
    ref = result("tourvisor", [hotel("A Palace", "100000", "tourvisor")])
    chk = result("sletat", success=False, error="TimeoutError: превышен таймаут")
    scan = detect(PARAMS, ref, chk)
    assert scan.gaps == []
    assert not scan.trustworthy


def test_reference_hotel_count_is_recorded_even_without_hotel_level_pass():
    """Сводка «эталон: N отелей» должна быть верной и когда до отельного разбора не дошли."""
    ref = result("tourvisor", [hotel("A Palace", "100000", "tourvisor"),
                               hotel("B Grand", "90000", "tourvisor")])
    scan = detect(PARAMS, ref, result("sletat", operators_no_tours=[PEGAS]))
    assert scan.reference_hotels == 2


def test_full_gap_lists_examples_for_investigation():
    ref = result("tourvisor", [
        hotel("A Palace", "100000", "tourvisor"),
        hotel("B Grand", "90000", "tourvisor"),
    ])
    scan = detect(PARAMS, ref, result("sletat", operators_no_tours=[PEGAS]))
    note = scan.gaps[0].note
    assert "B Grand" in note          # самый дешёвый показан первым
    assert scan.gaps[0].reference_price == Decimal("90000")


# --------------------------------- находки уровня отеля ---------------------------------


def test_hotel_gap_for_missing_hotel():
    ref = result("tourvisor", [
        hotel("A Palace", "100000", "tourvisor"),
        hotel("B Grand", "110000", "tourvisor"),
    ])
    chk = result("sletat", [hotel("A Palace", "101000", "sletat")])
    scan = detect(PARAMS, ref, chk)
    hotel_gaps = scan.gaps_of(GapKind.HOTEL)
    assert [g.hotel_name for g in hotel_gaps] == ["B Grand"]


def test_reverse_gap_reported_when_switched_on(monkeypatch):
    monkeypatch.setattr("pegasgap.gaps.REPORT_REVERSE", True)
    ref = result("tourvisor", [hotel("A Palace", "100000", "tourvisor")])
    chk = result("sletat", [
        hotel("A Palace", "100000", "sletat"),
        hotel("C Beach", "95000", "sletat"),
    ])
    scan = detect(PARAMS, ref, chk)
    assert [g.hotel_name for g in scan.gaps_of(GapKind.REVERSE)] == ["C Beach"]


def test_systematic_price_shift_is_not_a_finding():
    """Витрины считают цену на разной базе. Ровный сдвиг по всем отелям — свойство
    площадок, а не проблема отеля, и находкой быть не должен."""
    ref = result("tourvisor", [
        hotel("A Palace", "100000", "tourvisor"),
        hotel("B Grand", "200000", "tourvisor"),
        hotel("C Beach", "300000", "tourvisor"),
    ])
    chk = result("sletat", [                       # везде ровно +15%
        hotel("A Palace", "115000", "sletat"),
        hotel("B Grand", "230000", "sletat"),
        hotel("C Beach", "345000", "sletat"),
    ])
    scan = detect(PARAMS, ref, chk)
    assert scan.gaps_of(GapKind.PRICE) == []
    assert scan.price_offset_pct == 15.0


def test_outlier_against_the_shift_is_a_finding():
    ref = result("tourvisor", [
        hotel("A Palace", "100000", "tourvisor"),
        hotel("B Grand", "200000", "tourvisor"),
        hotel("C Beach", "300000", "tourvisor"),
    ])
    chk = result("sletat", [
        hotel("A Palace", "115000", "sletat"),     # +15% — фон
        hotel("B Grand", "230000", "sletat"),      # +15% — фон
        hotel("C Beach", "450000", "sletat"),      # +50% — выброс
    ])
    scan = detect(PARAMS, ref, chk)
    price_gaps = scan.gaps_of(GapKind.PRICE)
    assert [g.hotel_name for g in price_gaps] == ["C Beach"]
    assert price_gaps[0].diff_pct == 50.0


# --------------------------------- достоверность ---------------------------------


def test_unverified_operator_filter_marks_run_untrustworthy():
    """Цены карточек Турвизора не привязаны к оператору: если фильтр не применился,
    сравнивать нечего — и прогон обязан честно сказать об этом."""
    ref = result("tourvisor", [hotel("A Palace", "100000", "tourvisor")],
                 operator_filter_verified=False)
    chk = result("sletat", [hotel("A Palace", "100000", "sletat")])
    scan = detect(PARAMS, ref, chk)
    assert not scan.trustworthy
    assert any("фильтр по оператору" in p for p in scan.problems)


def test_collapsed_matching_marks_run_untrustworthy():
    """Если узнали меньше трети отелей эталона — сломался матчинг, а не каталог."""
    ref = result("tourvisor", [hotel(f"Hotel Number {i}", "100000", "tourvisor")
                               for i in range(10)])
    chk = result("sletat", [hotel("Hotel Number 1", "100000", "sletat")])
    scan = detect(PARAMS, ref, chk)
    assert not scan.trustworthy
    assert any("матчинга" in p for p in scan.problems)


def test_clean_run_is_trustworthy():
    ref = result("tourvisor", [hotel("A Palace", "100000", "tourvisor")])
    chk = result("sletat", [hotel("A Palace", "100000", "sletat")])
    scan = detect(PARAMS, ref, chk)
    assert scan.trustworthy
    assert scan.summary == {k.value: 0 for k in GapKind}


def test_weak_match_lands_in_review_not_in_gaps():
    ref = result("tourvisor", [hotel("A Palace", "100000", "tourvisor", stars=5)])
    chk = result("sletat", [hotel("A Palace", "100000", "sletat", stars=3)])
    scan = detect(PARAMS, ref, chk)
    assert scan.gaps_of(GapKind.HOTEL) == []
    assert len(scan.unmatched) == 1


def test_lopsided_coverage_suppresses_reverse_noise(monkeypatch):
    """Регресс живого прогона: эталон показал 14 отелей, наша сторона 962 — и инструмент
    выдал 949 «обратных пропусков», похоронив под ними настоящие находки.

    Перекос идёт ЗАМЕТКОЙ, а не проблемой: витрина эталона показывает по оператору
    выборку, и это её устройство, а не сбор. Считать это поломкой значило бы объявлять
    недостоверным каждый прогон и обесценить инструмент целиком. Прямое направление
    (отель есть на эталоне, у нас нет) от перекоса не страдает.
    """
    monkeypatch.setattr("pegasgap.gaps.REPORT_REVERSE", True)
    ref = result("tourvisor", [hotel(f"Ref {i}", "100000", "tourvisor") for i in range(3)])
    chk = result("sletat", [hotel(f"Ref {i}", "100000", "sletat") for i in range(3)]
                 + [hotel(f"Extra {i}", "100000", "sletat") for i in range(40)])
    scan = detect(PARAMS, ref, chk)
    assert scan.gaps_of(GapKind.REVERSE) == []
    assert scan.trustworthy
    assert any("выборку, а не весь каталог" in n for n in scan.notes)


def test_hotel_gaps_survive_lopsided_coverage():
    """Главный класс находок обязан работать и при разном объёме выдач — иначе перекос,
    который на этих площадках постоянен, отключал бы инструмент насовсем."""
    ref = result("tourvisor", [hotel("Only On Reference", "100000", "tourvisor"),
                               hotel("Shared Palace", "100000", "tourvisor")])
    chk = result("sletat", [hotel("Shared Palace", "100000", "sletat")]
                 + [hotel(f"Extra {i}", "100000", "sletat") for i in range(40)])
    scan = detect(PARAMS, ref, chk)
    assert [g.hotel_name for g in scan.gaps_of(GapKind.HOTEL)] == ["Only On Reference"]
    assert scan.trustworthy


def test_truncated_collection_is_still_a_problem():
    """Недособранная выдача — именно поломка: отель мог не догрузиться, а выглядит как
    отсутствующий. От структурного перекоса это отличается принципиально."""
    ref = result("tourvisor", [hotel("A Palace", "100000", "tourvisor")], truncated=True)
    chk = result("sletat", [hotel("A Palace", "100000", "sletat")])
    scan = detect(PARAMS, ref, chk)
    assert not scan.trustworthy


def test_comparable_coverage_still_reports_reverse(monkeypatch):
    monkeypatch.setattr("pegasgap.gaps.REPORT_REVERSE", True)
    ref = result("tourvisor", [hotel(f"Ref {i}", "100000", "tourvisor") for i in range(10)])
    chk = result("sletat", [hotel(f"Ref {i}", "100000", "sletat") for i in range(10)]
                 + [hotel("Extra", "100000", "sletat")])
    scan = detect(PARAMS, ref, chk)
    assert [g.hotel_name for g in scan.gaps_of(GapKind.REVERSE)] == ["Extra"]
    assert scan.trustworthy


def test_wide_spread_does_not_turn_exact_matches_into_findings():
    """Регресс живого прогона: разница цен расползлась на 4–13% при медиане 9%, и три
    отеля, где цена совпала ДО РУБЛЯ, попали в находки как «отклонение −9%». Формально
    они дальше всех от медианы, по сути — ничем не примечательны. Полоса нормального
    должна строиться по фактическому разбросу."""
    spread = [0.0, 0.0, 0.0, 4.2, 4.6, 8.5, 9.1, 9.8, 10.3, 11.4, 12.1, 12.6, 13.5]
    base = 100000
    ref = result("tourvisor", [hotel(f"H{i}", str(base), "tourvisor")
                               for i in range(len(spread))])
    chk = result("sletat", [hotel(f"H{i}", str(int(base * (1 + d / 100))), "sletat")
                            for i, d in enumerate(spread)])
    scan = detect(PARAMS, ref, chk)
    assert scan.gaps_of(GapKind.PRICE) == []


def test_true_outlier_survives_wide_spread():
    """Расширение полосы не должно глушить настоящий выброс."""
    spread = [0.0, 0.0, 0.0, 4.2, 4.6, 8.5, 9.1, 9.8, 10.3, 11.4, 12.1, 12.6, 90.0]
    base = 100000
    ref = result("tourvisor", [hotel(f"H{i}", str(base), "tourvisor")
                               for i in range(len(spread))])
    chk = result("sletat", [hotel(f"H{i}", str(int(base * (1 + d / 100))), "sletat")
                            for i, d in enumerate(spread)])
    scan = detect(PARAMS, ref, chk)
    assert [g.hotel_name for g in scan.gaps_of(GapKind.PRICE)] == ["H12"]


def test_implausible_offset_kills_price_findings():
    """Регресс живого прогона по Вьетнаму: цены Слетать оказались ровно вдвое ниже
    Турвизора — −48% на КАЖДОМ из сорока пяти сопоставленных отелей. Такая равномерность
    означает не разницу цен, а разное её определение: стороны считают не одно и то же.
    Инструмент выдавал шесть «находок», ранжируя шум."""
    base = 480000
    ref = result("tourvisor", [hotel(f"H{i}", str(base + i), "tourvisor") for i in range(10)])
    chk = result("sletat", [hotel(f"H{i}", str(int((base + i) * 0.52)), "sletat")
                            for i in range(10)])
    scan = detect(PARAMS, ref, chk)
    assert scan.gaps_of(GapKind.PRICE) == []
    assert not scan.trustworthy
    assert any("разное её определение" in p for p in scan.problems)
    assert scan.price_offset_pct is not None    # сам сдвиг показываем: это и есть симптом


def test_plausible_offset_still_yields_findings():
    """Обычная разница витрин находки не глушит."""
    diffs = [9.0] * 9 + [40.0]
    base = 100000
    ref = result("tourvisor", [hotel(f"H{i}", str(base), "tourvisor") for i in range(10)])
    chk = result("sletat", [hotel(f"H{i}", str(int(base * (1 + d / 100))), "sletat")
                            for i, d in enumerate(diffs)])
    scan = detect(PARAMS, ref, chk)
    assert [g.hotel_name for g in scan.gaps_of(GapKind.PRICE)] == ["H9"]
    assert scan.trustworthy


def test_reverse_gaps_are_off_by_default():
    """Живой обход дал 587 обратных пропусков из 641 находки: витрина показывает по
    оператору выборку, мы отдаём каталог целиком, и каждый лишний отель у нас становился
    «находкой». Инструмент ищет, чего нет У НАС, — зеркало топит настоящие находки."""
    ref = result("tourvisor", [hotel("A Palace", "100000", "tourvisor")])
    chk = result("sletat", [hotel("A Palace", "100000", "sletat"),
                            hotel("C Beach", "95000", "sletat")])
    scan = detect(PARAMS, ref, chk)
    assert scan.gaps_of(GapKind.REVERSE) == []
    assert any("обратные пропуски не показаны" in n for n in scan.notes)


def test_two_pairs_are_too_few_to_judge_price():
    """Живой прогон по России: сопоставились ровно две пары, и обе попали в ценовые
    находки. При двух точках медиана совпадает с наблюдением, и каждая пара оказывается
    выбросом относительно другой — это сравнение пары с ней же самой, а не находка."""
    ref = result("tourvisor", [hotel("A Palace", "100000", "tourvisor"),
                               hotel("B Grand", "100000", "tourvisor")])
    chk = result("sletat", [hotel("A Palace", "126000", "sletat"),
                            hotel("B Grand", "91000", "sletat")])
    scan = detect(PARAMS, ref, chk)
    assert scan.gaps_of(GapKind.PRICE) == []
    assert scan.price_offset_pct is not None      # сам сдвиг всё равно записан


def test_three_pairs_are_enough_to_spot_the_odd_one():
    ref = result("tourvisor", [hotel(f"H{i}", "100000", "tourvisor") for i in range(3)])
    chk = result("sletat", [hotel("H0", "100000", "sletat"), hotel("H1", "101000", "sletat"),
                            hotel("H2", "150000", "sletat")])
    scan = detect(PARAMS, ref, chk)
    assert [g.hotel_name for g in scan.gaps_of(GapKind.PRICE)] == ["H2"]
