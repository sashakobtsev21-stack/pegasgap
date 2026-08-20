"""Сверка номеров у ценовых находок — последний рубеж основы сравнения.

Заезд и питание сравнение прижало из данных поиска. Номер так прижать нельзя: в
поисковой выдаче витрины его имени нет, только внутренний id. Зато имя отдаёт карточка
конкретного тура (`actualize.php`) — и находок, которым это нужно, единицы на прогон.

Сверка требует ПОЛОЖИТЕЛЬНОГО совпадения, а не отсутствия противоречия. Первая версия
умела только опровергать, и пара «Jasmine Pool View» против «camelia family superior»
проходила в отчёт: ни одного словарного слова категории — опровергнуть нечем. Но это
заведомо разные номера, и «цена расходится» на такой паре не значит ничего.

Когда наш минимум оказался другим номером, находка не выбрасывается вслепую — у нас есть
разрез предложений, и в нём ищется ТОТ ЖЕ номер, что показала витрина:

* нашёлся и цены сходятся — находка снимается: разница была номером, не площадками;
* нашёлся и цены расходятся — находка ОСТАЁТСЯ, но сравнивает одинаковые номера:
  наша цена и номер заменяются на найденные;
* не нашёлся — находка снимается с заметкой: сравнивать не с чем, а «у нас нет такого
  номера» — вопрос ассортимента, не цены.

Не удалось узнать номер витрины — находка остаётся с пустым `reference_room`: отчёт
честно покажет «номер витрины не сверен», а не сделает вид, что сверил.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from pegasgap.basis import rooms_alike
from pegasgap.gaps import PRICE_TOLERANCE_PCT
from pegasgap.models import DayOffer, GapKind, HotelGap, ScanResult
from pegasgap.providers.tourvisor_api import fetch_tour_room

log = logging.getLogger("pegasgap.roomcheck")

Fetcher = Callable[[str | None], Awaitable[str | None]]


def _same_room_offer(scan: ScanResult, gap: HotelGap, room: str) -> DayOffer | None:
    """Наше самое дешёвое предложение ТОГО ЖЕ номера на тот же заезд и питание."""
    if scan.checked is None or gap.checked_checkin is None:
        return None
    wanted = gap.matched_name or gap.hotel_name
    for hotel in scan.checked.hotel_offers:
        if hotel.hotel_name != wanted:
            continue
        candidates = [
            offer for offer in hotel.day_offers.get(gap.checked_checkin, [])
            if offer.meal == gap.checked_meal and rooms_alike(offer.room, room)
        ]
        return min(candidates, key=lambda o: o.price) if candidates else None
    return None


async def pin_rooms(scan: ScanResult, fetch: Fetcher = fetch_tour_room) -> None:
    """Дозаполнить ценовым находкам номер витрины и оставить только пары одного номера.

    Меняет прогон на месте. `fetch` подменяется в тестах — сама логика решает судьбу
    находки и обязана проверяться офлайн.
    """
    if not any(g.kind is GapKind.PRICE for g in scan.gaps):
        return

    kept: list[HotelGap] = []
    settled: list[HotelGap] = []    # цены на один номер сошлись — разница была номером
    missing: list[HotelGap] = []    # такого номера у нас нет — вопрос ассортимента
    for gap in scan.gaps:
        if gap.kind is not GapKind.PRICE:
            kept.append(gap)
            continue
        room = await fetch(gap.reference_tour_id)
        if not room:
            kept.append(gap)        # не сверили — честно оставляем несверенным
            continue
        gap.reference_room = room
        if rooms_alike(room, gap.checked_room):
            kept.append(gap)
            continue

        ours = _same_room_offer(scan, gap, room)
        if ours is None:
            missing.append(gap)
            continue
        diff = float((ours.price - gap.reference_price) / gap.reference_price * 100)
        if abs(diff) <= PRICE_TOLERANCE_PCT:
            settled.append(gap)
            continue
        # Настоящее расхождение на ОДИНАКОВОМ номере — им находка и становится.
        gap.checked_price = ours.price
        gap.checked_room = ours.room
        gap.note = (f"разница {diff:+.1f}% на одинаковом номере; минимумы площадок "
                    f"приходились на разные номера")
        kept.append(gap)

    scan.gaps = kept
    if settled:
        sample = settled[0]
        scan.notes.append(
            f"снято ценовых расхождений: {len(settled)} — на одинаковом номере цены "
            f"сходятся, разница была номером (например {sample.hotel_name}: Турвизор "
            f"показал «{sample.reference_room}» дешевле нашего «{sample.checked_room}»)")
    if missing:
        sample = missing[0]
        scan.notes.append(
            f"снято ценовых расхождений: {len(missing)} — показанного Турвизором номера у нас нет, "
            f"сравнивать не с чем (например {sample.hotel_name}: "
            f"«{sample.reference_room}»)")
    if settled or missing:
        log.info("сверка номеров: снято %d (цены сошлись) и %d (номера у нас нет)",
                 len(settled), len(missing))
