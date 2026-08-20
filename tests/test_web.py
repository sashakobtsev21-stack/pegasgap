"""Тесты веб-API.

Проверяем контракт, на который смотрит дашборд: если ответ поедет, интерфейс молча
покажет пустые таблицы вместо находок — самая незаметная поломка из возможных.
"""

from datetime import date
from decimal import Decimal

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from pegasgap import storage
from pegasgap.models import (
    PEGAS,
    GapKind,
    HotelDiagnosis,
    HotelGap,
    OperatorStatus,
    ScanResult,
    SearchParams,
)
from pegasgap.web import create_app

PARAMS = SearchParams(
    departure_city="Москва", destination_country="Египет",
    date_from=date(2026, 9, 16), date_to=date(2026, 9, 23),
    nights_min=7, nights_max=7, adults=2,
)


def sample_scan(**kw) -> ScanResult:
    return ScanResult(
        params=PARAMS, operator=PEGAS,
        reference_status=OperatorStatus.PRICED,
        checked_status=OperatorStatus.PRICED,
        reference_hotels=15, matched_hotels=12,
        gaps=[
            HotelGap(kind=GapKind.HOTEL, hotel_name="DEXON ROMA HOTEL", stars=3,
                     reference_price=Decimal("148435"),
                     diagnosis=HotelDiagnosis.NOT_LINKED,
                     catalog_id=36064, catalog_name="Dexon – Roma Hotel",
                     note="«Dexon – Roma Hotel» id 36064 — связи с каталогом оператора нет"),
            HotelGap(kind=GapKind.PRICE, hotel_name="EMPIRE HOTEL",
                     reference_price=Decimal("100000"), checked_price=Decimal("111000")),
        ],
        notes=["эталон показал выборку"],
        unmatched=["SAND BEACH ≈ Picalbatros Sands"],
        **kw,
    )


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "web.db"
    app = create_app(db_path=db, config_path=tmp_path / "нет.yaml")
    with TestClient(app) as c:
        c.db_path = db          # тестам нужен путь, чтобы подложить прогон
        yield c


def seed(client, scan: ScanResult) -> int:
    with storage.session(client.db_path) as conn:
        return storage.save_scan(conn, scan)


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_run_not_found_is_404(client):
    assert client.get("/api/runs/999").status_code == 404


def test_run_returns_gaps_with_diagnosis(client):
    run_id = seed(client, sample_scan())
    body = client.get(f"/api/runs/{run_id}").json()
    assert body["trustworthy"] is True
    assert body["reference_hotels"] == 15
    assert body["notes"] == ["эталон показал выборку"]
    assert body["unmatched"] == ["SAND BEACH ≈ Picalbatros Sands"]

    hotel_gap = next(g for g in body["gaps"] if g["kind"] == "hotel")
    assert hotel_gap["diagnosis"] == "not_linked"
    assert hotel_gap["diagnosis_title"] == "нет линковки"
    assert hotel_gap["catalog_id"] == 36064


def test_price_gap_carries_computed_diff(client):
    run_id = seed(client, sample_scan())
    body = client.get(f"/api/runs/{run_id}").json()
    price_gap = next(g for g in body["gaps"] if g["kind"] == "price")
    assert price_gap["diff_pct"] == pytest.approx(11.0)


def test_unknown_diagnosis_is_hidden_not_labelled(client):
    """«Не проверялось» — не результат разбора. Пустая колонка честнее подписи."""
    scan = sample_scan()
    scan.gaps[0].diagnosis = HotelDiagnosis.UNKNOWN
    run_id = seed(client, scan)
    hotel_gap = next(g for g in client.get(f"/api/runs/{run_id}").json()["gaps"]
                     if g["kind"] == "hotel")
    assert hotel_gap["diagnosis"] is None
    assert hotel_gap["diagnosis_title"] is None


def test_actions_are_summarised_per_diagnosis(client):
    """Что делать — сводкой на прогон, а не повтором в каждой строке."""
    run_id = seed(client, sample_scan())
    actions = client.get(f"/api/runs/{run_id}").json()["actions"]
    assert len(actions) == 1
    assert actions[0]["count"] == 1
    assert "слинковать" in actions[0]["action"]


def test_untrustworthy_run_exposes_problems(client):
    run_id = seed(client, sample_scan(problems=["фильтр по оператору не применился"]))
    body = client.get(f"/api/runs/{run_id}").json()
    assert body["trustworthy"] is False
    assert body["problems"] == ["фильтр по оператору не применился"]


def test_history_counts_and_lists(client):
    seed(client, sample_scan())
    body = client.get("/api/history?days=7").json()
    assert body["trustworthy_runs"] == 1
    counts = {s["kind"]: s["count"] for s in body["summary"]}
    assert counts["hotel"] == 1
    assert counts["full"] == 0
    assert len(body["runs"]) == 1
    assert body["runs"][0]["gaps"] == 2
    # Каждый класс сопровождается подсказкой — иначе цифра ничего не подсказывает.
    assert all(s["hint"] for s in body["summary"])


def test_history_marks_untrustworthy_runs(client):
    seed(client, sample_scan(problems=["цены расходятся вдвое"]))
    body = client.get("/api/history").json()
    assert body["trustworthy_runs"] == 0
    assert body["runs"][0]["trustworthy"] is False


def test_scan_rejects_bad_mode(client):
    r = client.post("/api/scan", json={
        "country": "Египет", "date_from": "2026-09-16", "date_to": "2026-09-23",
        "mode": "круизы",
    })
    assert r.status_code == 400


def test_scan_rejects_reversed_dates(client):
    """Валидация модели должна доезжать до пользователя понятной ошибкой, а не 500."""
    r = client.post("/api/scan", json={
        "country": "Египет", "date_from": "2026-09-23", "date_to": "2026-09-16",
    })
    assert r.status_code == 400


def test_sweep_matrix_missing_config_is_400_not_500(client):
    """Файла сценариев нет — это ошибка настройки, и сказать о ней надо внятно."""
    r = client.get("/api/sweep/matrix")
    assert r.status_code == 400
    assert "scenarios" in r.json()["detail"].lower() or "не найден" in r.json()["detail"]


def test_sweep_idle_state(client):
    body = client.get("/api/sweep").json()
    assert body["running"] is False
    assert body["done"] == 0


def test_sweep_matrix_lists_actual_scenarios(tmp_path):
    """«12 сценариев» не отвечает на вопрос «что именно проверится», а даты по конфигу
    вообще не прочитать: там смещения от дня запуска. Поэтому эндпоинт отдаёт
    развёрнутый список, а не только его размер."""
    config = tmp_path / "scenarios.yaml"
    config.write_text(
        "operator: Pegas Touristik\n"
        "defaults:\n  departure_cities: [Москва]\n  nights_min: 7\n  modes: [tours, hotels]\n"
        "countries: [Турция, Египет]\n"
        "windows:\n  - offset_days: 30\n",
        encoding="utf-8")
    app = create_app(db_path=tmp_path / "m.db", config_path=config)
    with TestClient(app) as c:
        body = c.get("/api/sweep/matrix").json()

    assert body["total"] == 4                      # 1 город × 2 страны × 2 режима × 1 окно
    assert len(body["scenarios"]) == 4
    first = body["scenarios"][0]
    assert first["departure_city"] == "Москва"
    assert {s["mode"] for s in body["scenarios"]} == {"tours", "hotels"}
    assert {s["country"] for s in body["scenarios"]} == {"Турция", "Египет"}
    # Даты конкретные, а не смещения — иначе список не помог бы свериться.
    assert first["date_from"].count("-") == 2
    assert first["date_from"] < first["date_to"]


def test_point_check_can_pick_the_operator(client):
    """Операторов несколько, и точечная проверка чаще всего нужна именно чтобы разобрать
    жалобу по конкретному ТО — без выбора она проверяла бы всегда первого."""
    assert "operators" in client.get("/api/refdata").json()


def test_unknown_operator_is_refused_not_silently_swapped(client):
    """Молча подменить оператора значит показать человеку разбор чужой выдачи."""
    r = client.post("/api/scan", json={
        "country": "Турция", "departure": "Москва",
        "date_from": "2026-09-20", "date_to": "2026-09-27",
        "operator": "Библио-Глобус",
    })
    assert r.status_code == 400
    assert "не в списке" in r.json()["detail"]


def test_every_db_backed_endpoint_answers_on_an_empty_base(tmp_path):
    """И3 плана: свежая пустая база не должна ронять ни один эндпоинт — «нет данных»
    это ответ, а не исключение. Сетевые эндпоинты (refdata, scan) сюда не входят."""
    app = create_app(tmp_path / "empty.db")
    client = TestClient(app)
    for path in ("/healthz", "/api/queue", "/api/findings", "/api/worker",
                 "/api/proxies", "/api/history", "/api/sweep", "/api/sweep/matrix"):
        response = client.get(path)
        assert response.status_code == 200, f"{path}: {response.status_code}"
        assert response.json() is not None, path
    assert client.get("/api/runs/9999").status_code == 404
