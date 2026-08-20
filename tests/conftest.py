"""Общие фикстуры."""

import pytest

from pegasgap import aliases
from pegasgap.matching import refresh_aliases


@pytest.fixture(autouse=True)
def _no_alias_dictionary(tmp_path, monkeypatch):
    """Изолировать тесты от НАСТОЯЩЕГО hotel_aliases.yaml в корне проекта.

    Словарь синонимов сильнее любого правила матчинга, и по мере его роста живые записи
    молча меняли бы вердикты в тестах: «Erboy» становился бы EXACT там, где проверяется
    ветка шатких кандидатов. Тесты словаря подменяют путь своей фикстурой поверх этой.
    """
    monkeypatch.setattr(aliases, "DEFAULT_PATH", tmp_path / "no_aliases.yaml")
    aliases._CACHE = (-1.0, [])
    refresh_aliases()
    yield
    aliases._CACHE = (-1.0, [])
    refresh_aliases()
