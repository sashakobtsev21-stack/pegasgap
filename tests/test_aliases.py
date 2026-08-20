"""Словарь синонимов: подтверждённые вручную пары сильнее правил матчинга."""

from decimal import Decimal

import pytest

from pegasgap import aliases
from pegasgap.matching import Confidence, compare, match_hotels, refresh_aliases
from pegasgap.models import HotelOffer


def h(name: str, stars: int | None = None, provider: str = "x") -> HotelOffer:
    return HotelOffer(provider=provider, hotel_name=name, price=Decimal("100000"),
                      stars=stars)


@pytest.fixture
def dictionary(tmp_path, monkeypatch):
    """Подменить словарь на временный файл и вернуть функцию его записи.

    Подмена происходит сразу, а не при первой записи: в рабочем каталоге лежит НАСТОЯЩИЙ
    hotel_aliases.yaml, и без неё «сравнение без словаря» зависело бы от того, какой тест
    успел загрузить его раньше.
    """
    path = tmp_path / "hotel_aliases.yaml"
    monkeypatch.setattr(aliases, "DEFAULT_PATH", path)
    aliases._CACHE = (-1.0, [])
    refresh_aliases()

    def write(text: str) -> None:
        path.write_text(text, encoding="utf-8")
        refresh_aliases()

    yield write
    aliases._CACHE = (-1.0, [])


def test_confirmed_pair_is_exact(dictionary):
    """«Erboy» и «ERBOY HOTEL SIRKECI» матчер сводил лишь до «сверить название» — ядро
    короткое. Человек сверил, строка в словаре — и пара сходится точно."""
    conf, why = compare(h("Erboy"), h("ERBOY HOTEL SIRKECI"))
    assert conf is not Confidence.EXACT          # без словаря — не точное совпадение
    dictionary("- [Erboy, ERBOY HOTEL SIRKECI]\n")
    conf, why = compare(h("Erboy"), h("ERBOY HOTEL SIRKECI"))
    assert conf is Confidence.EXACT
    assert "словар" in why


def test_dictionary_overrides_the_stars_veto(dictionary):
    """Живой Aqua Fantasy: имя один в один, а звёзды 5 против 3 — вето разводило пару в
    корзину проверки. Подтверждённая запись означает, что человек это видел."""
    dictionary("- [Aqua Fantasy Aquapark Hotel & Spa, AQUA FANTASY AQUAPARK HOTEL & SPA]\n")
    conf, _ = compare(h("Aqua Fantasy Aquapark Hotel & Spa", stars=5),
                      h("AQUA FANTASY AQUAPARK HOTEL & SPA", stars=3))
    assert conf is Confidence.EXACT


def test_unrelated_names_are_untouched(dictionary):
    dictionary("- [Erboy, ERBOY HOTEL SIRKECI]\n")
    assert compare(h("Rixos Premium"), h("Erboy"))[0] is Confidence.NONE


def test_pairing_uses_the_dictionary(dictionary):
    """Сквозной эффект: пара из словаря перестаёт быть «нет у нас» + «нет у них»."""
    dictionary("- [Azure Villas By Cornelia, CORNELIA AZURE VILLAS]\n")
    res = match_hotels([h("CORNELIA AZURE VILLAS", provider="tourvisor")],
                       [h("Azure Villas By Cornelia", provider="sletat")])
    assert len(res.pairs) == 1
    assert not res.only_reference and not res.only_checked


def test_broken_file_keeps_the_previous_dictionary(dictionary):
    dictionary("- [Erboy, ERBOY HOTEL SIRKECI]\n")
    dictionary("это: [не, список\n")           # битый YAML
    assert compare(h("Erboy"), h("ERBOY HOTEL SIRKECI"))[0] is Confidence.EXACT
