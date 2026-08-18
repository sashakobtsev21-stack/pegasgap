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


def test_load_matrix_rejects_empty_directions(tmp_path):
    """Направления можно задать двумя способами, но хотя бы один нужен — и в ошибке
    должны быть названы оба, иначе непонятно, чего именно не хватает."""
    path = tmp_path / "scenarios.yaml"
    path.write_text("windows:\n  - offset_days: 14\n", encoding="utf-8")
    with pytest.raises(ValueError, match="направления") as exc:
        load_matrix(path)
    assert "countries" in str(exc.value) and "routes" in str(exc.value)


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


# --------------------------------- явные маршруты ---------------------------------

ROUTES_CONFIG = """
operator: Pegas Touristik
defaults:
  modes: [tours]
routes:
  - {from: Москва, country: ОАЭ}
  - {from: Санкт-Петербург, country: Турция}
windows:
  - offset_days: 30
"""


def test_routes_are_pairs_not_cross_product(tmp_path):
    """Объём оператора живёт на паре «откуда → куда»: из Москвы в ОАЭ его тысячи, из
    Петербурга туда же может не быть вовсе. Перемножать города на страны — значит
    гарантированно намолотить пустых сценариев."""
    path = tmp_path / "scenarios.yaml"
    path.write_text(ROUTES_CONFIG, encoding="utf-8")
    matrix = load_matrix(path)
    assert matrix.pairs() == [("Москва", "ОАЭ"), ("Санкт-Петербург", "Турция")]
    items = matrix.build(date(2026, 9, 1))
    assert len(items) == 2                       # 2 маршрута × 1 режим × 1 окно
    assert {(i.departure_city, i.destination_country) for i in items} == {
        ("Москва", "ОАЭ"), ("Санкт-Петербург", "Турция")}


def test_routes_win_over_cross_product(tmp_path):
    """Заданы маршруты — countries и departure_cities не участвуют, иначе получился бы
    молчаливый гибрид, в котором непонятно, что именно проверяется."""
    path = tmp_path / "scenarios.yaml"
    path.write_text(ROUTES_CONFIG + "countries: [Египет, Таиланд]\n", encoding="utf-8")
    matrix = load_matrix(path)
    assert len(matrix.pairs()) == 2
    assert all(c != "Египет" for _, c in matrix.pairs())


def test_route_without_country_is_rejected(tmp_path):
    path = tmp_path / "scenarios.yaml"
    path.write_text("routes:\n  - {from: Москва}\nwindows:\n  - offset_days: 30\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="country"):
        load_matrix(path)


def test_cross_product_still_works_without_routes(tmp_path):
    """Старая форма конфига остаётся рабочей — ломать её ради новой незачем."""
    path = tmp_path / "scenarios.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    matrix = load_matrix(path)
    assert not matrix.routes
    assert len(matrix.pairs()) == 2              # 1 город × 2 страны


def test_durations_are_a_case_dimension(tmp_path):
    """Оператор отваливается не «по стране», а на конкретной длительности: неделя есть,
    десять ночей уже нет. Значит длительность — измерение кейса, а не число на весь обход."""
    cfg = tmp_path / "s.yaml"
    cfg.write_text(
        "operator: Pegas Touristik\n"
        "defaults:\n  modes: [tours]\n  nights: [7, 10, {min: 12, max: 14}]\n"
        "routes:\n  - {from: Москва, country: Турция}\n"
        "windows:\n  - {offset_days: 14}\n",
        encoding="utf-8")
    built = load_matrix(cfg).build(date(2026, 9, 1))
    assert [(p.nights_min, p.nights_max) for p in built] == [(7, 7), (10, 10), (12, 14)]


def test_old_nights_pair_is_still_understood(tmp_path):
    """По старой паре написаны конфиги и README — молча перестать её понимать значит
    сломать обход тому, кто просто не обновил файл."""
    cfg = tmp_path / "s.yaml"
    cfg.write_text(
        "operator: Pegas Touristik\n"
        "defaults:\n  modes: [tours]\n  nights_min: 10\n  nights_max: 12\n"
        "routes:\n  - {from: Москва, country: Турция}\n"
        "windows:\n  - {offset_days: 14}\n",
        encoding="utf-8")
    built = load_matrix(cfg).build(date(2026, 9, 1))
    assert [(p.nights_min, p.nights_max) for p in built] == [(10, 12)]


def test_backwards_duration_is_rejected(tmp_path):
    cfg = tmp_path / "s.yaml"
    cfg.write_text(
        "operator: Pegas Touristik\n"
        "defaults:\n  modes: [tours]\n  nights: [{min: 14, max: 7}]\n"
        "routes:\n  - {from: Москва, country: Турция}\n"
        "windows:\n  - {offset_days: 14}\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="задом наперёд"):
        load_matrix(cfg)
