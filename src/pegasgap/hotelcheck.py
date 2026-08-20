"""Верификация «отеля нет на Слетать» прижатой пробой шлюза — зеркало reversecheck.

«Отеля нет в выдаче» само по себе не доказательство: на обратной стороне ровно такое
допущение дало 37% фантомов. Прямая сторона надёжнее (шлюз читается постранично до
`finished`), но главный класс находок инструмента заслуживает не «надёжнее», а
подтверждения: параметр `hotels=` шлюза принимает список наших id и фильтрует точно
(контрольная проба: два живых отеля вернулись, неслинкованный — нет).

Пробуются только находки, у которых отель УВЕРЕННО опознан в справочнике (диагнозы
`linked_no_offer` / `not_linked` / `catalog_disabled` / `in_catalog_unchecked`):
шаткий кандидат «не опознан» пробовать нельзя — туры чужого отеля снимали бы чужую
находку.

* туры нашлись — находка снимается: наша выдача была недочитана или отель потерялся
  при сборе; заметка называет пример;
* туров нет — находка остаётся уже как подтверждённое отсутствие;
* проба не состоялась — находки честно помечаются неверифицированными.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from pegasgap.models import GapKind, HotelDiagnosis, ScanResult, SearchParams
from pegasgap.providers.sletat_api import probe_hotels_with_tours

log = logging.getLogger("pegasgap.hotelcheck")

Probe = Callable[[SearchParams, list[int]], Awaitable[set[int] | None]]

# Диагнозы, при которых catalog_id — уверенное опознание, а не догадка.
_PROBEABLE = {
    HotelDiagnosis.LINKED_NO_OFFER,
    HotelDiagnosis.NOT_LINKED,
    HotelDiagnosis.CATALOG_DISABLED,
    HotelDiagnosis.IN_CATALOG_UNCHECKED,
}


async def verify_hotel_gaps(scan: ScanResult,
                            probe: Probe = probe_hotels_with_tours) -> None:
    """Снять отельные находки, у которых прижатый поиск шлюза нашёл туры.

    Меняет прогон на месте. `probe` подменяется в тестах — судьбу находки решает эта
    логика, и она обязана проверяться офлайн.
    """
    targets = [g for g in scan.gaps
               if g.kind is GapKind.HOTEL and g.catalog_id
               and g.diagnosis in _PROBEABLE]
    if not targets:
        return

    ids = sorted({g.catalog_id for g in targets})
    found = await probe(scan.params, ids)
    if found is None:
        scan.notes.append(
            f"отельные находки ({len(targets)}) не верифицированы прижатой пробой "
            f"шлюза — проба не состоялась")
        return
    if not found:
        scan.notes.append(
            f"отельные находки верифицированы прижатой пробой шлюза: у всех "
            f"{len(ids)} отелей туров действительно нет")
        return

    dropped = [g for g in targets if g.catalog_id in found]
    scan.gaps = [g for g in scan.gaps
                 if not (g.kind is GapKind.HOTEL and g.catalog_id in found)]
    sample = dropped[0]
    log.info("верификация отельных: снято %d из %d (наша выдача была недочитана), "
             "пример %s", len(dropped), len(targets), sample.hotel_name)
    scan.notes.append(
        f"снято отельных находок: {len(dropped)} из {len(targets)} — прижатый поиск "
        f"шлюза НАШЁЛ туры, наша выдача была недочитана (например {sample.hotel_name}); "
        f"остальные {len(targets) - len(dropped)} подтверждены пробой")
