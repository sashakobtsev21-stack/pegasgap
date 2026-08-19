"""Разбор логов плагина.

Проверяется на строках, снятых с живого поиска: сигнатуры — единственное, что стоит
между логом и текстом в отчёте, и ошибиться в них значит назвать неверную причину.
"""

from pegasgap.pluginlog import parse_lines

FAN_OUT = ("Pegas search fan-out truncated to 1 of 15 (date x nights) requests. "
           "RequestId: 687743136, CheckinFrom: 2026-08-22, CheckinTo: 2026-09-05, "
           "Nights: 7-7. Some dates/durations will not be searched.")

PROCESSED = ("Tour search has been processed successfully ; \n Tours Count From TO = 3260; "
             "\n Tour Count After Filtering = 3257; \n OriginalSearchUrl = http://sletat.ru/"
             "?callback=search&city=832&country=119&operators=3; \n FilteredCount = 3 \n "
             "UnicHotelsCount = 720 \n UnicHotelsCountAfterFilering = 718")


def codes(messages):
    return {c.code for c in parse_lines(messages)}


def only(messages, code):
    return next(c for c in parse_lines(messages) if c.code == code)


def test_fan_out_says_which_part_of_the_window_was_not_searched():
    """Главная причина расхождения: витрина ищет всё окно, мы — одну дату из пятнадцати."""
    cause = only([FAN_OUT], "fan_out_truncated")
    assert "1 запросов из 15" in cause.detail or "отправлено 1" in cause.detail
    assert "не искались" in cause.detail


def test_processed_line_gives_both_sides_of_the_loss():
    """Сколько дал оператор и сколько потеряли уже мы — разные вопросы к разным людям."""
    got = codes([PROCESSED])
    assert {"counts", "hotels_lost"} <= got
    assert "3260" in only([PROCESSED], "counts").detail
    assert "720" in only([PROCESSED], "hotels_lost").detail
    assert "потеряно 2" in only([PROCESSED], "hotels_lost").detail


def test_repeats_collapse_with_a_count():
    """Одна причина приходит десятками строк — по одной на тур; вываливать их по одной
    значит утопить всё остальное."""
    cause = only(["Error while parsing tour. Message: x"] * 12, "parse_error")
    assert "повторов 12" in cause.detail


def test_unknown_lines_are_silent():
    assert parse_lines(["AerospikeReadQueue finish process 687744199/3 by 855ms"]) == []


def test_gate_and_linking_signatures():
    assert "gate_skipped" in codes([
        "No available dates in requested window, search skipped without request to TO"])
    assert "no_ranges" in codes(["InternalRequestId: 1.\n Ranges count: 0\n Origin..."])
    assert "no_link_hotel" in codes(["Tour Hotel does not have a link."])
    assert "hotels_unavailable" in codes([
        "Founded 7 hotels that are not available from TO. MissingIds: 1,2,3"])
