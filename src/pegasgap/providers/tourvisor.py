"""Провайдер площадки Tourvisor (tourvisor.ru) на Playwright.

Селекторы перенесены из исходного Selenium-скрипта (проверены на живом сайте)
и дополнены поддержкой детей с возрастом. Драйвинг браузера и парсинг
инкапсулированы здесь; наружу отдаётся унифицированный ProviderResult.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from decimal import Decimal

from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeout

from pegasgap.models import (
    HotelOffer,
    NotApplicableError,
    Offer,
    OperatorOffer,
    ProviderResult,
    SearchParams,
    is_not_applicable_error,
)
from pegasgap.names import (
    OPERATOR_ALIASES as _TV_OPERATOR_ALIASES,
)
from pegasgap.names import (
    filter_offers_by_operators,
    operator_matches,
)
from pegasgap.names import (
    operator_norm as _operator_norm,
)
from pegasgap.providers._formcheck import (
    UNKNOWN,
    FormVerificationError,
    text_contains,
)
from pegasgap.providers.base import (
    DESKTOP_CHROME_UA,
    dedup_hotel_offers,
    register_provider,
    start_frame_pump,
    stop_frame_pump,
)
from pegasgap.providers.base import (
    capture_top as _capture_top,
)
from pegasgap.urlcheck import verify_tourvisor_search_url

log = logging.getLogger("pegasgap.providers.tourvisor")

_MONTHS_RU = {
    1: "ЯНВАРЬ", 2: "ФЕВРАЛЬ", 3: "МАРТ", 4: "АПРЕЛЬ",
    5: "МАЙ", 6: "ИЮНЬ", 7: "ИЮЛЬ", 8: "АВГУСТ",
    9: "СЕНТЯБРЬ", 10: "ОКТЯБРЬ", 11: "НОЯБРЬ", 12: "ДЕКАБРЬ",
}

_OPERATOR_MAP = {
    "anex": "Anex",
    "biblioglobus": "Biblioglobus",
    "funsun": "FUN&SUN (TUI)",
    "travelata": "Travelata",
    "coral": "Coral",
    "sunmar": "Sunmar",
    "pegas": "Pegas Touristik",
}

# Нормализация и сопоставление имён операторов живут в `pegasgap.names` — там же, откуда
# их берёт классификация пропусков. Единственный источник намеренно: разъехавшийся
# матчинг означал бы, что на форме выбрали одного оператора, а в выдаче отфильтровали
# другого. JS-зеркало этой логики остаётся внутри `_select_operators` (оно исполняется
# в браузере и общим кодом быть не может) — при правке менять обе стороны.


# Подписи городов вылета на Tourvisor сокращённые и отличаются от Sletat.
_DEPARTURE_MAP = {
    "Санкт-Петербург": "С.Петербург",
    "Нижний Новгород": "Н.Новгород",
    "Минеральные Воды": "Мин.Воды",
    "Набережные Челны": "Наб.Челны",
    "Петропавловск-Камчатский": "П.Камчатский",
    "Южно-Сахалинск": "Ю.Сахалинск",
    "Ростов-на-Дону": "Ростов-на-Дону",
}


def _departure_candidates(city: str) -> list[str]:
    """Возможные подписи города вылета на Tourvisor: сокращённое имя (если знаем),
    полное имя и последнее слово («С.Петербург»/«Петербург» ← «Санкт-Петербург»).
    Используется и для клика по городу, и для терпимой сверки формы."""
    cands = [c for c in (_DEPARTURE_MAP.get(city), city) if c]
    last = city.replace("-", " ").split()[-1]
    if len(last) >= 4 and last not in cands:
        cands.append(last)
    return cands


def _parse_price(text: str | None) -> Decimal | None:
    # `or ""` — guard на None из row.get("price") при пустой ценовой ячейке DOM.
    # До 2026-06 здесь `re.sub(pattern, None)` падал TypeError'ом; единственный
    # провайдер где этой защиты не было (sletat/travelata/level/ostrovok делают так).
    digits = re.sub(r"[^\d]", "", text or "")
    return Decimal(digits) if digits else None


def build_offers(provider_name: str, rows: list[dict]) -> list[Offer]:
    """Собрать офферы из «сырых» строк панели операторов [{name, price}].

    Чистая функция (без браузера): парсит цену и дедуплицирует по оператору,
    оставляя минимальную цену. Вынесена для модульного тестирования.
    """
    best: dict[str, tuple[Decimal, str]] = {}
    for row in rows:
        name = (row.get("name") or "").strip()
        raw = (row.get("price") or "").strip()
        price = _parse_price(raw)
        if not name or price is None:
            continue
        if name not in best or price < best[name][0]:
            best[name] = (price, raw)
    return [
        Offer(provider=provider_name, operator=name, price=price, currency="RUB", raw_label=raw)
        for name, (price, raw) in best.items()
    ]


def _split_name_stars(title: str) -> tuple[str, int | None]:
    """Из «Mert Seaside Hotel 3*» → ('Mert Seaside Hotel', 3)."""
    m = re.search(r"(\d)\s*\*", title)
    stars = int(m.group(1)) if m else None
    name = re.sub(r"\d\s*\*\s*$", "", title).strip() if m else title.strip()
    return name, stars


# Публичные алиасы для testkit/catalog.py (P2-b audit-3): закрывают layering-issue,
# когда тест-слой импортировал приватные имена. См. sletat.py.
OPERATOR_MAP = _OPERATOR_MAP
parse_price = _parse_price
split_name_stars = _split_name_stars
departure_candidates = _departure_candidates


def build_hotel_offers(provider_name: str, rows: list[dict]) -> list[HotelOffer]:
    """Собрать HotelOffer из «сырых» карточек [{title, subtitle, rating, price}].

    Чистая функция (без браузера) для модульного тестирования.
    """
    out: list[HotelOffer] = []
    for row in rows:
        title = (row.get("title") or "").strip()
        price = _parse_price(row.get("price") or "")
        if not title or price is None:
            continue
        name, stars = _split_name_stars(title)
        rating = None
        rraw = (row.get("rating") or "").replace(",", ".").strip()
        m = re.search(r"\d+(\.\d+)?", rraw)
        if m:
            try:
                rating = float(m.group(0))
            except ValueError:
                rating = None
        out.append(
            HotelOffer(
                provider=provider_name,
                hotel_name=name,
                stars=stars,
                rating=rating,
                destination=(row.get("subtitle") or "").strip() or None,
                price=price,
                currency="RUB",
                raw_label=(row.get("price") or "").strip(),
            )
        )
    return dedup_hotel_offers(out)         # P2-c: lazy-load дублирует карточки


# Предохранитель от бесконечной ленты выдачи. Лимит сознательно большой: обрезка делает
# прогон недостоверным (недогруженный отель неотличим от отсутствующего), поэтому упираться
# в него должно быть исключением, а не нормой.
MAX_HOTELS = int(os.environ.get("PEGASGAP_MAX_HOTELS") or 300)
# Сколько раз жать «Найти больше туров». Каждое нажатие добавляет порцию карточек, так что
# верхнюю границу задаёт MAX_HOTELS; это лишь страховка от зацикливания.
_LOAD_ROUNDS = 60

# Поля SearchParams, которые tourvisor.ru НЕ выставляет в UI формы и API —
# у пользователя они выглядят как «применилось», но Tourvisor их игнорирует.
# Возвращаем в ProviderResult.unsupported_filters → потребитель видит явно,
# а не молча. Если когда-то реализуем — убрать соответствующий ключ отсюда.
_TOURVISOR_UNSUPPORTED = (
    "direct_only", "no_stops", "with_transfer",
    "instant_confirmation", "hotel_rating_min", "hotel_types",
)


def _unsupported_used(params: SearchParams) -> list[str]:
    """Имена unsupported-фильтров, которые ПОЛЬЗОВАТЕЛЬ реально задал (а не default)."""
    used: list[str] = []
    for f in _TOURVISOR_UNSUPPORTED:
        v = getattr(params, f, None)
        # bool=True / непустой список / не-None число — считаем «задано пользователем».
        given = (
            (isinstance(v, bool) and v)
            or (isinstance(v, list) and v)
            or (v is not None and not isinstance(v, bool | list))
        )
        if given:
            used.append(f)
    return used


@register_provider("tourvisor")
class TourvisorProvider:
    """Поиск туров на tourvisor.ru."""

    name = "tourvisor"
    URL = "https://tourvisor.ru/search.php"
    HOMEPAGE_URL = "https://tourvisor.ru/"          # туры: навигирует на /tours/{country}/{city}?params
    HOTELS_URL = "https://tourvisor.ru/poisk-otelej"  # форма «Поиск отелей» (без перелёта)

    def __init__(self, headless: bool = False, timeout_ms: int = 20_000) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        # Колбэк живых кадров (устанавливается оркестратором); None = не транслировать.
        self.on_frame = None

    async def search(self, params: SearchParams) -> ProviderResult:
        # Tourvisor не поддерживает часть фильтров — сообщаем пользователю явно,
        # чтобы он видел в результате, а не думал что фильтр применился.
        unsupported = _unsupported_used(params)
        if unsupported:
            log.warning("Tourvisor: фильтры %s не поддерживаются и не применены",
                        ", ".join(unsupported))
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled", "--window-size=1600,1080"],
            )
            # Вьюпорт 1600: сайт целиком по ширине, но крупнее (читабельнее) в live-окне и скриншоте.
            # Явный desktop-UA: при headless=True по умолчанию шлётся «HeadlessChrome»,
            # антибот-системы это видят. См. base.DESKTOP_CHROME_UA.
            context = await browser.new_context(
                viewport={"width": 1600, "height": 1080},
                user_agent=DESKTOP_CHROME_UA,
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()
            page.set_default_timeout(self.timeout_ms)
            # Навигации даём больше времени, чем ожиданиям элементов: первая загрузка
            # тяжёлая, при разовых тормозах сети/сайта 20 с впритык и весь поиск падает на goto.
            page.set_default_navigation_timeout(max(self.timeout_ms, 45_000))
            pump = start_frame_pump(self.name, page, self.on_frame)
            start = time.monotonic()
            nav_url: str | None = None  # URL поиска, как только произошёл переход на /tours/
            try:
                if params.search_mode == "hotels":
                    await page.goto(self.HOTELS_URL, wait_until="domcontentloaded")
                    await page.wait_for_timeout(1500)
                    log.info("Tourvisor: сайт открыт (отели), задаю параметры…")
                    await self._fill_form(page, params)
                    await self._verify_and_fix(page, params)
                    await self._click_search(page)
                    start = time.monotonic()
                    log.info("Tourvisor: поиск запущен, жду полной загрузки результатов…")
                    await self._wait_for_completion(page)
                    dur = time.monotonic() - start  # скорость = клик «Найти» → выдача (до парса)
                    truncated = await self._load_all_hotels(page, MAX_HOTELS)
                    hotel_offers = await self._parse_hotels(page)
                    # Операторы (best-effort): панель операторов, если есть; отель — по цене.
                    try:
                        op_raw = await self._parse_operators(page)
                    except Exception:
                        op_raw = []
                    filter_ok = await self._verify_operator_filter(page, params.operators)
                    if params.operators:
                        op_raw = filter_offers_by_operators(op_raw, params.operators)
                    operator_offers = self._to_operator_offers(op_raw, hotel_offers)
                    shot = await self._safe_screenshot(page)
                    log.info("Tourvisor: результаты получены — %d отелей за %.1f с",
                             len(hotel_offers), dur)
                    return ProviderResult(
                        provider=self.name, success=bool(hotel_offers),
                        duration_seconds=dur,
                        search_mode="hotels", hotel_offers=hotel_offers,
                        operator_offers=operator_offers,
                        operator_filter_verified=filter_ok,
                        truncated=truncated,
                        # ссылка «поиск»: URL страницы отелей после поиска
                        search_url=page.url,
                        screenshot_path=shot,
                        error=None if hotel_offers else "Предложений не найдено по заданным параметрам.",
                        unsupported_filters=unsupported,
                    )

                # Туры: главная форма навигирует на /tours/{country}/{city}?params (URL со всеми
                # базовыми параметрами). Расширенные фильтры применяем уже на /tours/.
                await page.goto(self.HOMEPAGE_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)
                log.info("Tourvisor: сайт открыт, задаю параметры…")
                await self._fill_tours_basic(page, params)
                await self._verify_and_fix(page, params)
                start = time.monotonic()
                await self._click_search(page)
                try:
                    await page.wait_for_url("**/tours/**", timeout=30_000)
                except PWTimeout:
                    pass
                if "/tours/" in page.url:
                    nav_url = page.url  # ссылка на поиск появляется после перехода на /tours/
                await page.wait_for_timeout(1500)
                log.info("Tourvisor: поиск запущен, жду полной загрузки результатов…")
                await self._apply_tours_advanced(page, params)
                await self._wait_for_completion(page)
                dur = time.monotonic() - start  # скорость = клик «Найти» → выдача (до парса)

                search_url = page.url
                url_problems = verify_tourvisor_search_url(search_url, params)
                offers = await self._parse_operators(page)
                filter_ok = await self._verify_operator_filter(page, params.operators)
                # Подстраховка поверх проверки: даже если фильтр не удержался, в сравнение
                # не должны попасть офферы чужих операторов.
                if params.operators:
                    kept = filter_offers_by_operators(offers, params.operators)
                    if len(kept) != len(offers):
                        log.info("Tourvisor: фильтр операторов неточен — оставляю %d из %d по запросу",
                                 len(kept), len(offers))
                    offers = kept
                if url_problems:
                    return ProviderResult(
                        provider=self.name, success=False,
                        duration_seconds=dur,
                        search_mode="tours", search_url=search_url,
                        error="URL-параметры не совпали: " + "; ".join(
                            f"{f}: ожидали {e!r}, получили {a!r}" for f, e, a in url_problems),
                        unsupported_filters=unsupported,
                    )
                # Раньше здесь бралась только первая десятка — этого хватало для показа
                # выдачи, но для поиска пропусков обрезка означала бы, что весь хвост
                # каталога числится отсутствующим. Берём всё, что удалось догрузить.
                truncated = await self._load_all_hotels(page, MAX_HOTELS)
                hotels = await self._parse_hotels(page)
                operator_offers = self._to_operator_offers(offers, hotels)
                shot = await self._safe_screenshot(page)
                log.info("Tourvisor: результаты получены — %d предложений, %d отелей за %.1f с",
                         len(offers), len(hotels), dur)
                return ProviderResult(
                    provider=self.name, success=bool(offers),
                    duration_seconds=dur,
                    search_mode="tours", offers=offers, hotel_offers=hotels,
                    operator_offers=operator_offers,
                    operator_filter_verified=filter_ok,
                    truncated=truncated,
                    search_url=search_url,
                    screenshot_path=shot,
                    error=None if offers else "Предложений не найдено по заданным параметрам.",
                    unsupported_filters=unsupported,
                )
            except Exception as exc:
                log.warning("tourvisor search failed (mode=%s): %s: %s",
                            params.search_mode, type(exc).__name__, exc)
                shot = await self._safe_screenshot(page)
                # если поиск уже начинался (был переход на /tours/) — отдаём ссылку на него
                if nav_url is None and "/tours/" in (page.url or ""):
                    nav_url = page.url
                msg = str(exc)
                # «не обслуживает такой запрос» — детерминированный отказ (не сбой):
                # чистым текстом, без префикса типа исключения. Первичен тип
                # NotApplicableError; regex — фолбэк для текстов из вложенных слоёв.
                err = (msg if isinstance(exc, NotApplicableError) or is_not_applicable_error(msg)
                       else f"{type(exc).__name__}: {msg}")
                return ProviderResult(
                    provider=self.name,
                    success=False,
                    duration_seconds=time.monotonic() - start,
                    search_mode=params.search_mode,
                    error=err,
                    screenshot_path=shot,
                    search_url=nav_url,
                    unsupported_filters=unsupported,
                )
            finally:
                await stop_frame_pump(pump)
                await browser.close()

    # --- заполнение формы ---

    async def _fill_tours_basic(self, page: Page, params: SearchParams) -> None:
        """Базовые поля на главной форме (она навигирует на /tours/...).

        На главной есть: вылет, страна, даты, ночи, туристы, звёзды, питание.
        Бюджет/операторы/курорт — только на /tours/, их применяем после навигации.
        """
        async def opt(coro) -> None:
            try:
                await coro
            except Exception:
                pass

        await self._select_departure_city(page, params.departure_city)
        await self._select_country(page, params.destination_country)
        await self._select_dates(page, params.date_from, params.date_to)
        await self._select_nights(page, params.nights_min, params.nights_max)
        await self._select_tourists(page, params.adults, params.children_ages)
        if params.hotel_stars:
            await opt(self._select_stars(page, params.hotel_stars))
        if params.meals:
            await opt(self._select_meal(page, params.meals[0]))

    async def _apply_tours_advanced(self, page: Page, params: SearchParams) -> None:
        """Расширенные фильтры на странице /tours/ (после навигации) + перезапуск поиска."""
        async def opt(coro) -> None:
            try:
                await coro
            except Exception:
                pass

        advanced = bool(params.operators or params.resorts or params.charter_only
                        or params.price_min is not None or params.price_max is not None)
        if not advanced:
            return
        if params.operators:
            await opt(self._select_operators(page, params.operators))
        if params.resorts:
            await opt(self._select_resorts(page, params.resorts))
        if params.price_min is not None or params.price_max is not None:
            await opt(self._set_budget(page, params.price_min, params.price_max))
        if params.charter_only:
            await opt(self._toggle_charter(page, params.charter_only))
        await opt(self._click_search(page))
        await page.wait_for_timeout(1500)

    async def _fill_form(self, page: Page, params: SearchParams) -> None:
        # Используется для режима «Отели» (/poisk-otelej). Туры идут через _fill_tours_basic.
        hotels = params.search_mode == "hotels"

        async def opt(coro) -> None:
            # В режиме «Отели» (/poisk-otelej) часть полей иная/скрыта — делаем шаги мягкими.
            try:
                await coro
            except Exception:
                pass

        if hotels:
            # Страница /poisk-otelej: вылет «Без перелёта»; направление — через «Направление».
            await self._select_destination_hotels(page, params.destination_country)
            await opt(self._select_dates(page, params.date_from, params.date_to))
            await opt(self._select_nights(page, params.nights_min, params.nights_max))
        else:
            await self._select_departure_city(page, params.departure_city)
            await self._select_country(page, params.destination_country)
            await self._select_dates(page, params.date_from, params.date_to)
            await self._select_nights(page, params.nights_min, params.nights_max)

        await self._select_tourists(page, params.adults, params.children_ages)
        if params.hotel_stars:
            await opt(self._select_stars(page, params.hotel_stars))
        if params.meals:
            await opt(self._select_meal(page, params.meals[0]))
        if params.resorts:
            await opt(self._select_resorts(page, params.resorts))
        if params.price_min is not None or params.price_max is not None:
            await opt(self._set_budget(page, params.price_min, params.price_max))
        if params.operators:
            if hotels:
                await opt(self._select_operators(page, params.operators))
            else:
                await self._select_operators(page, params.operators)
        if hotels:
            await opt(self._toggle_charter(page, params.charter_only))
        else:
            await self._toggle_charter(page, params.charter_only)

    async def _select_destination_hotels(self, page: Page, destination: str) -> None:
        """Направление на /poisk-otelej: ввести страну/курорт/отель и выбрать из подсказок."""
        await page.click("div.TVHotelSearchFilter")
        inp = page.locator(".TVHotelTourSearchInput input")
        await inp.click()
        await inp.fill("")
        await inp.type(destination, delay=60)
        await page.wait_for_timeout(1200)
        await page.click(
            f"xpath=//div[contains(@class,'TVListBoxItem')]"
            f"[.//div[contains(@class,'TVSearchInputResultItemTitle') and contains(text(),'{destination}')]][1]"
        )
        await page.wait_for_timeout(600)

    async def _select_stars(self, page: Page, stars: list[int]) -> None:
        # «Класс отеля» = минимальная звёздность; инлайн-кнопки .TVStarsSelectItem (1..5).
        target = min(stars)
        items = page.locator("div.TVStarsFilter .TVStarsSelectItem")
        if await items.count() >= target:
            await items.nth(target - 1).click()
            await page.wait_for_timeout(300)

    async def _select_meal(self, page: Page, code: str) -> None:
        # Питание — радио-дропдаун; выбираем по префиксу (BB/HB/FB/AI/UAI).
        await page.click("div.TVMealFilter")
        await page.wait_for_timeout(600)
        item = page.locator(
            f"xpath=//div[contains(@class,'TVInputRadio')]"
            f"[.//t-span[contains(@class,'TVRadioGroupSelectItemPrefix') and normalize-space(text())='{code}']]"
        )
        if await item.count():
            await item.first.click()
            await page.wait_for_timeout(300)
        await page.mouse.click(10, 10)

    async def _select_resorts(self, page: Page, resorts: list[str]) -> None:
        # Курорт — дерево чекбоксов TVCheckboxTreeItem → TVCheckBox по названию.
        await page.click("xpath=//div[contains(@class,'TVResortTreeFilter')]")
        await page.wait_for_timeout(700)
        for r in resorts:
            loc = page.locator(
                f"xpath=//div[contains(@class,'TVCheckboxTreeItem')]"
                f"//div[contains(@class,'TVCheckBox') and normalize-space(text())='{r}']"
            )
            if await loc.count():
                await loc.first.click()
                await page.wait_for_timeout(300)
        await page.mouse.click(10, 10)

    async def _set_budget(self, page: Page, price_min, price_max) -> None:
        # Бюджет — инпуты мин/макс цены + кнопка «Выбрать».
        await page.click("xpath=//div[contains(@class,'TVBudgetFilter')]")
        await page.wait_for_timeout(500)
        if price_min is not None:
            await page.fill("input.TVTourBudgetMinPriceInput", str(int(price_min)))
        if price_max is not None:
            await page.fill("input.TVTourBudgetMaxPriceInput", str(int(price_max)))
        await page.click(
            "xpath=//div[contains(@class,'TVBudgetSelectTooltipSubmit')]"
            "//div[contains(@class,'TVButtonControl')]"
        )
        await page.wait_for_timeout(300)

    async def _select_departure_city(self, page: Page, city: str) -> None:
        await page.click("div.TVDepartureFilter")
        await page.wait_for_selector("div.TVDepartureTableBody")
        await page.wait_for_timeout(400)
        # Кандидаты подписи: сокращённое имя Tourvisor (если знаем), полное имя и
        # последнее слово (С.Петербург ← «Петербург», Н.Новгород ← «Новгород»).
        candidates = _departure_candidates(city)
        # Клик через JS с нормализацией (пробелы/разные дефисы), а не xpath contains(text()).
        clicked = await page.evaluate(
            """(cands) => {
                const norm = s => (s || '').replace(/\\s+/g, ' ')
                    .replace(/[\\u2010-\\u2015\\u2212]/g, '-').trim().toLowerCase();
                const wants = cands.map(norm).filter(Boolean);
                const body = document.querySelector('.TVDepartureTableBody');
                if (!body) return false;
                const leaves = [...body.querySelectorAll('*')]
                    .filter(e => e.children.length === 0 && norm(e.textContent));
                for (const want of wants) {
                    let el = leaves.find(e => norm(e.textContent) === want)
                          || leaves.find(e => norm(e.textContent).includes(want));
                    if (el) { el.click(); return true; }
                }
                return false;
            }""",
            candidates,
        )
        if not clicked:
            # Города нет в таблице вылетов — детерминированный отказ, повтор не поможет.
            raise NotApplicableError(f"Город вылета «{city}» недоступен на Tourvisor")
        await page.wait_for_timeout(400)

    async def _select_country(self, page: Page, country: str) -> None:
        await page.click("div.TVCountryFilter")
        await page.wait_for_selector(
            "xpath=//div[contains(@class,'TVCountryAirportList') and not(contains(@class,'TVHide'))]"
        )
        await page.wait_for_timeout(400)
        # Список стран сгруппирован («Популярные» + алфавит) и подгружается лениво
        # (scrollHeight растёт при прокрутке). Страна-строка — `.TVComplexListItemContent`
        # с СОБСТВЕННЫМ текстом = названию страны (у `.TVComplexListItem` в textContent
        # ещё и города — поэтому раньше «Мальдивы» не находились). Скроллим контейнер,
        # пока не найдём строку с точным собственным текстом, и кликаем её.
        clicked = await page.evaluate(
            """async (country) => {
                const sleep = ms => new Promise(r => setTimeout(r, ms));
                const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const want = norm(country);
                const conts = [...document.querySelectorAll('div')]
                    .filter(e => e.scrollHeight - e.clientHeight > 40 && /Country|AirportList/i.test(e.className || ''));
                const cont = conts.sort((a, b) => b.scrollHeight - a.scrollHeight)[0]
                          || document.querySelector('.TVCountryAirportList');
                if (!cont) return false;
                const ownText = el => norm([...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(''));
                // Поиск по ВСЕМУ документу (страна может быть в другом контейнере/вкладке),
                // а скроллим найденный контейнер cont, чтобы дотянуть лениво подгружаемые.
                const find = () => [...document.querySelectorAll('.TVComplexListItemContent, .TVComplexListItem')]
                    .find(e => ownText(e) === want);
                let el = find();
                if (!el) {
                    cont.scrollTop = 0; await sleep(60);
                    const step = Math.max(80, cont.clientHeight - 40);
                    let lastTop = -1;
                    for (let i = 0; i < 150; i++) {
                        el = find(); if (el) break;
                        cont.scrollTop = cont.scrollTop + step; await sleep(60);
                        el = find(); if (el) break;
                        if (cont.scrollTop === lastTop) break;  // дальше не прокручивается = конец
                        lastTop = cont.scrollTop;
                    }
                }
                if (el) { el.scrollIntoView({block: 'center'}); el.click(); return true; }
                return false;
            }""",
            country,
        )
        if not clicked:
            # Страны нет в списке направлений (доскроллили до конца) — детерминированный отказ.
            raise NotApplicableError(f"Страна «{country}» недоступна на Tourvisor")
        await page.wait_for_timeout(500)

    async def _open_dates_filter(self, page: Page) -> None:
        """Открыть календарь дат. Туры — `.TVFlyDatesFilter`; отели (/poisk-otelej) —
        `.TVTripDurationFilter` («Даты проживания»), т.к. там `.TVFlyDatesFilter` скрыт
        (класс `TVHide`) и клик по нему молча падал → оставались дефолтные даты."""
        for sel in ("div.TVFlyDatesFilter:visible", "div.TVTripDurationFilter:visible"):
            loc = page.locator(sel)
            if await loc.count():
                await loc.first.click()
                return
        await page.click("div.TVTripDurationFilter")

    async def _select_dates(self, page: Page, date_from, date_to) -> None:
        await self._open_dates_filter(page)
        # Календарь одинаков в обоих режимах; ждём заголовок месяца (а не tours-only tooltip).
        await page.wait_for_selector(".TVCalendarTitleControlMonth", timeout=10_000)
        await self._scroll_to_month(page, date_from.month, date_from.year)
        await self._click_day(page, date_from.day)
        await page.wait_for_timeout(400)
        if date_to:
            if (date_to.month, date_to.year) != (date_from.month, date_from.year):
                await self._scroll_to_month(page, date_to.month, date_to.year)
            await self._click_day(page, date_to.day)
            await page.wait_for_timeout(300)
        # Закрыть календарь кликом в угол
        await page.mouse.click(10, 10)

    async def _scroll_to_month(self, page: Page, month: int, year: int) -> None:
        target = _MONTHS_RU[month]
        for _ in range(13):
            cur_month = (await page.inner_text("div.TVCalendarTitleControlMonth")).strip().upper()
            cur_year = (await page.inner_text("div.TVCalendarTitleControlYear")).strip()
            if cur_month == target and cur_year == str(year):
                return
            await page.click(
                "xpath=//div[contains(@class,'TVCalendarSliderViewRightButton') "
                "and not(contains(@class,'TVDisabled'))]"
            )
            await page.wait_for_timeout(350)
        raise RuntimeError(f"Месяц {target} {year} не найден в календаре")

    async def _click_day(self, page: Page, day: int) -> None:
        await page.click(
            f"xpath=//t-td[@data-value='{day}' and not(contains(@class,'TVCalendarDisabledCell'))]"
        )

    async def _select_nights(self, page: Page, nights_min: int, nights_max: int) -> None:
        await page.click("xpath=//div[contains(@class,'TVNightsFilter')]")
        await page.wait_for_selector("div.TVRangeTableContainer")
        for night in (nights_min, nights_max):
            await page.click(
                f"xpath=//div[contains(@class,'TVRangeTableCell') and "
                f".//div[contains(@class,'TVRangeCellLabel') and text()='{night}']]"
            )

    async def _select_tourists(self, page: Page, adults: int, children_ages: list[int]) -> None:
        await page.click("div.TVTouristsFilter")
        await page.wait_for_selector("xpath=//div[contains(@class,'TVTouristsSelectTooltip')]")

        # Взрослые: подвести счётчик TVTouristAll к нужному значению.
        # None-guard на re.search: при неполной загрузке виджета inner_text может
        # быть без цифр («Взр» / пустая строка) → match=None → AttributeError на .group().
        count_loc = page.locator("div.TVTouristCount.TVTouristAll")
        m = re.search(r"\d+", await count_loc.inner_text())
        current = int(m.group()) if m else 0
        plus = page.locator("div.TVTouristActionPlus")
        minus = page.locator("div.TVTouristActionMinus")
        while current != adults:
            if current < adults:
                await plus.click()
                current += 1
            else:
                await minus.click()
                current -= 1
            await page.wait_for_timeout(150)

        # Дети: на каждого ребёнка добавить слот и выбрать возраст
        for age in children_ages:
            await page.click("div.TVTouristDynamic div.TVTouristButton")
            await page.wait_for_selector("div.TVSelectChildAge")
            label = "до 2" if age < 2 else str(min(age, 15))
            await page.click(
                f"xpath=//div[contains(@class,'TVSelectChildAgeItem')]"
                f"[.//div[contains(@class,'TVSelectChildAgeValue') and normalize-space(text())='{label}']]"
            )
            await page.wait_for_timeout(300)

        await page.click(
            "xpath=//div[contains(@class,'TVButtonControl') and contains(text(),'Выбрать')]"
        )

    async def _select_operators(self, page: Page, operators: list[str]) -> None:
        await page.click("div.TVOperatorListFilter")
        await page.wait_for_selector("div.TVOperatorsList")
        # ВАЖНО: часть операторов отмечена по умолчанию (промо, напр. Biblioglobus).
        # Сначала снимаем все отмеченные, иначе выбор «добавится» к дефолтным.
        checked = page.locator("div.TVOperatorsList .TVCheckBox.TVChecked")
        for _ in range(await checked.count()):
            # коллекция «съезжает» после клика — каждый раз берём первый отмеченный
            cur = page.locator("div.TVOperatorsList .TVCheckBox.TVChecked").first
            if await cur.count() == 0:
                break
            await cur.click()
            await page.wait_for_timeout(200)
        # Отмечаем нужные. Имена операторов на Sletat и Tourvisor различаются
        # («FUN and SUN» ↔ «FUN&SUN (TUI)», «Biblio Globus» ↔ «Biblioglobus»), поэтому
        # сопоставляем НЕЧЁТКО: нормализуем (нижний регистр, убрать скобки/«and»/«и» и
        # всё, кроме букв/цифр) и матчим по точному ключу, затем по вхождению.
        # Применяем алиасы (Спектрум→Spectrum и т.п.) до нечёткого матчинга.
        wanted = [_TV_OPERATOR_ALIASES.get(_operator_norm(o), o) for o in operators]
        indices = await page.evaluate(
            """(wanted) => {
                // region — извлекаем суффикс (BY)/(KZ)/(UZ), чтобы не путать
                // региональные клоны оператора (Coral vs Coral Travel (BY)).
                const region = s => { const m = (s||'').match(/\\((BY|KZ|UZ)\\)/i); return m ? m[1].toLowerCase() : ''; };
                const core = s => (s || '').toLowerCase()
                    .replace(/\\(.*?\\)/g, '')
                    .replace(/\\band\\b|\\bи\\b/g, '')
                    .replace(/[^a-zа-я0-9]/gi, '');
                const boxes = [...document.querySelectorAll('div.TVOperatorsList .TVCheckBox')];
                const live = i => i >= 0 && !/TVDisabled/.test(boxes[i].className || '');
                return wanted.map(w => {
                    const wc = core(w), wr = region(w);
                    if (!wc) return -1;
                    // только кандидаты того же региона (без региона ↔ без региона)
                    const same = i => !/TVDisabled/.test(boxes[i].className||'') && region(boxes[i].textContent) === wr;
                    let idx = boxes.findIndex((b,i) => same(i) && core(b.textContent) === wc);
                    if (idx < 0) idx = boxes.findIndex((b,i) => {
                        if (!same(i)) return false;
                        const bc = core(b.textContent);
                        return bc && (bc.includes(wc) || wc.includes(bc));
                    });
                    return live(idx) ? idx : -1;
                });
            }""",
            wanted,
        )
        boxes = page.locator("div.TVOperatorsList .TVCheckBox")
        for op, idx in zip(operators, indices, strict=True):
            if idx is None or idx < 0:
                log.info("Tourvisor: оператор «%s» не найден в списке — пропуск", op)
                continue
            box = boxes.nth(idx)
            cls = await box.get_attribute("class") or ""
            if "TVChecked" not in cls:
                await box.click()
                await page.wait_for_timeout(300)

    async def _toggle_charter(self, page: Page, charter_only: bool) -> None:
        if not charter_only:
            return
        checkbox = page.locator(
            "xpath=//div[contains(@class,'TVCheckboxControl') and "
            ".//div[contains(text(),'Только чартер')]]"
        )
        if await checkbox.count() == 0:
            return
        cls = await checkbox.first.get_attribute("class") or ""
        if "TVChecked" not in cls:
            await checkbox.first.click()

    async def _click_search(self, page: Page) -> None:
        # На главной кнопка — иконка без текста, на search.php/poisk-otelej — «Найти туры»/«Найти».
        # Кликаем видимую кнопку поиска независимо от текста.
        await page.click("div.TVSearchButton:visible")

    # --- верификация формы перед поиском ---
    # Операторы и курорт-дерево НЕ верифицируем: чтение требует переоткрытия панели,
    # а оно повторно проставляет дефолты (напр. Biblioglobus) и может испортить выбор.

    async def _verify_and_fix(self, page: Page, params: SearchParams) -> None:
        problems = await self._verify_form(page, params)
        if not problems:
            return
        for field, _, _ in problems:
            try:
                await self._refill_field(page, params, field)
            except Exception:
                pass
        problems = await self._verify_form(page, params)
        if problems:
            raise FormVerificationError(problems)

    async def _safe_text(self, page: Page, selector: str):
        try:
            loc = page.locator(selector)
            if await loc.count() == 0:
                return UNKNOWN
            return await loc.first.text_content()
        except Exception:
            return UNKNOWN

    async def _verify_form(self, page: Page, params: SearchParams) -> list[tuple[str, object, object]]:
        problems: list[tuple[str, object, object]] = []
        hotels = params.search_mode == "hotels"

        def check(field, expected, actual, ok: bool) -> None:
            if actual is UNKNOWN:
                return
            if not ok:
                problems.append((field, expected, actual))

        # На /poisk-otelej (отели) форма иная: город/страна/ночи задаются иначе — пропускаем.
        if not hotels:
            dep = await self._safe_text(page, "div.TVDepartureFilter")
            # Tourvisor подписывает город сокращённо («Город вылетаС.Петербург» ←
            # «Санкт-Петербург»), поэтому сверяем по ЛЮБОМУ из кандидатов, а не по
            # полному имени — иначе валидный поиск ложно падал FormVerificationError.
            dep_ok = dep is UNKNOWN or any(text_contains(c, dep) for c in _departure_candidates(params.departure_city))
            check("departure", params.departure_city, dep, dep_ok)
            country = await self._safe_text(page, "div.TVCountryFilter")
            check("country", params.destination_country, country, text_contains(params.destination_country, country) if country is not UNKNOWN else True)
            nights = await self._safe_text(page, "div.TVNightsFilter")
            check("nights", f"{params.nights_min} - {params.nights_max}", nights,
                  text_contains(f"{params.nights_min} - {params.nights_max}", nights) if nights is not UNKNOWN else True)

        # Подпись на форме сокращённая («Туристы3 взр 2 реб») — сверяем по ведущему числу (взрослые).
        tourists = await self._safe_text(page, "div.TVTouristsFilter")
        if tourists is not UNKNOWN:
            m = re.search(r"\d+", tourists or "")
            actual_adults = int(m.group()) if m else None
            check("tourists", params.adults, actual_adults, actual_adults == params.adults)

        if params.hotel_stars:
            # Виджет «Класс отеля» подсвечивает звёзды КУМУЛЯТИВНО и по-разному в AB-вариантах
            # (кол-во активных нестабильно: видели и 4, и 6 для «4★»), поэтому равенство
            # «активных == min★» — ложный критерий и блокировало валидный поиск. Надёжно
            # проверяем, что ИМЕННО нужная кнопка (target-я) активна; честность звёзд в ВЫДАЧЕ
            # проверяется отдельно на уровне результатов.
            target = min(params.hotel_stars)
            try:
                btn_active = await page.evaluate(
                    "(t) => { const it = [...document.querySelectorAll("
                    "'div.TVStarsFilter .TVStarsSelectItem')]; "
                    "return it[t - 1] ? it[t - 1].className.includes('TVActive') : null; }", target)
            except Exception:
                btn_active = None
            if btn_active is not None:
                check("stars", f"кнопка {target}★ активна", btn_active, bool(btn_active))

        if params.meals:
            meal = await self._safe_text(page, "div.TVMealFilter")
            check("meals", params.meals[0], meal, text_contains(params.meals[0], meal) if meal is not UNKNOWN else True)

        if params.price_max is not None:
            budget = await self._safe_text(page, "div.TVBudgetFilter")
            if budget is not UNKNOWN:
                digits = re.sub(r"[^\d]", "", budget)
                check("budget", str(int(params.price_max)), budget, str(int(params.price_max)) in digits)

        return problems

    async def _refill_field(self, page: Page, params: SearchParams, field: str) -> None:
        if field == "departure":
            await self._select_departure_city(page, params.departure_city)
        elif field == "country":
            await self._select_country(page, params.destination_country)
        elif field == "nights":
            await self._select_nights(page, params.nights_min, params.nights_max)
        elif field == "tourists":
            await self._select_tourists(page, params.adults, params.children_ages)
        elif field == "stars":
            await self._select_stars(page, params.hotel_stars)
        elif field == "meals":
            await self._select_meal(page, params.meals[0])
        elif field == "budget":
            await self._set_budget(page, params.price_min, params.price_max)

    # --- ожидание и парсинг ---

    async def _wait_for_completion(self, page: Page, timeout_s: int = 120) -> None:
        """Ждать завершения поиска.

        Сигнал (см. RESULTS.md): `.TVProgressBar` виден на старте и СКРЫВАЕТСЯ по
        завершении, после чего число `.TVResultItem` стабилизируется. Спиннеры в счёт
        не идут (ленивые картинки).

        ВАЖНО: поиск может завершиться и с НУЛЁМ результатов — тогда `.TVResultItem`
        не появится никогда. Поэтому НЕЛЬЗЯ просто висеть в `wait_for_selector(.TVResultItem)`
        весь таймаут (раньше пустая выдача держала ~120 c): ориентир — прогресс-бар.
        """
        # 1) старт: прогресс-бар стал видим (поиск реально пошёл)
        try:
            await page.wait_for_selector(".TVProgressBar", state="visible", timeout=20_000)
            started = True
        except PWTimeout:
            # бар не показался — возможно, результаты появились мгновенно или поиск
            # не стартовал; не висим, сразу к стабилизации счётчика
            started = False
        # 2) завершение: прогресс-бар скрылся — надёжно и для пустой выдачи
        if started:
            try:
                await page.wait_for_selector(".TVProgressBar", state="hidden", timeout=timeout_s * 1000)
            except PWTimeout:
                pass
        # 3) стабилизация числа карточек (часть догружается чуть позже бара); count==0 = честно пусто
        deadline = time.monotonic() + timeout_s
        last_count, stable = -1, 0
        while time.monotonic() < deadline:
            await page.wait_for_timeout(1000)
            try:
                count = await page.locator(".TVResultItem").count()
            except Exception:
                count = 0
            stable = stable + 1 if count == last_count else 0
            last_count = count
            if stable >= 2:
                return

    async def _parse_hotels(self, page: Page) -> list[HotelOffer]:
        """Собрать предложения по отелям из карточек `.TVResultItem`."""
        rows = await page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('.TVResultItem').forEach(it => {
                    const title = (it.querySelector('.TVResultItemTitle')?.textContent || '').trim();
                    const subtitle = (it.querySelector('.TVResultItemSubTitle')?.textContent || '').trim();
                    const rating = (it.querySelector('.TVResultItemBeforeDescription, [class*=Rating]')?.textContent || '').trim();
                    const price = (it.querySelector('.TVResultItemPriceValue')?.textContent || '').trim();
                    if (title && price) out.push({title, subtitle, rating, price});
                });
                return out;
            }"""
        )
        return build_hotel_offers(self.name, rows)

    async def _load_all_hotels(self, page: Page, cap: int) -> bool:
        """Догрузить выдачу до конца. True = упёрлись в предохранитель.

        Выдача разворачивается кнопкой «Найти больше туров»
        (`.TVResultToursShowMoreButton`), а НЕ бесконечной прокруткой: колесо мыши не
        добавляет ни одной карточки, счётчик стоит на первых ~19. Пока это не было учтено,
        эталон отдавал два десятка отелей вместо сотен — и сравнение теряло смысл: по
        такой выборке «отельных пропусков нет» означает лишь «мы посмотрели девятнадцать
        отелей». Скрытая кнопка помечается классом `TVHide`; её отсутствие среди видимых =
        выдача кончилась.
        """
        for _ in range(_LOAD_ROUNDS):
            try:
                count = await page.locator(".TVResultItem").count()
            except Exception:
                return False
            if count >= cap:
                log.warning("Tourvisor: выдача обрезана лимитом в %d отелей", cap)
                return True
            # Именно `:visible`, а не `:not(.TVHide)`: разметка содержит под два десятка
            # экземпляров кнопки, и первый по DOM — невидимый, клик по нему висит до
            # таймаута и молча обрывает догрузку на первой же порции.
            button = page.locator(".TVResultToursShowMoreButton:visible")
            try:
                if await button.count() == 0:
                    return False  # больше нечего показывать
                await button.first.scroll_into_view_if_needed(timeout=5000)
                await button.first.click(timeout=5000)
            except Exception:
                return False
            try:
                # Ждём именно ПРИРОСТА карточек: фиксированная пауза либо режет выдачу на
                # медленном ответе, либо тратит время впустую на быстром.
                await page.wait_for_function(
                    "n => document.querySelectorAll('.TVResultItem').length > n",
                    arg=count, timeout=20_000,
                )
            except PWTimeout:
                return False  # кнопка есть, а прироста нет — считаем, что всё загружено
        log.warning("Tourvisor: выдача не сошлась за %d нажатий «Найти больше туров»",
                    _LOAD_ROUNDS)
        return True

    async def _read_checked_operators(self, page: Page) -> list[str] | None:
        """Имена операторов, реально отмеченных в фильтре ПОСЛЕ поиска.

        Прямая проверка того, что наши клики по чекбоксам удержались. Не полагается на
        состав ценовой колонки: панель может перечислять операторов независимо от
        применённого фильтра, а состояние чекбокса — это и есть применённый фильтр.
        None = панель недоступна (проверить не удалось).
        """
        try:
            return await page.evaluate(
                """() => {
                    const list = document.querySelector('div.TVOperatorsList');
                    if (!list) return null;
                    return [...list.querySelectorAll('.TVCheckBox.TVChecked')]
                        .map(b => (b.textContent || '').trim()).filter(Boolean);
                }"""
            )
        except Exception:
            return None

    async def _verify_operator_filter(self, page: Page, operators: list[str]) -> bool | None:
        """Применился ли фильтр по оператору. None = проверить не удалось.

        Без этой проверки инструмент нельзя использовать по назначению: карточки отелей
        на Турвизоре не несут имени оператора, поэтому при неприменившемся фильтре их цены
        — минимум по ВСЕМ операторам (а в фильтре по умолчанию предотмечен промо-оператор).
        Отчёт о пропусках, построенный на таких данных, ложен целиком.
        """
        if not operators:
            return None
        checked = await self._read_checked_operators(page)
        if checked is None:
            log.warning("Tourvisor: панель операторов недоступна — фильтр не проверен")
            return None
        extra = [c for c in checked
                 if not any(operator_matches(c, w) for w in operators)]
        missing = [w for w in operators
                   if not any(operator_matches(c, w) for c in checked)]
        if extra or missing:
            log.warning("Tourvisor: фильтр операторов не применился — лишние %s, недостающие %s",
                        extra or "нет", missing or "нет")
            return False
        return True

    async def _parse_operators(self, page: Page) -> list[Offer]:
        btn = page.locator("div.TVResultToolbarOperators")
        if await btn.count() == 0:
            return []
        await btn.first.click()

        # Цены подгружаются асинхронно, а строки пересортировываются по цене.
        # Поэтому: дождаться появления цен и исчезновения спиннеров, затем снять
        # данные ОДНИМ атомарным снимком DOM (иначе имя и цена разъезжаются).
        try:
            await page.wait_for_selector(
                ".TVOperatorFilterColumnBody .TVOperatorFilterItemPriceValue", timeout=30_000
            )
        except PWTimeout:
            return []
        try:
            await page.wait_for_function(
                "() => { const b = document.querySelector('.TVOperatorFilterColumnBody');"
                " return b && b.querySelectorAll('.TVSpinner').length === 0; }",
                timeout=20_000,
            )
        except PWTimeout:
            pass
        await page.wait_for_timeout(400)

        rows = await page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll(
                    '.TVOperatorFilterColumnBody .TVOperatorFilterItemControl'
                ).forEach(it => {
                    const name = (it.querySelector('.TVCheckBox')?.textContent || '').trim();
                    const price = (it.querySelector('.TVOperatorFilterItemPriceValue')?.textContent || '').trim();
                    if (name && price) out.push({name, price});
                });
                return out;
            }"""
        )

        return build_offers(self.name, rows)

    def _to_operator_offers(
        self, offers: list[Offer], hotel_offers: list[HotelOffer],
    ) -> list[OperatorOffer]:
        """Offer (оператор+мин.цена) → OperatorOffer; отель по совпадению цены, скорость —
        недоступна на Tourvisor (панель операторов грузится после результатов) → None."""
        hotel_by_price: dict[Decimal, str] = {}
        for h in hotel_offers:
            hotel_by_price.setdefault(h.price, h.hotel_name)
        out = [
            OperatorOffer(
                provider=self.name, operator=o.operator, price=o.price, currency=o.currency,
                hotel_name=hotel_by_price.get(o.price), load_seconds=None, raw_label=o.raw_label,
            )
            for o in offers
        ]
        out.sort(key=lambda x: x.price)
        return out

    async def _safe_screenshot(self, page: Page) -> str | None:
        # Верхняя часть страницы во всю ширину (форма + «нашлось N» + первые
        # результаты), но не вся бесконечная лента — информативно и читабельно.
        path = f"screenshots/tourvisor_{datetime.now():%Y%m%d_%H%M%S_%f}.png"
        return await _capture_top(page, path)
