"""Единый интерфейс провайдера площадки и реестр провайдеров.

Добавление новой площадки = реализовать `SearchProvider` и зарегистрировать его.
Оркестратор и слой сравнения работают только с этим интерфейсом и не знают
о конкретных сайтах.
"""

from __future__ import annotations

import asyncio
import base64
import glob
import os
import time
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from pegasgap.models import HotelOffer, ProviderResult, SearchParams

# Колбэк живого кадра: (имя площадки, jpeg в base64) → корутина.
FrameCallback = Callable[[str, str], Awaitable[None]]

# Общий «настольный Chrome» UA для провайдеров. До 2026-06 Sletat и Tourvisor
# полагались на Playwright-дефолт, который при headless=True содержит «HeadlessChrome»
# в строке — антибот-системы это видят и могут отдавать заглушку/CAPTCHA. Явный UA
# снимает различие headless/headful и unify'ит поведение площадок.
DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def dedup_hotel_offers(offers: list[HotelOffer]) -> list[HotelOffer]:
    """Дедупликация HotelOffer по (hotel_name.lower().strip(), price) (P2-c audit-3).

    Сайты с lazy-load (Travelata/Level/Ostrovok) при долистывании выдачи могут отдавать
    одни и те же карточки повторно. До 2026-06 дубликаты ехали как разные офферы,
    «лучшая цена» считалась бы корректно, но списки выдачи раздувались. Сохраняем
    ПЕРВЫЙ оффер для каждой пары (имя, цена) — порядок не страдает.
    """
    seen: set[tuple[str, str]] = set()
    out = []
    for o in offers:
        key = ((o.hotel_name or "").strip().lower(), str(o.price))
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    return out


async def _frame_pump(name: str, page, on_frame: FrameCallback, interval_ms: int = 1400) -> None:
    """Периодически снимать скриншот вкладки и отдавать его как base64 jpeg.

    Лёгкие кадры (jpeg, низкое качество, только видимая область) — чтобы показать
    в вебе «в реальном времени», что сейчас происходит на площадке. Любые ошибки
    скриншота (навигация/закрытая страница) глотаем — это не должно ронять поиск.
    """
    while True:
        try:
            # quality повыше — текст формы площадки читабелен в live-окне (окно
            # показывает кадр на всю ширину через object-cover).
            data = await page.screenshot(type="jpeg", quality=62, full_page=False)
            await on_frame(name, base64.b64encode(data).decode("ascii"))
        except Exception:
            pass
        await asyncio.sleep(interval_ms / 1000)


def start_frame_pump(name: str, page, on_frame: FrameCallback | None) -> asyncio.Task | None:
    """Запустить фоновую отдачу живых кадров (если колбэк задан)."""
    if on_frame is None:
        return None
    return asyncio.create_task(_frame_pump(name, page, on_frame))


async def stop_frame_pump(task: asyncio.Task | None) -> None:
    """Остановить отдачу живых кадров."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except BaseException:
        pass


async def capture_top(page, path: str, max_height: int = 1500) -> str | None:
    """Снять верхнюю часть страницы во всю ширину вьюпорта.

    Берём форму поиска + «нашлось N» + первые результаты, но не всю бесконечную
    ленту: информативно и читабельно. Высота = min(высота контента, max_height),
    но не меньше высоты вьюпорта.
    """
    from pathlib import Path

    try:
        Path("screenshots").mkdir(exist_ok=True)
        try:
            await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        vp = page.viewport_size or {"width": 1920, "height": 1080}
        try:
            content_h = await page.evaluate(
                "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
            )
        except Exception:
            content_h = vp["height"]
        height = max(vp["height"], min(int(content_h or vp["height"]), max_height))
        # full_page=True + clip: позволяет захватить область ниже видимой части
        # (без него clip обрезается высотой вьюпорта).
        await page.screenshot(
            path=path, full_page=True,
            clip={"x": 0, "y": 0, "width": vp["width"], "height": height},
        )
        return path
    except Exception:
        return None


def prune_screenshots(
    directory: str = "screenshots", max_age_days: float = 10.0, keep_max: int = 250
) -> int:
    """Best-effort очистка скриншотов прогонов: удалить старше `max_age_days` И оставить
    не более `keep_max` самых свежих (иначе папка растёт безгранично — каждый поиск каждой
    площадки пишет ~1 МБ). Возвращает число удалённых; ошибки/отсутствие папки глотаем."""
    try:
        files = [(p, os.path.getmtime(p)) for p in glob.glob(os.path.join(directory, "*"))
                 if os.path.isfile(p)]
    except Exception:
        return 0
    removed = 0
    cutoff = time.time() - max_age_days * 86400
    survivors: list[tuple[str, float]] = []
    for path, mtime in files:
        if mtime < cutoff:
            try:
                os.remove(path)
                removed += 1
            except Exception:
                pass
        else:
            survivors.append((path, mtime))
    survivors.sort(key=lambda x: x[1], reverse=True)  # свежие первыми
    for path, _ in survivors[keep_max:]:
        try:
            os.remove(path)
            removed += 1
        except Exception:
            pass
    return removed


@runtime_checkable
class SearchProvider(Protocol):
    """Контракт площадки поиска.

    Реализация инкапсулирует драйвинг браузера и парсинг, но наружу отдаёт
    только унифицированный `ProviderResult`.
    """

    name: str

    async def search(self, params: SearchParams) -> ProviderResult:
        """Выполнить поиск с заданными параметрами и вернуть результат."""
        ...


_REGISTRY: dict[str, type[SearchProvider]] = {}


def register_provider(name: str):
    """Декоратор регистрации класса-провайдера под заданным именем."""

    def wrapper(cls: type[SearchProvider]) -> type[SearchProvider]:
        key = name.lower()
        if key in _REGISTRY:
            raise ValueError(f"Провайдер '{name}' уже зарегистрирован")
        _REGISTRY[key] = cls
        return cls

    return wrapper


def get_provider(name: str) -> type[SearchProvider]:
    key = name.lower()
    if key not in _REGISTRY:
        raise KeyError(f"Провайдер '{name}' не найден. Доступны: {list_providers()}")
    return _REGISTRY[key]


def list_providers() -> list[str]:
    return sorted(_REGISTRY)
