"""Тесты сопоставления отелей.

Проверяем не столько «матчер работает», сколько заданную асимметрию: щедро сводить
похожее и уводить сомнительное в проверку, но не выдумывать совпадений там, где их нет.
"""

from decimal import Decimal

from pegasgap.matching import Confidence, compare, match_hotels, normalize
from pegasgap.models import HotelOffer


def h(name: str, price: str = "100000", stars: int | None = None, provider: str = "tourvisor"):
    return HotelOffer(provider=provider, hotel_name=name, price=Decimal(price), stars=stars)


# --------------------------------- нормализация ---------------------------------


def test_normalize_strips_noise_words():
    assert normalize("Rixos Premium Hotel & Resort") == normalize("Rixos Premium")


def test_normalize_strips_ex_suffix():
    # Приписка о прежнем названии есть не на всех площадках — сравнивать надо текущее имя.
    assert normalize("Rixos Sharm El Sheikh (ex. Premier Royal)") == normalize("Rixos Sharm El Sheikh")


def test_normalize_strips_stars_marker():
    assert normalize("Sunrise Grand Select 5*") == normalize("Sunrise Grand Select")


def test_normalize_handles_ampersand():
    assert normalize("Bed & Breakfast Inn") == normalize("Bed and Breakfast Inn")


def test_normalize_keeps_something_for_all_noise_name():
    # Название целиком из шумовых слов не должно схлопнуться в пустоту — иначе отель
    # пропал бы из сопоставления совсем.
    assert normalize("The Hotel Resort") != ""


# --------------------------------- пары ---------------------------------


def test_exact_when_only_noise_differs():
    conf, _ = compare(h("Rixos Premium Seagate"), h("Rixos Premium Seagate Resort & SPA"))
    assert conf is Confidence.EXACT


def test_strong_on_containment():
    conf, _ = compare(h("Sunrise Grand Select Arabian Beach"), h("Sunrise Grand Select"))
    assert conf is Confidence.STRONG


def test_short_core_containment_is_not_strong():
    # «sun» входит в «sunrise», но это разные отели — короткое ядро ничего не доказывает.
    conf, _ = compare(h("Sun"), h("Sunrise"))
    assert conf is not Confidence.STRONG


def test_stars_conflict_downgrades_to_weak():
    # Одно имя, разные звёзды: либо ошибка данных, либо разные объекты сети.
    # Пропуском объявлять нельзя — только в проверку. Одна звезда допускается
    # (см. _STARS_TOLERANCE), две — уже расхождение.
    conf, reason = compare(h("Rixos Premium", stars=5), h("Rixos Premium", stars=3))
    assert conf is Confidence.WEAK
    assert "звёзды" in reason


def test_unknown_stars_do_not_conflict():
    conf, _ = compare(h("Rixos Premium", stars=5), h("Rixos Premium", stars=None))
    assert conf is Confidence.EXACT


def test_different_hotels_do_not_match():
    conf, _ = compare(h("Rixos Premium Seagate"), h("Albatros Palace Resort"))
    assert conf is Confidence.NONE


# --------------------------------- раскладка выдачи ---------------------------------


def test_exact_pass_wins_over_weak():
    """Точное совпадение не должно быть «съедено» более ранним слабым кандидатом."""
    reference = [h("Rixos Premium Seagate")]
    checked = [h("Rixos Premium Magawish", provider="sletat"),
               h("Rixos Premium Seagate", provider="sletat")]
    res = match_hotels(reference, checked)
    assert len(res.pairs) == 1
    assert res.pairs[0].checked.hotel_name == "Rixos Premium Seagate"
    assert res.pairs[0].confidence is Confidence.EXACT


def test_unmatched_reference_becomes_gap_candidate():
    res = match_hotels([h("Albatros Palace")], [h("Rixos Premium", provider="sletat")])
    assert [o.hotel_name for o in res.only_reference] == ["Albatros Palace"]
    assert [o.hotel_name for o in res.only_checked] == ["Rixos Premium"]
    assert not res.pairs


def test_weak_match_is_not_a_gap():
    """Сомнительное совпадение уходит в проверку, а не в пропуски: выдуманный пропуск
    дороже пропущенного."""
    res = match_hotels([h("Rixos Premium", stars=5)],
                       [h("Rixos Premium", stars=3, provider="sletat")])
    assert not res.only_reference          # НЕ пропуск
    assert not res.pairs                   # и не сравнимая пара
    assert len(res.review) == 1


def test_one_checked_hotel_is_used_once():
    """Иначе один отель нашей выдачи закрыл бы сразу несколько пропусков эталона."""
    reference = [h("Rixos Premium Seagate"), h("Rixos Premium Magawish")]
    checked = [h("Rixos Premium Seagate", provider="sletat")]
    res = match_hotels(reference, checked)
    assert len(res.pairs) == 1
    assert [o.hotel_name for o in res.only_reference] == ["Rixos Premium Magawish"]


def test_matched_share_reflects_coverage():
    reference = [h("A Palace Resort"), h("B Grand Select"), h("C Beach Albatros")]
    checked = [h("A Palace Resort", provider="sletat")]
    res = match_hotels(reference, checked)
    assert res.matched_share == 1 / 3


# --------------------------------- русские названия ---------------------------------
# Случаи ниже взяты из живого прогона по России, где сопоставилось лишь 20% отелей.


def test_single_letter_core_matches_nothing():
    """«А-Отель» после вычистки шума превращался в «а», и этот символ входил в половину
    названий: живой прогон дал «ЭРА CПА → А-Отель» и «В ЁЛКАХ → А-Отель»."""
    assert compare(h("ЭРА CПА"), h("А-Отель"))[0] is Confidence.NONE
    assert compare(h("В ЁЛКАХ"), h("А-Отель"))[0] is Confidence.NONE
    assert compare(h("КРУИЗ ПИОНЕРСКИЙ"), h("Гостиница Н"))[0] is Confidence.NONE


def test_latin_lookalike_inside_cyrillic_word_is_fixed():
    """В «ЭРА CПА» буква C латинская: на вид не отличить, а для сравнения это другое
    слово."""
    assert normalize("ЭРА CПА") == normalize("ЭРА СПА")   # первая C латинская
    assert compare(h("ЭРА CПА"), h("Эра Спа"))[0] is Confidence.EXACT


def test_genuinely_latin_name_is_left_alone():
    """Правка гомоглифов не должна портить честно латинское название."""
    assert compare(h("SALVE"), h("Salve"))[0] is Confidence.EXACT
    assert "а" not in normalize("SALVE")                  # не подменили на кириллицу


def test_alternate_spelling_in_parentheses_matches():
    """«SALVE (САЛЬВЭ)» и «БЭСЭДЭР (BESEDER)» — одно имя двумя алфавитами, и площадки
    выбирают из них по-разному."""
    assert compare(h("SALVE (САЛЬВЭ)"), h("Сальвэ"))[0].comparable
    assert compare(h("БЭСЭДЭР (BESEDER)"), h("Beseder"))[0].comparable
    assert compare(h("БЭСЭДЭР (BESEDER)"), h("Бэсэдэр"))[0].comparable


def test_russian_noise_words_are_stripped():
    assert compare(h("АЛЬБЕРТИНА ГОСТЕВОЙ ДОМ"), h("Альбертина"))[0].comparable
    assert compare(h("ЭЛИЗА ЗАРКАУ"), h("Гостевой дом Элиза Заркау"))[0].comparable


def test_city_suffix_still_matches_by_containment():
    assert compare(h("БАЛТИКА КАЛИНИНГРАД"), h("Балтика Калининград"))[0] is Confidence.EXACT


def test_ex_name_in_parentheses_is_not_an_alternate_spelling():
    """Приписка (ex. …) — ПРОШЛОЕ имя, а не второе написание нынешнего. Сойтись по нему
    с чужим объектом нельзя."""
    assert not compare(h("Rixos Sharm (ex. Premier Royal)"), h("Premier Royal"))[0].comparable


def test_one_star_apart_is_a_data_discrepancy_not_a_different_hotel():
    """Живой обход по Грузии: «ORBI BEACH TOWER ≈ Orbi Beach Tower (4 и 3)»,
    «IVERIA INN ≈ Iveria Inn (4 и 3)». Имя совпадает буквально — площадки просто
    разошлись в звёздности, и уводить такие пары в проверку значит терять их."""
    assert compare(h("ORBI BEACH TOWER", stars=4), h("Orbi Beach Tower", stars=3))[0] \
        is Confidence.EXACT
    assert compare(h("IVERIA INN", stars=4), h("Iveria Inn", stars=3))[0] is Confidence.EXACT


def test_two_stars_apart_still_needs_a_human():
    conf, reason = compare(h("Rixos Premium", stars=5), h("Rixos Premium", stars=3))
    assert conf is Confidence.WEAK
    assert "звёзды" in reason
