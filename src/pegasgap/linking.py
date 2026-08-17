"""Линковка отелей туроператора — чтение таблицы связей из плагинной базы.

Отвечает на вопрос, который по HTTP не выяснить: **связан ли отель из справочника
Слетать с отелем в каталоге оператора.** Именно он делит отельный пропуск на два разных
случая с разными исполнителями: «нет линковки» чинят справочниками, «линковка есть, а
тура нет» — разбором наличия и логов поиска.

Слой опциональный. Без строки подключения инструмент работает и даёт диагноз на уровень
грубее — это сознательно: требовать доступ к боевой базе ради уточнения было бы плохим
разменом, а молча выдавать «нет линковки» там, где её просто не проверили, — враньём.

Только чтение: единственный запрос — SELECT по `link_data`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

log = logging.getLogger("pegasgap.linking")

# Плагинная база оператора на ag_main. Имя базы — часть конфигурации, а не константа:
# у каждого оператора она своя (rawPegasV5_Main, rawAnexV6_Main, ...).
DEFAULT_DATABASE = os.environ.get("PEGASGAP_PLUGIN_DB") or "rawPegasV5_Main"

# Идентификатор словаря отелей в link_data. Значение из таблицы link_tables той же базы:
# 3 = hotel. Хардкодить его страшновато, но альтернатива — лишний запрос на каждый прогон
# ради значения, которое не менялось никогда; проверить можно через `link_tables`.
HOTEL_DICT_ID = 3


@dataclass(frozen=True)
class LinkSet:
    """Внутренние ID отелей, у которых есть линковка с каталогом оператора."""

    database: str
    linked_ids: frozenset[int]
    available: bool = True

    def has(self, catalog_id: int | None) -> bool:
        return catalog_id is not None and catalog_id in self.linked_ids

    @classmethod
    def unavailable(cls, database: str = DEFAULT_DATABASE) -> LinkSet:
        """Заглушка на случай, когда база недоступна: ничего не утверждаем."""
        return cls(database=database, linked_ids=frozenset(), available=False)


def connection_settings() -> dict[str, str] | None:
    """Параметры подключения из окружения. None = слой выключен.

    Пароль читается только отсюда и никуда не логируется.
    """
    server = os.environ.get("PEGASGAP_DB_SERVER")
    user = os.environ.get("PEGASGAP_DB_USER")
    password = os.environ.get("PEGASGAP_DB_PASSWORD")
    if not (server and user and password):
        return None
    return {"server": server, "user": user, "password": password}


@lru_cache(maxsize=4)
def load_links(database: str = DEFAULT_DATABASE) -> LinkSet:
    """Прочитать все связи отелей оператора. При любой проблеме — «недоступно».

    Недоступность намеренно не является ошибкой прогона: диагноз просто станет грубее.
    Ронять ночной обход из-за того, что не дотянулись до базы, незачем.

    Результат кешируется на процесс: при обходе матрицы направлений таблица связей одна
    и та же, а весит она десятки тысяч строк — перечитывать её на каждый сценарий незачем.
    """
    settings = connection_settings()
    if settings is None:
        log.info("линковка: доступ к базе не настроен — диагноз без неё")
        return LinkSet.unavailable(database)
    try:
        import pymssql
    except ImportError:
        log.warning("линковка: не установлен pymssql (pip install -e '.[linking]')")
        return LinkSet.unavailable(database)

    try:
        with pymssql.connect(database=database, timeout=60, login_timeout=15,
                             **settings) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT int_id FROM link_data WHERE dict_id = %s", (HOTEL_DICT_ID,))
            ids = frozenset(int(row[0]) for row in cur.fetchall() if row[0] is not None)
    except Exception as exc:
        log.warning("линковка: база %s недоступна (%s) — диагноз без неё",
                    database, type(exc).__name__)
        return LinkSet.unavailable(database)

    log.info("линковка: в базе %s связано отелей: %d", database, len(ids))
    return LinkSet(database=database, linked_ids=ids)
