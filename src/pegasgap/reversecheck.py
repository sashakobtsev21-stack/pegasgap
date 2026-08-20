"""Верификация обратных находок прижатой пробой — «нет в листинге» ещё не «нет».

Обратная находка рождается из сравнения с ЛИСТИНГОМ витрины, а листинг годен для
утверждения «отель есть», но не «отеля нет»: под нагрузкой его пагинация заикается,
«прирост иссяк» случается раньше настоящего конца, и наш признак полноты этого не
видит. Живой замер, из-за которого стадия появилась: у прогона по Турции 19 из 52
находок «туров нет» оказались фантомами — прижатый к отелям поиск туры нашёл.

Поэтому каждый кандидат в «отеля нет на Турвизоре», у которого известен id в словаре
витрины, дополнительно проверяется прижатым поиском (пачками по 40 id за запрос —
фильтр серверный, подтверждён контрольной пробой):

* туры нашлись — находка снимается: листинг был неполон, отель на витрине есть;
* туров нет — находка остаётся, и теперь это не «не увидели в листинге», а
  подтверждённое отсутствие;
* проба не состоялась — находки остаются с заметкой «не верифицированы»: сбой сети не
  повод ни снимать, ни утверждать.

Кандидаты без id (в словаре витрины отель не опознан) пробе недоступны — их честность
держит диагноз «нет в словаре» / «возможно, другое имя».
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from pegasgap.models import GapKind, ScanResult, SearchParams
from pegasgap.providers.tourvisor_api import probe_hotels_with_tours

log = logging.getLogger("pegasgap.reversecheck")

Probe = Callable[[SearchParams, list[int]], Awaitable[set[int] | None]]


async def verify_reverse(scan: ScanResult, probe: Probe = probe_hotels_with_tours) -> None:
    """Снять обратные находки, у которых прижатый поиск нашёл туры. Меняет прогон.

    `probe` подменяется в тестах: судьбу находки решает эта логика, и она обязана
    проверяться офлайн.
    """
    targets = [g for g in scan.gaps
               if g.kind is GapKind.REVERSE and g.reference_hotel_id]
    if not targets:
        return

    ids = sorted({g.reference_hotel_id for g in targets})
    found = await probe(scan.params, ids)
    if found is None:
        scan.notes.append(
            f"обратные находки ({len(targets)}) не верифицированы прижатой пробой — "
            f"проба не состоялась; «нет в листинге» может означать недочитанный листинг")
        return

    if not found:
        scan.notes.append(
            f"обратные находки верифицированы прижатой пробой: у всех {len(ids)} "
            f"отелей туров действительно нет")
        return

    dropped = [g for g in targets if g.reference_hotel_id in found]
    scan.gaps = [g for g in scan.gaps
                 if not (g.kind is GapKind.REVERSE and g.reference_hotel_id in found)]
    sample = dropped[0]
    log.info("верификация обратных: снято %d из %d (листинг был неполон), пример %s",
             len(dropped), len(targets), sample.hotel_name)
    scan.notes.append(
        f"снято обратных находок: {len(dropped)} из {len(targets)} — прижатый поиск "
        f"НАШЁЛ туры, листинг витрины был неполон (например {sample.hotel_name}); "
        f"остальные {len(targets) - len(dropped)} подтверждены пробой")
