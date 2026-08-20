"""Сверка номеров у ценовых находок — последний рубеж основы сравнения.

Заезд и питание сравнение уже прижало из данных поиска. Номер так прижать нельзя: в
поисковой выдаче витрины его имени нет, только внутренний id. Зато имя отдаёт карточка
конкретного тура (`actualize.php`) — и находок, которым это нужно, единицы на прогон.

Поэтому сверка точечная и идёт ПОСЛЕ разбора: для каждой ценовой находки запрашивается
номер того самого тура витрины, с которым сравнивались, и если категории заведомо разные
(промо против стандарта, эконом против сюита) — находка снимается: это расхождение
состава, а не цены площадок. Правило опровергающее, как и всё сравнение номеров: нет
сигнала — нет и снятия, снять находку из-за непрочитанного названия значило бы прятать
настоящие расхождения (см. `basis.rooms_differ`).

Не удалось узнать номер — находка остаётся с пустым `reference_room`: отчёт честно
покажет «номер витрины не сверен», а не сделает вид, что сверил.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from pegasgap.basis import rooms_differ
from pegasgap.models import GapKind, ScanResult
from pegasgap.providers.tourvisor_api import fetch_tour_room

log = logging.getLogger("pegasgap.roomcheck")

Fetcher = Callable[[str | None], Awaitable[str | None]]


async def pin_rooms(scan: ScanResult, fetch: Fetcher = fetch_tour_room) -> None:
    """Дозаполнить ценовым находкам номер витрины и снять пары разных категорий.

    Меняет прогон на месте. `fetch` подменяется в тестах — сама логика решает судьбу
    находки и обязана проверяться офлайн.
    """
    if not any(g.kind is GapKind.PRICE for g in scan.gaps):
        return

    kept, dropped = [], []
    for gap in scan.gaps:
        if gap.kind is not GapKind.PRICE:
            kept.append(gap)
            continue
        room = await fetch(gap.reference_tour_id)
        if room:
            gap.reference_room = room
            if rooms_differ(room, gap.checked_room):
                dropped.append(gap)
                continue
        kept.append(gap)

    if dropped:
        scan.gaps = kept
        sample = dropped[0]
        log.info("сверка номеров сняла %d ценовых находок, пример: %s («%s» ≠ «%s»)",
                 len(dropped), sample.hotel_name, sample.reference_room,
                 sample.checked_room)
        scan.notes.append(
            f"снято ценовых расхождений: {len(dropped)} — номера сторон разных категорий "
            f"(например {sample.hotel_name}: у витрины «{sample.reference_room}», "
            f"у нас «{sample.checked_room}»)")
