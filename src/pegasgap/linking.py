"""Причина пропуска по внутренним справочникам Слетать.

Отвечает на вопросы, которые по HTTP не выяснить, и делит «этого отеля у нас нет» на
случаи с разными исполнителями:

* **связан ли отель** из справочника Слетать с каталогом оператора — «нет линковки»
  чинят справочниками, «линковка есть, а тура нет» разбирают наличием и логами поиска;
* **не выключен ли отель** в самом справочнике — тогда его не покажут, сколько бы туров
  оператор ни прислал;
* **заведено ли направление** «город вылета → страна» у этого оператора — если нет,
  поиск не даст ничего, и никакие правки по отелям тут не помогут.

**Источник — общая таблица `link_alldata` в базе `searcher`, а не база конкретного
плагина.** Раньше читали `link_data` из `rawPegasV5_Main`, и это требовало отдельного
доступа на каждого оператора: три оператора — три базы, и в конфиге помещался только
один. Центральная таблица содержит ровно те же связи (по Pegas сошлось до строки:
33 730) и покрывает всех сразу. Единственная разница — нумерация словарей: отели в
`searcher` идут под `dict_id = 4`, а в плагинной базе тот же словарь имеет номер 3.

Слой опциональный. Без доступа инструмент работает и даёт диагноз на уровень грубее —
это сознательно: требовать боевую базу ради уточнения было бы плохим разменом, а молча
выдать «нет линковки» там, где её просто не проверяли, — враньём.

Только чтение: единственные запросы — SELECT.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

log = logging.getLogger("pegasgap.linking")

# База общих справочников площадки. Имя вынесено в окружение только ради тестовых
# контуров — по умолчанию оно одно на всю компанию.
SEARCHER_DB = os.environ.get("PEGASGAP_SEARCHER_DB") or "searcher"

# Идентификатор словаря отелей в `searcher`. В плагинных базах тот же словарь идёт под
# номером 3, и перепутать их — значит прочитать связи звёзд вместо отелей и молча
# объявить весь каталог неслинкованным.
HOTEL_DICT_ID = 4

# Идентификаторы операторов в справочнике площадки (`sources.PartnerID`). Совпадают с
# теми, что понимает поисковый шлюз, — проверено по самой таблице.
SOURCE_IDS = {
    "Pegas Touristik": 3,
    "Coral Travel": 6,
    "Sunmar": 54,
}


@dataclass(frozen=True)
class LinkSet:
    """Что справочники говорят об отелях этого оператора."""

    operator: str = ""
    linked_ids: frozenset[int] = frozenset()
    # Отели, выключенные в справочнике Слетать. Такой отель не покажут независимо от
    # того, что прислал оператор, и это отдельная причина с отдельным исполнителем.
    disabled_ids: frozenset[int] = frozenset()
    available: bool = True
    database: str = SEARCHER_DB

    def has(self, catalog_id: int | None) -> bool:
        return catalog_id is not None and catalog_id in self.linked_ids

    def is_disabled(self, catalog_id: int | None) -> bool:
        return catalog_id is not None and catalog_id in self.disabled_ids

    @classmethod
    def unavailable(cls, operator: str = "") -> LinkSet:
        """Заглушка, когда база недоступна: ничего не утверждаем."""
        return cls(operator=operator, available=False)


@dataclass(frozen=True)
class Direction:
    """Заведено ли у оператора направление «город вылета → страна»."""

    known: bool = False
    with_flight: bool = False
    without_flight: bool = False
    available: bool = True

    def serves(self, search_mode: str) -> bool:
        """Обслуживает ли оператор направление в этом режиме поиска."""
        return self.without_flight if search_mode == "hotels" else self.with_flight

    @classmethod
    def unchecked(cls) -> Direction:
        return cls(available=False)


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


def source_id(operator: str) -> int | None:
    return SOURCE_IDS.get((operator or "").strip())


def _connect(settings: dict[str, str]):
    import pymssql

    return pymssql.connect(database=SEARCHER_DB, timeout=60, login_timeout=15, **settings)


def _ready(operator: str) -> tuple[dict[str, str], int] | None:
    """Общая проверка перед запросом: доступ настроен и оператор нам известен."""
    settings = connection_settings()
    if settings is None:
        log.info("справочники: доступ к базе не настроен — диагноз без них")
        return None
    sid = source_id(operator)
    if sid is None:
        log.info("справочники: оператор «%s» не в списке — диагноз без них", operator)
        return None
    try:
        import pymssql  # noqa: F401
    except ImportError:
        log.warning("справочники: не установлен pymssql (pip install -e '.[linking]')")
        return None
    return settings, sid


@lru_cache(maxsize=8)
def load_links(operator: str) -> LinkSet:
    """Связи и выключенные отели оператора. При любой проблеме — «недоступно».

    Недоступность намеренно не является ошибкой прогона: диагноз просто станет грубее.
    Ронять ночной обход из-за того, что не дотянулись до базы, незачем.

    Результат кешируется на процесс: при обходе матрицы таблица связей одна и та же на
    все направления оператора, а весит она десятки тысяч строк.
    """
    ready = _ready(operator)
    if ready is None:
        return LinkSet.unavailable(operator)
    settings, sid = ready

    try:
        with _connect(settings) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT int_id FROM link_alldata "
                "WHERE dict_id = %s AND source_id = %s", (HOTEL_DICT_ID, sid))
            linked = frozenset(int(r[0]) for r in cur.fetchall() if r[0] is not None)
            # Выключенные берём только среди слинкованных: остальные всё равно недостижимы
            # для этого оператора, и тащить весь справочник страны незачем.
            cur.execute(
                "SELECT h.inc FROM hotel h "
                "WHERE h.disabled = 1 AND EXISTS (SELECT 1 FROM link_alldata l "
                "  WHERE l.int_id = h.inc AND l.dict_id = %s AND l.source_id = %s)",
                (HOTEL_DICT_ID, sid))
            disabled = frozenset(int(r[0]) for r in cur.fetchall() if r[0] is not None)
    except Exception as exc:
        log.warning("справочники: база %s недоступна (%s) — диагноз без них",
                    SEARCHER_DB, type(exc).__name__)
        return LinkSet.unavailable(operator)

    log.info("справочники: у «%s» связано отелей %d, из них выключено %d",
             operator, len(linked), len(disabled))
    return LinkSet(operator=operator, linked_ids=linked, disabled_ids=disabled)


@lru_cache(maxsize=256)
def load_direction(operator: str, departure_city: str, country: str) -> Direction:
    """Заведено ли направление у оператора. Недоступность — не отрицательный ответ.

    Различать «не заведено» и «не смогли посмотреть» здесь обязательно: первое —
    готовый вывод о причине, второе — молчание. Спутать их значит отправить человека
    заводить направление, которое давно заведено.
    """
    ready = _ready(operator)
    if ready is None:
        return Direction.unchecked()
    settings, sid = ready

    try:
        with _connect(settings) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT l.wFlight, l.noFlight FROM TownFrom_State_Links l "
                "JOIN town_from tf ON tf.inc = l.townFromId "
                "JOIN state s ON s.inc = l.countryId "
                "WHERE l.sourceId = %s AND tf.name = %s AND s.name = %s",
                (sid, departure_city, country))
            row = cur.fetchone()
    except Exception as exc:
        log.warning("справочники: направление не проверить (%s)", type(exc).__name__)
        return Direction.unchecked()

    if row is None:
        return Direction(known=False)
    return Direction(known=True, with_flight=bool(row[0]), without_flight=bool(row[1]))


def reset_cache() -> None:
    """Сбросить кеши — для тестов и для перечитывания после смены доступов."""
    load_links.cache_clear()
    load_direction.cache_clear()
