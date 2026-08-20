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
  подтверждённое отсутствие.

Дальше поведение зависит от полноты листинга. На ПОЛНОМ несостоявшаяся проба и
кандидаты без id остаются с честными пометками: у стороны есть второе основание —
дочитанный листинг. На НЕДОЧИТАННОМ (режим «отели» с потолком витрины в 50, заикание
пагинации) второго основания нет, проба — единственное: всё неподтверждённое снимается.
Так обратная сторона работает и там, где раньше молчала целиком.
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

    На ПОЛНОМ листинге проба — усиление: не состоялась, находки остаются с честной
    пометкой. На НЕДОЧИТАННОМ листинге (режим «отели» с потолком в 50, заикание
    пагинации) проба — единственное основание стороны: кандидат без id витрины или без
    состоявшейся пробы снимается, потому что «не в листинге» там недоказуемо.

    `probe` подменяется в тестах: судьбу находки решает эта логика, и она обязана
    проверяться офлайн.
    """
    reverse = [g for g in scan.gaps if g.kind is GapKind.REVERSE]
    if not reverse:
        return
    truncated = scan.reference is not None and scan.reference.truncated

    doomed: set[int] = set()          # id() снимаемых находок
    unprovable = [g for g in reverse if not g.reference_hotel_id]
    if truncated and unprovable:
        doomed |= {id(g) for g in unprovable}
        scan.notes.append(
            f"снято кандидатов «нет на Турвизоре»: {len(unprovable)} — листинг неполон, "
            f"а в справочнике Турвизора отель не опознан: проверить нечем")

    targets = [g for g in reverse if g.reference_hotel_id]
    if targets:
        ids = sorted({g.reference_hotel_id for g in targets})
        found = await probe(scan.params, ids)
        if found is None:
            if truncated:
                doomed |= {id(g) for g in targets}
                scan.notes.append(
                    f"снято кандидатов «нет на Турвизоре»: {len(targets)} — листинг "
                    f"неполон и прижатая проба не состоялась, утверждать нечего")
            else:
                scan.notes.append(
                    f"обратные находки ({len(targets)}) не верифицированы прижатой "
                    f"пробой — проба не состоялась; «нет в листинге» может означать "
                    f"недочитанный листинг")
        else:
            dropped = [g for g in targets if g.reference_hotel_id in found]
            confirmed = len(targets) - len(dropped)
            doomed |= {id(g) for g in dropped}
            if dropped:
                sample = dropped[0]
                log.info("верификация обратных: снято %d из %d, пример %s",
                         len(dropped), len(targets), sample.hotel_name)
                scan.notes.append(
                    f"снято обратных находок: {len(dropped)} из {len(targets)} — "
                    f"прижатый поиск НАШЁЛ туры, выдача Турвизора была неполна "
                    f"(например {sample.hotel_name}); остальные {confirmed} "
                    f"подтверждены пробой")
            else:
                scan.notes.append(
                    f"обратные находки верифицированы прижатой пробой: у всех "
                    f"{len(ids)} отелей туров действительно нет")

    if doomed:
        scan.gaps = [g for g in scan.gaps if id(g) not in doomed]
