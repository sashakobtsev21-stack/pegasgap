"""Основа сравнения цен: нормализация питания и опровержение по номерам.

Эти правила решают, какие пары цен вообще сравнимы, поэтому проверяются на живых
написаниях обеих площадок, а не на выдуманных.
"""

from pegasgap.basis import normalize_meal, room_tags, rooms_alike, rooms_differ

# --------------------------------- питание ---------------------------------


def test_both_platforms_speak_the_same_meal_codes():
    """Шлюз пишет кодом, витрина — полным именем из своего словаря; сходиться обязаны."""
    assert normalize_meal("AI") == normalize_meal("All Inclusive") == "AI"
    assert normalize_meal("RO") == normalize_meal("Без питания") == "RO"
    assert normalize_meal("BB") == normalize_meal("Завтрак") == "BB"
    assert normalize_meal("UAI") == normalize_meal("Ultra All Inclusive") == "UAI"


def test_plus_variants_collapse_to_the_base():
    """«HB+» против «HB» — строгое равенство разорвало бы сравнимые пары: вторая
    площадка плюс-варианты не различает."""
    assert normalize_meal("HB+") == "HB"
    assert normalize_meal("FB+") == "FB"


def test_unknown_meal_is_none_not_a_guess():
    assert normalize_meal("шведский стол у бассейна") is None
    assert normalize_meal("") is None
    assert normalize_meal(None) is None


# --------------------------------- номера ---------------------------------


def test_live_pair_from_actualize_matches_ours():
    """Живой ответ actualize: «стандарт 2 местный» — и наш «Стандартный номер»."""
    assert not rooms_differ("стандарт 2 местный", "Стандартный номер")


def test_promo_against_standard_is_a_real_difference():
    assert rooms_differ("Promo Room", "Стандартный номер")
    assert rooms_differ("Эконом-номер с 2 отдельными кроватями", "стандарт 2 местный")


def test_bed_configuration_is_not_a_category():
    """«Твин» может быть тем самым стандартом — опровержение по кроватям выдумывало бы
    разницу."""
    assert room_tags("Твин") == frozenset()
    assert not rooms_differ("Твин", "Standard Room")


def test_no_signal_means_no_refutation():
    assert not rooms_differ("Номер 12", "Standard Room")
    assert not rooms_differ(None, "Standard Room")
    assert not rooms_differ("", "")


def test_suite_family_speaks_both_scripts():
    assert not rooms_differ("Сюит Вид на Территорию", "Deluxe Suite")
    assert rooms_differ("Сюит Вид на Территорию", "Promo Room")


# --------------------------------- один ли это номер ---------------------------------


def test_alike_needs_positive_evidence_not_absence_of_clash():
    """«Jasmine Pool View» против «camelia family superior» опровергнуть нечем — ни
    одного словарного слова категории у первой. Но и совпадением это не является."""
    assert not rooms_differ("Jasmine Pool View", "camelia family superior")
    assert not rooms_alike("Jasmine Pool View", "camelia family superior")


def test_alike_by_matching_category_sets():
    assert rooms_alike("eco room", "Двухместный Эконом")
    assert rooms_alike("стандарт 2 местный", "Стандартный номер")


def test_overlapping_but_unequal_categories_are_not_alike():
    """{family, suite} и {family, superior} пересекаются по family, оставаясь разными
    номерами — пересечения мало, нужен одинаковый набор."""
    assert not rooms_alike("Family Suite", "Family Superior")


def test_alike_by_nearly_identical_names_without_categories():
    assert rooms_alike("family roof suite with jacuzzi", "Family Roof Suite With Jacuzzi")
    assert rooms_alike("superior swim up room sea view", "Superior Room Sea View")
