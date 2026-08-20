"""Тесты разбора причин отельных пропусков.

Диагноз определяет, кто и что будет делать с находкой, поэтому цена ошибки высокая:
сказать «нет линковки» там, где отель просто назван иначе, значит отправить человека
править справочник, в котором всё в порядке.
"""

from decimal import Decimal

from pegasgap.catalog import CatalogHotel
from pegasgap.diagnosis import CatalogIndex, diagnose, diagnose_gap, diagnose_reverse, reverse_index
from pegasgap.gaps import MATCH_COLLAPSE_MARKER
from pegasgap.linking import Direction, LinkSet
from pegasgap.models import (
    GapKind,
    HotelDiagnosis,
    HotelGap,
    OperatorStatus,
    ScanResult,
    SearchParams,
)

PARAMS = SearchParams(
    departure_city="Москва", destination_country="Турция",
    date_from="2026-09-16", date_to="2026-09-23",
    nights_min=7, nights_max=7, adults=2,
)

# Названия взяты из живого справочника Слетать по Турции.
CATALOG = [
    CatalogHotel(id=46066, name="Kemer Star Hotel", stars=3, town_id=11),
    CatalogHotel(id=40475, name="Sun Star Beach", stars=4, town_id=11),
    CatalogHotel(id=116596, name="Almera Park Apart Hotel", stars=3, town_id=11),
    CatalogHotel(id=6610, name="Britannia Hotel & Villas", stars=4, town_id=11),
]

LINKED = LinkSet(database="plugin_db", linked_ids=frozenset({46066, 6610}))
NO_DB = LinkSet.unavailable()


def gap(name: str, stars: int | None = None) -> HotelGap:
    return HotelGap(kind=GapKind.HOTEL, hotel_name=name, stars=stars,
                    reference_price=Decimal("100000"))


def diagnosed(name: str, stars: int | None = None, links: LinkSet = LINKED,
              catalog: list[CatalogHotel] | None = None) -> HotelGap:
    g = gap(name, stars)
    diagnose_gap(g, CatalogIndex(CATALOG if catalog is None else catalog), links)
    return g


# --------------------------------- поиск в справочнике ---------------------------------


def test_hotel_absent_from_catalog():
    g = diagnosed("Nonexistent Grand Palace")
    assert g.diagnosis is HotelDiagnosis.NOT_IN_CATALOG
    assert "завести отель" in g.diagnosis.action
    assert g.catalog_id is None


def test_exact_name_resolves_to_catalog_id():
    g = diagnosed("Kemer Star Hotel")
    assert g.catalog_id == 46066


def test_ex_suffix_does_not_break_resolution():
    """На витрине отель подписан с прежним именем в скобках, в справочнике — без."""
    g = diagnosed("KEMER STAR HOTEL (EX. KEMPER DINARA GARDEN)")
    assert g.catalog_id == 46066


def test_word_split_difference_resolves():
    """Живой случай: «SUNSTAR BEACH HOTEL» на витрине против «Sun Star Beach» у нас."""
    g = diagnosed("SUNSTAR BEACH HOTEL")
    assert g.catalog_id == 40475


def test_noise_words_do_not_block_resolution():
    """«ALMERA PARK APARTMENT» против «Almera Park Apart Hotel»."""
    g = diagnosed("ALMERA PARK APARTMENT")
    assert g.catalog_id == 116596


# --------------------------------- линковка ---------------------------------


def test_linked_hotel_points_away_from_reference_data():
    """Справочники в порядке — значит разбирать надо не их."""
    g = diagnosed("Kemer Star Hotel")
    assert g.diagnosis is HotelDiagnosis.LINKED_NO_OFFER
    assert "наличие" in g.diagnosis.action


def test_unlinked_hotel_points_at_linking():
    g = diagnosed("Sun Star Beach")
    assert g.diagnosis is HotelDiagnosis.NOT_LINKED
    assert g.catalog_id == 40475
    assert "связать" in g.diagnosis.action


def test_without_db_we_do_not_claim_missing_link():
    """Без доступа к базе «нет линковки» — это домысел. Говорим только то, что знаем:
    отель в справочнике опознан, а линковка не проверена. Это не то же самое, что
    «не смотрели вовсе» — половина ответа уже есть, и терять её незачем."""
    g = diagnosed("Sun Star Beach", links=NO_DB)
    assert g.diagnosis is HotelDiagnosis.IN_CATALOG_UNCHECKED
    assert g.diagnosis is not HotelDiagnosis.UNKNOWN
    assert g.catalog_id == 40475
    assert g.catalog_name == "Sun Star Beach"


def test_shaky_match_never_claims_missing_link():
    """Похожий, но неуверенно опознанный отель не должен обвинять справочники."""
    g = diagnosed("Britannia", stars=2)   # звёзды расходятся с каталогом (4)
    assert g.diagnosis is HotelDiagnosis.UNCERTAIN
    assert g.diagnosis is not HotelDiagnosis.NOT_LINKED
    assert "вручную" in g.diagnosis.action


def test_no_catalog_means_no_verdict():
    g = diagnosed("Kemer Star Hotel", catalog=[])
    assert g.diagnosis is HotelDiagnosis.UNKNOWN


# --------------------------------- разбор прогона целиком ---------------------------------


def scan_with(gaps: list[HotelGap]) -> ScanResult:
    return ScanResult(params=PARAMS, gaps=gaps,
                      reference_status=OperatorStatus.PRICED,
                      checked_status=OperatorStatus.PRICED)


def test_only_hotel_gaps_are_diagnosed():
    """Для полного пропуска и расхождения цены справочники ни при чём."""
    others = [
        HotelGap(kind=GapKind.FULL, hotel_name="— весь запрос —"),
        HotelGap(kind=GapKind.PRICE, hotel_name="Kemer Star Hotel",
                 reference_price=Decimal("1"), checked_price=Decimal("2")),
    ]
    scan = scan_with([gap("Sun Star Beach"), *others])
    diagnose(scan, CATALOG, LINKED)
    assert scan.gaps_of(GapKind.HOTEL)[0].diagnosis is HotelDiagnosis.NOT_LINKED
    assert all(g.diagnosis is HotelDiagnosis.UNKNOWN for g in others)


def test_missing_catalog_is_noted_on_the_run():
    scan = scan_with([gap("Sun Star Beach")])
    diagnose(scan, [], LINKED)
    assert any("справочник отелей" in n for n in scan.notes)


def test_missing_db_is_noted_on_the_run():
    scan = scan_with([gap("Sun Star Beach")])
    diagnose(scan, CATALOG, NO_DB)
    assert any("линковка оператора не проверялась" in n for n in scan.notes)


def test_clean_run_gets_no_noise_notes():
    """Нет отельных пропусков — нечего и оговаривать."""
    scan = scan_with([])
    diagnose(scan, CATALOG, NO_DB)
    assert scan.notes == []


def test_diagnosis_survives_storage_roundtrip(tmp_path):
    """Диагноз обязан доезжать до отчёта и истории, иначе он бесполезен."""
    from pegasgap import storage
    scan = scan_with([gap("Sun Star Beach")])
    diagnose(scan, CATALOG, LINKED)
    with storage.session(tmp_path / "t.db") as conn:
        run_id = storage.save_scan(conn, scan)
        row = storage.gaps_of_run(conn, run_id)[0]
    assert row["diagnosis"] == HotelDiagnosis.NOT_LINKED.value
    assert row["catalog_id"] == 40475


def test_empty_reference_search_is_not_a_failure():
    """Регресс живого прогона: по ОАЭ и Таиланду у оператора на витрине нет ничего, и
    инструмент помечал прогон недостоверным. Пустой эталон — законный ответ «тут ничего
    нет», а не сбой сбора; иначе каждое такое направление засоряет отчёт."""
    from pegasgap.gaps import detect
    from pegasgap.models import ProviderResult
    ref = ProviderResult(provider="tourvisor", success=True, duration_seconds=1.0)
    chk = ProviderResult(provider="sletat", success=True, duration_seconds=1.0)
    scan = detect(PARAMS, ref, chk)
    assert scan.gaps == []
    assert scan.trustworthy


def test_note_complements_the_verdict_instead_of_repeating_it():
    """Вердикт печатается отдельной колонкой; дублировать его в комментарии — значит
    занимать место тем, что уже видно, вместо имени и id, по которым находку открывают."""
    g = diagnosed("Sun Star Beach")
    assert g.diagnosis.title not in g.note
    assert "40475" in g.note and "Sun Star Beach" in g.note


# ------------------------- опровержение вердикта о матчинге -------------------------

# Берём метку из кода, а не переписываем текст: формулировку правят, и тест,
# прибитый к ней буквально, ломается на ровном месте.
COLLAPSE = f"{MATCH_COLLAPSE_MARKER}: из показанных эталоном узнали лишь 4%"


def test_catalog_refutes_the_match_collapse_verdict():
    """Живой прогон по России: эталон показал 53 отеля, сопоставились 2, прогон объявлен
    недостоверным — а 43 из 50 пропущенных нашлись в справочнике по имени. Опознание идёт
    тем же матчером, значит имена читаются, и полсотни находок теряться не должны."""
    scan = scan_with([gap("Kemer Star Hotel"), gap("Sun Star Beach"),
                      gap("Britannia Hotel & Villas"), gap("Тьмутаракань")])
    scan.problems.append(COLLAPSE)

    diagnose(scan, CATALOG, NO_DB)

    assert scan.trustworthy
    assert not any(MATCH_COLLAPSE_MARKER in p for p in scan.problems)
    assert any("уверенно нашлись в справочнике" in n for n in scan.notes)


def test_verdict_survives_when_the_catalog_agrees_it_is_broken():
    """Если имена эталона не находят себя и в справочнике — нормализация правда развалилась,
    и осторожный вердикт остаётся."""
    scan = scan_with([gap("Тьмутаракань"), gap("Зазеркалье"), gap("Кудыкина гора")])
    scan.problems.append(COLLAPSE)

    diagnose(scan, CATALOG, NO_DB)

    assert not scan.trustworthy
    assert any(MATCH_COLLAPSE_MARKER in p for p in scan.problems)


def test_refutation_clears_only_its_own_verdict():
    """Снимается ровно один вердикт. Прочие проблемы прогона к справочнику отношения
    не имеют, и трогать их нельзя."""
    scan = scan_with([gap("Kemer Star Hotel"), gap("Sun Star Beach")])
    scan.problems += [COLLAPSE, "проверяемая: выдача получена не целиком"]

    diagnose(scan, CATALOG, NO_DB)

    assert scan.problems == ["проверяемая: выдача получена не целиком"]
    assert not scan.trustworthy


def test_no_catalog_cannot_refute_anything():
    """Без справочника доказывать нечем — вердикт остаётся."""
    scan = scan_with([gap("Kemer Star Hotel"), gap("Sun Star Beach")])
    scan.problems.append(COLLAPSE)

    diagnose(scan, [], NO_DB)

    assert not scan.trustworthy


# --- Новые признаки справочников ---------------------------------------------------

def test_disabled_hotel_is_its_own_verdict_not_a_linking_problem():
    """Связь может быть в порядке, но выключенный отель не покажут всё равно —
    и чинить надо не связь."""
    links = LinkSet(operator="Pegas Touristik", linked_ids=frozenset({46066}),
                    disabled_ids=frozenset({46066}))
    g = diagnosed("Kemer Star Hotel", links=links)
    assert g.diagnosis is HotelDiagnosis.CATALOG_DISABLED
    assert "выключен" in g.note


def test_unregistered_direction_is_noted_on_the_whole_run():
    """Пока пары «город вылета → страна» нет у оператора, правки по отелям бесполезны."""
    scan = scan_with([gap("Kemer Star Hotel")])
    diagnose(scan, CATALOG, LinkSet.unavailable(), Direction(known=False))
    assert any("не заведено" in n for n in scan.notes)


def test_unchecked_direction_says_nothing():
    """«Не смотрели» — не то же самое, что «не заведено»: спутать значит отправить
    человека заводить направление, которое давно заведено."""
    scan = scan_with([gap("Kemer Star Hotel")])
    diagnose(scan, CATALOG, LinkSet.unavailable(), Direction.unchecked())
    assert not any("не заведено" in n for n in scan.notes)


def test_direction_registered_only_without_flight():
    scan = scan_with([gap("Kemer Star Hotel")])
    diagnose(scan, CATALOG, LinkSet.unavailable(),
             Direction(known=True, with_flight=False, without_flight=True))
    assert any("не в режиме" in n for n in scan.notes)


# --- Причина «отеля нет на Турвизоре» — по словарю самой витрины ---------------------

THEIR = {
    71351: {"name": "ATLANTIS ROYAL", "stars": 3},
    52905: {"name": "ROYAL ATLANTIS BEACH", "stars": 4},
}


def reverse_scan(name: str) -> ScanResult:
    g = HotelGap(kind=GapKind.REVERSE, hotel_name=name,
                 checked_price=Decimal("125716"))
    return scan_with([g])


def test_listed_but_without_tours_is_named_so():
    """Живой Atlantis Royal: в словаре витрины отель есть, а поиск, прижатый к нему,
    возвращает ноль туров и на широкое окно. Находка верная — но без причины она
    читалась как ошибка инструмента."""
    scan = reverse_scan("Atlantis Royal Hotel")
    diagnose_reverse(scan, reverse_index(THEIR))
    gap = scan.gaps[0]
    assert gap.diagnosis is HotelDiagnosis.REF_LISTED_NO_TOURS
    assert gap.reference_hotel_id == 71351          # ссылка прижмётся к отелю
    assert "ATLANTIS ROYAL" in gap.note


def test_unknown_hotel_is_ours_alone():
    scan = reverse_scan("Гранд Пляж Юг")
    diagnose_reverse(scan, reverse_index(THEIR))
    assert scan.gaps[0].diagnosis is HotelDiagnosis.REF_NOT_IN_DICTIONARY


def test_shaky_candidate_is_offered_for_review():
    """Похожее имя в словаре есть, но совпадение шаткое: предлагаем кандидата, а не
    утверждаем — это ровно та «предполагаемая причина», которую просит разбор."""
    scan = reverse_scan("Atlantis Beach")
    diagnose_reverse(scan, reverse_index(THEIR))
    gap = scan.gaps[0]
    assert gap.diagnosis in (HotelDiagnosis.REF_MAYBE_NAMED,
                             HotelDiagnosis.REF_LISTED_NO_TOURS)
    assert gap.reference_hotel_id is not None


def test_junk_containment_is_not_offered_as_a_candidate():
    """Живой список предлагал «ABEL» для «Annabella Park» и один «ALA HOTEL» сразу
    четырём отелям — случайное вхождение трёх букв не помогает сверке, а хоронит
    доверие к колонке."""
    junk = {1: {"name": "ALA HOTEL ADULTS ONLY", "stars": 4},
            2: {"name": "ABEL", "stars": 3}}
    scan = reverse_scan("Hotel SU & Aqualand")
    diagnose_reverse(scan, reverse_index(junk))
    assert scan.gaps[0].diagnosis is HotelDiagnosis.REF_NOT_IN_DICTIONARY
    assert "убедительно" in scan.gaps[0].note


def test_district_suffix_stays_a_candidate():
    """«Erboy» против «ERBOY HOTEL SIRKECI» — стамбульская приписка района; это
    осмысленный кандидат, и его сверка стоит времени."""
    their = {7: {"name": "ERBOY HOTEL SIRKECI", "stars": 3}}
    scan = reverse_scan("Erboy")
    diagnose_reverse(scan, reverse_index(their))
    gap = scan.gaps[0]
    assert gap.diagnosis is HotelDiagnosis.REF_MAYBE_NAMED
    assert gap.reference_hotel_id == 7


def test_same_name_different_stars_says_exactly_that():
    """«Aqua Fantasy» один в один с обеих сторон, разошлась только звёздность — данные
    о звёздах у площадок расходятся сплошь и рядом, и это почти наверняка тот же отель."""
    their = {9: {"name": "AQUA FANTASY AQUAPARK HOTEL & SPA", "stars": 3}}
    g = HotelGap(kind=GapKind.REVERSE, hotel_name="Aqua Fantasy Aquapark Hotel & Spa",
                 stars=5, checked_price=Decimal("100000"))
    scan = scan_with([g])
    diagnose_reverse(scan, reverse_index(their))
    assert g.diagnosis is HotelDiagnosis.REF_MAYBE_NAMED
    assert "звёзды" in g.note and "тот же отель" in g.note
    assert g.reference_hotel_id == 9


def test_short_fuzzy_and_place_only_overlap_are_rejected():
    """Живой список: «BIR» для «Birbey», «CLUB SEVEN» для «Evsen», «BODRUM BEACH
    RESORT» двум отелям Бодрума сразу — совпадение по огрызку или по топониму не
    кандидат."""
    junk = {1: {"name": "BIR", "stars": 3},
            2: {"name": "CLUB SEVEN", "stars": 3},
            3: {"name": "BODRUM BEACH RESORT", "stars": 4}}
    for name in ("Birbey", "Evsen", "Senses Hotel Bodrum"):
        scan = reverse_scan(name)
        diagnose_reverse(scan, reverse_index(junk))
        assert scan.gaps[0].diagnosis is HotelDiagnosis.REF_NOT_IN_DICTIONARY, name


def test_weak_pair_with_star_clash_is_not_the_same_name():
    """У матчера два текста со словом «звёзды»: «названия совпали…» и «похоже на то же
    название…». Второй — не совпадение имён, и в ветку «это же имя» попадать не должен."""
    their = {5: {"name": "ARI HOTEL", "stars": 3}}
    g = HotelGap(kind=GapKind.REVERSE, hotel_name="Arin Resort Bodrum", stars=5,
                 checked_price=Decimal("100000"))
    scan = scan_with([g])
    diagnose_reverse(scan, reverse_index(their))
    assert g.diagnosis is HotelDiagnosis.REF_NOT_IN_DICTIONARY
