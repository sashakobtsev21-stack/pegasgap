"""Тесты разбора причин отельных пропусков.

Диагноз определяет, кто и что будет делать с находкой, поэтому цена ошибки высокая:
сказать «нет линковки» там, где отель просто назван иначе, значит отправить человека
править справочник, в котором всё в порядке.
"""

from decimal import Decimal

from pegasgap.catalog import CatalogHotel
from pegasgap.diagnosis import CatalogIndex, diagnose, diagnose_gap
from pegasgap.gaps import MATCH_COLLAPSE_MARKER
from pegasgap.linking import LinkSet
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
