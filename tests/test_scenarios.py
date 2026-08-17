"""Тесты матрицы регулярного обхода."""

from datetime import date

import pytest

from pegasgap.models import PEGAS
from pegasgap.scenarios import Matrix, Window, load_matrix

CONFIG = """
operator: Pegas Touristik
defaults:
  departure_cities: [Москва]
  adults: 2
  nights_min: 7
  modes: [tours, hotels]
countries: [Турция, Египет]
windows:
  - offset_days: 14
  - offset_days: 45
    length_days: 10
"""


def test_windows_are_relative_to_run_day():
    """Абсолютные даты в конфиге протухли бы, и обход искал бы туры в прошлом."""
    start, end = Window(offset_days=14, length_days=7).dates(date(2026, 9, 1))
    assert start == date(2026, 9, 15)
    assert end == date(2026, 9, 22)


def test_matrix_expands_to_full_product():
    matrix = Matrix(
        departure_cities=["Москва"], countries=["Турция", "Египет"],
        modes=["tours", "hotels"], windows=[Window(14), Window(45)],
    )
    items = matrix.build(date(2026, 9, 1))
    assert len(items) == 1 * 2 * 2 * 2


def test_operator_is_pinned_on_every_request():
    """Пустой список операторов означал бы поиск по всем ТО — тогда выдача перестала бы
    быть выдачей оператора, и любой вывод о пропусках стал бы неверным."""
    items = Matrix(countries=["Турция"], windows=[Window(14)]).build(date(2026, 9, 1))
    assert all(i.operators == [PEGAS] for i in items)


def test_load_matrix_reads_file(tmp_path):
    path = tmp_path / "scenarios.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    matrix = load_matrix(path)
    assert matrix.countries == ["Турция", "Египет"]
    assert matrix.modes == ["tours", "hotels"]
    assert [w.length_days for w in matrix.windows] == [7, 10]
    assert len(matrix.build(date(2026, 9, 1))) == 8


def test_load_matrix_rejects_empty_countries(tmp_path):
    path = tmp_path / "scenarios.yaml"
    path.write_text("windows:\n  - offset_days: 14\n", encoding="utf-8")
    with pytest.raises(ValueError, match="страны"):
        load_matrix(path)


def test_load_matrix_rejects_bad_mode(tmp_path):
    path = tmp_path / "scenarios.yaml"
    path.write_text(
        "countries: [Турция]\nwindows:\n  - offset_days: 14\n"
        "defaults:\n  modes: [tours, круизы]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="режимы"):
        load_matrix(path)


def test_missing_file_says_what_to_do(tmp_path):
    with pytest.raises(FileNotFoundError, match="--config"):
        load_matrix(tmp_path / "нет.yaml")


def test_shipped_config_is_valid():
    """Файл в репозитории должен разбираться — иначе `sweep` падает на первом же запуске."""
    matrix = load_matrix("scenarios.yaml")
    assert matrix.operator == PEGAS
    assert matrix.build(date(2026, 9, 1))
