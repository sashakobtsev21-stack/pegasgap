"""Шина событий: что происходит прямо сейчас.

Нужна, потому что опрос состояния раз в несколько секунд отвечает на вопрос «сколько
сделано», но не на вопрос «что происходит». Для круглосуточного воркера второе важнее:
если он молчит десять минут, надо видеть, ждёт он площадку, уперся в квоту или завис.

Два типа событий и разные требования к ним:

* **лог** — поток строк, интересен только «сейчас». Копится в кольцевом буфере, чтобы
  подключившийся позже увидел хвост, а не пустой экран;
* **находка** — факт, который нельзя терять. Хранится в БД, а через шину идёт лишь
  уведомление, чтобы отчёт наполнялся на глазах.

Подписчик, который не успевает читать, теряет события, а не тормозит воркера: очередь
подписчика ограничена, и при переполнении выбрасывается самое старое. Мониторинг не
должен вставать из-за подвисшей вкладки.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime
from typing import Any, Literal

log = logging.getLogger("pegasgap.events")

EventKind = Literal["log", "finding", "case", "state"]

# Хвост лога для тех, кто подключился к уже идущему прогону.
HISTORY_SIZE = 300
# Больше этого числа непрочитанных — подписчик считается отставшим.
SUBSCRIBER_QUEUE = 100


class EventBus:
    """Раздача событий подписчикам. Один экземпляр на процесс."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._history: deque[dict] = deque(maxlen=HISTORY_SIZE)

    def publish(self, kind: EventKind, **payload: Any) -> None:
        """Разослать событие. Не блокирует и не падает, даже если читателей нет."""
        event = {
            "kind": kind,
            "at": datetime.now().isoformat(timespec="seconds"),
            **payload,
        }
        self._history.append(event)
        for queue in list(self._subscribers):
            if queue.full():
                # Отставший подписчик теряет самое старое событие, но не задерживает
                # воркера. Терять хвост лога не жалко: находки живут в БД.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def log(self, message: str, level: str = "info") -> None:
        self.publish("log", message=message, level=level)

    def history(self) -> list[dict]:
        return list(self._history)

    def clear(self) -> int:
        """Забыть накопленные события. Возвращает, сколько выбросили.

        Нужно перед чистым прогоном: лог живёт в памяти процесса, и до сих пор очистить
        его можно было только перезапуском сервера — то есть заодно погасив обход.
        Подписчиков не трогаем: их очереди уже отданы, а новые события пойдут как обычно.
        """
        count = len(self._history)
        self._history.clear()
        return count

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)


# Единая шина процесса: воркер пишет, веб читает.
bus = EventBus()
