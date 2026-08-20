"""Настройки доступа к справочникам: свои переменные и фолбэк на коннекторные."""

from pegasgap.linking import connection_settings

ALL = ("PEGASGAP_DB_SERVER", "PEGASGAP_DB_USER", "PEGASGAP_DB_PASSWORD",
       "SLETAT_DB_SERVER", "SLETAT_DB_USER", "SLETAT_DB_PASSWORD")


def clear(monkeypatch):
    for var in ALL:
        monkeypatch.delenv(var, raising=False)


def test_connector_variables_fill_the_missing_pieces(monkeypatch):
    """Хост из .env инструмента, учётка — из переменных MCP-коннектора машины: значения
    не проходят ни через чат, ни через файлы репозитория."""
    clear(monkeypatch)
    monkeypatch.setenv("PEGASGAP_DB_SERVER", "db.example")
    monkeypatch.setenv("SLETAT_DB_USER", "u")
    monkeypatch.setenv("SLETAT_DB_PASSWORD", "p")
    assert connection_settings() == {"server": "db.example", "user": "u", "password": "p"}


def test_own_variables_take_precedence(monkeypatch):
    clear(monkeypatch)
    monkeypatch.setenv("PEGASGAP_DB_SERVER", "db.example")
    monkeypatch.setenv("PEGASGAP_DB_USER", "own")
    monkeypatch.setenv("PEGASGAP_DB_PASSWORD", "own-pass")
    monkeypatch.setenv("SLETAT_DB_USER", "connector")
    assert connection_settings()["user"] == "own"


def test_incomplete_settings_disable_the_layer(monkeypatch):
    clear(monkeypatch)
    monkeypatch.setenv("PEGASGAP_DB_SERVER", "db.example")
    assert connection_settings() is None
