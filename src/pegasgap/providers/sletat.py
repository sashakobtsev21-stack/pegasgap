"""Провайдер площадки Sletat (sletat.ru/b2b) на Playwright — главная площадка.

Селекторы выверены живой инспекцией (см. SITES.md, RESULTS.md). Особенности,
учтённые здесь:
- город/страна ВВОДЯТСЯ текстом (не все значения есть в дефолтном списке);
- по умолчанию сортировка по «Популярности» → переключаем на «Цена»;
- режим «Отели» (без перелёта) = переключатель switch-search-type.hotels-btn;
- сигнал завершения = стабилизация `.search-status__tours-count` и числа карточек
  (спиннеры не считаем — это ленивые картинки);
- туры → операторы с мин. ценой (панель `.blinchik`); отели → карточки `.search-result__list-item`.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date, datetime
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
from pegasgap.providers._formcheck import (
    UNKNOWN,
    FormVerificationError,
    exact,
    norm,
    set_equal,
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
from pegasgap.urlcheck import verify_sletat_search_url

log = logging.getLogger("pegasgap.providers.sletat")

_MONTHS_RU = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]

# Унифицированные ключи операторов → отображаемые имена на Sletat.
_OPERATOR_MAP = {
    "anex": "Anex",
    "biblioglobus": "Biblio Globus",
    "funsun": "FUN and SUN",
    "coral": "Coral",
    "sunmar": "Sunmar",
    "pegas": "Pegas Touristik",
    "tez": "TEZ TOUR",
    "intourist": "Intourist",
    "travelata": "Travelata",
}

# Коды питания → подписи быстрых кнопок Sletat.
_MEAL_BTN = {"BB": "BB", "HB": "HB", "FB": "FB", "AI": "AI", "UAI": "UAI"}

# Предохранитель от бесконечной ленты выдачи; см. `_load_all_hotels`. Держим общим с
# Турвизором через одну env-переменную: сравнивать имеет смысл только выдачи, снятые с
# одинаковой глубиной, иначе разница в покрытии сама выглядит как пропуски.
MAX_HOTELS = int(os.environ.get("PEGASGAP_MAX_HOTELS") or 300)
_SCROLL_ROUNDS = 40


def _parse_price(text: str) -> Decimal | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return Decimal(digits) if digits else None


def _parse_hotel_price(text: str) -> Decimal | None:
    """Цена карточки отеля Sletat. Текст вида «от 9 097 1 тур» / «от 13 962 6 туров»:
    цена и счётчик туров стоят в DOM рядом и склеиваются («9 0971»), из-за чего обычное
    «убрать всё кроме цифр» давало 90971 вместо 9097. Берём ВЕДУЩЕЕ число с пробелами-
    разделителями тысяч (группы ровно по 3 цифры), хвост «N тур(ов)» отбрасываем."""
    m = re.search(r"\d{1,3}(?:\s\d{3})+|\d+", text or "")
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    return Decimal(digits) if digits else None


# Публичные алиасы для testkit/catalog.py (P2-b audit-3): закрывают layering-issue,
# когда тест-слой импортировал приватные имена. Подчёркнутые версии остаются для
# обратной совместимости — не удаляем, чтобы не сломать внутренний код провайдера.
OPERATOR_MAP = _OPERATOR_MAP
MEAL_BTN = _MEAL_BTN
parse_price = _parse_price
parse_hotel_price = _parse_hotel_price


def build_operator_offers(provider_name: str, rows: list[dict]) -> list[Offer]:
    """Из строк панели операторов [{name, price}] → Offer (дедуп, мин. цена)."""
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
        Offer(provider=provider_name, operator=n, price=p, currency="RUB", raw_label=r)
        for n, (p, r) in best.items()
    ]


def build_hotel_offers(provider_name: str, rows: list[dict]) -> list[HotelOffer]:
    """Из карточек [{name, stars, rating, destination, price, operators}] → HotelOffer."""
    out: list[HotelOffer] = []
    for row in rows:
        name = (row.get("name") or "").strip()
        price = _parse_hotel_price(row.get("price") or "")
        if not name or price is None:
            continue
        rating = None
        # рейтинг 0.0–10.0 с одним знаком после запятой; текст может слипаться с числом
        # оценок («8.4230 оценок») → берём только ведущее «X.X».
        m = re.search(r"(?:10|\d)[.,]\d", row.get("rating") or "")
        if m:
            try:
                rating = float(m.group(0).replace(",", "."))
            except ValueError:
                rating = None
        stars = row.get("stars") or None
        ops = None
        mo = re.search(r"(\d+)\s*оператор", row.get("operators") or "")
        if mo:
            ops = int(mo.group(1))
        out.append(
            HotelOffer(
                provider=provider_name,
                hotel_name=name,
                stars=int(stars) if stars else None,
                rating=rating,
                destination=(row.get("destination") or "").strip() or None,
                price=price,
                currency="RUB",
                operators_count=ops,
                raw_label=(row.get("price") or "").strip(),
            )
        )
    return dedup_hotel_offers(out)         # P2-c: lazy-load на sletat иногда дублирует карточки


@register_provider("sletat")
class SletatProvider:
    """Поиск туров/отелей на sletat.ru/b2b."""

    name = "sletat"
    URL = "https://sletat.ru/b2b/"

    def __init__(self, headless: bool = False, timeout_ms: int = 20_000) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        # Колбэк живых кадров (устанавливается оркестратором); None = не транслировать.
        self.on_frame = None

    async def search(self, params: SearchParams) -> ProviderResult:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled", "--window-size=1600,1080"],
            )
            # Вьюпорт 1600: сайт целиком по ширине, но крупнее (читабельнее) в live-окне и скриншоте.
            # Явный desktop-UA: при headless=True Playwright по умолчанию шлёт «HeadlessChrome»,
            # который антибот-системы могут отбивать. См. base.DESKTOP_CHROME_UA.
            context = await browser.new_context(
                viewport={"width": 1600, "height": 1080}, permissions=[],
                user_agent=DESKTOP_CHROME_UA,
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()
            page.set_default_timeout(self.timeout_ms)
            # Навигации даём больше времени, чем ожиданиям элементов: первая загрузка
            # b2b-страницы тяжёлая (много JS), при разовых тормозах сети/сайта 20 с —
            # впритык и весь поиск падает на goto. Элементы при этом ждём быстро (timeout_ms).
            page.set_default_navigation_timeout(max(self.timeout_ms, 45_000))
            pump = start_frame_pump(self.name, page, self.on_frame)
            start = time.monotonic()
            try:
                await page.goto(self.URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(3500)
                await self._close_popups(page)
                log.info("Sletat: сайт открыт, задаю параметры (%s)…", params.search_mode)
                # Переключение в «Отели» теперь делает _fill_form ПОСЛЕ выбора страны
                # (страна выбирается в «Турах» и переносится — см. _select_country_for_hotels).
                await self._fill_form(page, params)
                filter_ok = await self._verify_and_fix(page, params)
                await self._click_search(page)
                start = time.monotonic()
                # Открываем блинчик СРАЗУ после клика «Найти»: только при открытой панели
                # цены операторов прогружаются прогрессивно — иначе скорость по каждому
                # оператору не засечь (цены появлялись все разом в конце).
                await self._ensure_panel_open(page)
                # Засекаем, за сколько каждый оператор появляется с ценой в блинчике.
                op_seen: dict[str, float] = {}
                log.info("Sletat: поиск запущен, жду полной загрузки результатов…")
                await self._wait_for_completion(page, op_seen=op_seen, op_start=start)
                # Скорость поиска = от клика «Найти» до появления результатов. Сортировку
                # «по цене» и парсинг ниже в неё НЕ включаем (это уже пост-обработка).
                dur = time.monotonic() - start
                # URL результата кодирует реальные параметры поиска — финальная сверка.
                search_url = page.url
                url_problems = verify_sletat_search_url(search_url, params)
                if params.sort_by == "price":
                    await self._sort_by_price(page)
                # Блинчик с группировкой: цены / «Туров нет» / «Оператор не отвечает».
                blink = await self._parse_blinchik(page)
                no_tours = blink["no_tours"]
                not_responding = blink["not_responding"]
                # Выдачу доматываем всегда: инструмент ищет ОТСУТСТВУЮЩИЕ отели, а отель,
                # который просто не догрузился, неотличим от отсутствующего. Раньше в
                # режиме туров бралась только первая десятка (этого хватало для показа) —
                # для поиска пропусков так весь хвост каталога числился бы пропавшим.
                truncated = await self._load_all_hotels(page, MAX_HOTELS)
                hotel_offers = await self._parse_hotels(page)
                offers = [] if params.search_mode == "hotels" else build_operator_offers(
                    self.name, blink["priced"])
                # Таблица оператор → отель → цена → скорость (блинчик + тайминги).
                operator_offers = self._operator_offers(blink["priced"], op_seen, hotel_offers)
                if url_problems:
                    # площадка искала не то, что просили — результаты невалидны
                    return ProviderResult(
                        provider=self.name, success=False,
                        duration_seconds=dur,
                        search_mode=params.search_mode, search_url=search_url,
                        error="URL-параметры не совпали: " + "; ".join(
                            f"{f}: ожидали {e!r}, получили {a!r}" for f, e, a in url_problems),
                    )
                found = bool(offers or hotel_offers)
                # Раскрываем выезжающую слева панель операторов (blinchik) — и в
                # «Турах», и в «Отелях» — чтобы на скриншоте выдачи был виден
                # перечень туроператоров с ценами.
                await self._open_operator_panel(page)
                shot = await self._safe_screenshot(page)
                log.info("Sletat: результаты получены — %d предложений за %.1f с",
                         len(offers) + len(hotel_offers), dur)
                return ProviderResult(
                    provider=self.name,
                    success=found,
                    duration_seconds=dur,
                    search_mode=params.search_mode,
                    offers=offers,
                    hotel_offers=hotel_offers,
                    operator_offers=operator_offers,
                    operators_no_tours=no_tours,
                    operators_not_responding=not_responding,
                    operator_filter_verified=filter_ok,
                    truncated=truncated,
                    search_url=search_url,
                    screenshot_path=shot,
                    error=None if found else "Предложений не найдено по заданным параметрам.",
                )
            except Exception as exc:
                log.warning("sletat search failed (mode=%s): %s: %s",
                            params.search_mode, type(exc).__name__, exc)
                shot = await self._safe_screenshot(page)
                msg = str(exc)
                # «не предлагается / не обслуживает» — детерминированный отказ (не сбой):
                # показываем чистым текстом, без префикса типа исключения. Первичен тип
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
                )
            finally:
                await stop_frame_pump(pump)
                await browser.close()

    # --- подготовка ---

    async def _close_popups(self, page: Page) -> None:
        for sel in (".icon-remove", "button[data-testid='layout.cookie-alert.accept-btn']"):
            try:
                await page.click(sel, timeout=3000)
                await page.wait_for_timeout(300)
            except PWTimeout:
                pass

    async def _switch_to_hotels(self, page: Page) -> None:
        try:
            await page.click("[data-testid='b2b.search-form.switch-search-type.hotels-btn']", timeout=5000)
            await page.wait_for_timeout(1200)
        except PWTimeout:
            await page.click("xpath=//*[contains(@class,'tab') and normalize-space(text())='Отели']")
            await page.wait_for_timeout(1200)

    # --- заполнение формы ---

    async def _fill_form(self, page: Page, params: SearchParams) -> None:
        if params.search_mode == "hotels":
            # Режим «Отели» (без перелёта): город вылета не нужен, НО список стран в этом
            # режиме УРЕЗАН — часть направлений (напр. Армения) в нём отсутствует, хотя
            # отели по ним есть. Выбираем страну в «Турах» (полный список) и переключаемся.
            await self._select_country_for_hotels(page, params)
        else:
            await self._select_departure_city(page, params.departure_city)
            await self._select_country(page, params.destination_country)
        # Курорт — ПОСЛЕ страны: смена страны перезагружает список курортов.
        if params.resorts:
            await self._select_resorts(page, params.resorts)
        await self._select_dates(page, params.date_from, params.date_to)
        await self._set_nights(page, params.nights_min, params.nights_max)
        await self._select_tourists(page, params.adults, params.children_ages)
        if params.operators:
            await self._select_operators(page, params.operators)
        if params.hotel_stars:
            await self._select_stars(page, params.hotel_stars)
        if params.meals:
            await self._select_meals(page, params.meals)
        if params.hotel_rating_min is not None:
            await self._select_rating(page, params.hotel_rating_min)
        if params.price_min is not None or params.price_max is not None:
            await self._select_price(page, params.price_min, params.price_max)
        if params.currency and params.currency != "RUB":
            await self._select_currency(page, params.currency)
        await self._toggle_flight_flags(page, params)

    async def _select_departure_city(self, page: Page, city: str) -> None:
        await page.click("input.excludeClickOutside")
        await page.fill("input.excludeClickOutside", "")
        await page.type("input.excludeClickOutside", city, delay=40)
        await page.wait_for_timeout(900)
        await page.click(
            f"xpath=//div[contains(@class,'city-selector-list')]//li//button[contains(.,'{city}')][1]"
        )
        await page.wait_for_timeout(500)

    async def _select_country(self, page: Page, country: str) -> None:
        # Ввод текста + дожидаемся появления опции (а не «слепой» клик через 800мс —
        # под нагрузкой дропдаун не успевал отфильтроваться → 20с таймаут). Две попытки;
        # если опции нет и со второй — страна не предлагается на Sletat (нет в списке).
        opt = (f"xpath=//span[contains(@class,'slsf-country-to__select-text')"
               f" and contains(text(),'{country}')][1]")
        for _attempt in range(2):
            await page.click("#ui-select-country-to")
            await page.wait_for_timeout(400)
            await page.keyboard.type(country, delay=40)
            await page.wait_for_timeout(600)
            try:
                await page.wait_for_selector(opt, state="visible", timeout=6000)
                await page.click(opt)
                await page.wait_for_timeout(800)
                return
            except PWTimeout:
                try:  # очистить поле и повторить ввод
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Delete")
                except Exception:
                    pass
                await page.wait_for_timeout(200)
        # Страна отсутствует в активном списке Sletat (после двух попыток ввода). Это не сбой
        # формы, а «не предлагается» — показывается нейтрально (ℹ️), без красной ошибки. В
        # режиме «Отели» вызывающий (_select_country_for_hotels) сперва пробует выбрать страну
        # в «Турах» и перенести — сюда долетают лишь направления, которых нет нигде.
        raise NotApplicableError(f"направление «{country}» не предлагается на Sletat")

    async def _select_country_for_hotels(self, page: Page, params: SearchParams) -> None:
        """Выбрать страну назначения для режима «Отели» (без перелёта).

        Список стран в режиме «Отели» урезан: часть направлений (напр. Армения) в нём
        отсутствует, хотя инвентарь отелей по ним есть. Рабочий путь (как в UI у агента):
        выбрать страну в режиме «Туры» (там полный список; нужен город вылета) и затем
        переключиться в «Отели» — страна ПЕРЕНОСИТСЯ, перелёт отбрасывается (ticketsincluded=
        false), отели по стране подгружаются. Если страны нет и в «Турах» — запасной путь:
        выбрать прямо в списке отелей (вдруг направление доступно только как «отели»)."""
        in_tours = False
        try:
            await self._select_departure_city(page, params.departure_city)
            await self._select_country(page, params.destination_country)
            in_tours = True
        except Exception as exc:
            log.info("Sletat: «%s» нет в списке «Туров» (%s) — пробую прямой выбор в «Отелях»",
                     params.destination_country, exc)
        await self._switch_to_hotels(page)
        await page.wait_for_timeout(2000)
        if not in_tours:
            # запасной путь: страна не нашлась в турах — пробуем напрямую в списке отелей
            await self._select_country(page, params.destination_country)

    async def _select_dates(self, page: Page, date_from: date, date_to: date) -> None:
        # Sletat ограничивает окно вылета ±13 дней от первой даты — дальше дни disabled.
        # Лимит самой площадки, повтор не поможет — детерминированный отказ.
        if (date_to - date_from).days > 13:
            raise NotApplicableError(
                f"Диапазон дат вылета {(date_to - date_from).days} дн. превышает лимит Sletat (13)"
            )
        await page.click("div.containerTitle")
        await page.wait_for_selector("button.rdrDay")
        await self._goto_month(page, date_from)
        await self._click_day(page, date_from.day)
        await page.wait_for_timeout(400)
        await self._goto_month(page, date_to)
        await self._click_day(page, date_to.day)
        await page.wait_for_timeout(300)
        try:
            await page.click("button.date-range-date-label", timeout=4000)
        except PWTimeout:
            await page.mouse.click(10, 10)

    async def _goto_month(self, page: Page, target: date) -> None:
        for _ in range(18):
            name = (await page.locator(".rdrMonthName").first.inner_text()).strip().lower()
            parts = name.split()
            cur_month = next((i + 1 for i, m in enumerate(_MONTHS_RU) if m in parts[0]), None)
            cur_year = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else target.year
            if cur_month is None:
                return
            diff = (target.year - cur_year) * 12 + (target.month - cur_month)
            if diff == 0:
                return
            btn = (
                "button.navigatorSlideButton.nextButton"
                if diff > 0
                else "button.navigatorSlideButton:not(.nextButton)"
            )
            await page.click(btn)
            await page.wait_for_timeout(300)

    async def _click_day(self, page: Page, day: int) -> None:
        await page.click(
            f"xpath=(//button[contains(@class,'rdrDay') and not(contains(@class,'rdrDayDisabled'))]"
            f"[.//span[contains(@class,'customDay')]/span[1][normalize-space(text())='{day}']])[1]"
        )

    async def _set_nights(self, page: Page, nmin: int, nmax: int) -> None:
        # Ночи — это дропдауны (readonly-инпут открывает список uis-select__options-item).
        # Прямая установка value НЕ влияет на поиск (в URL уходит дефолт) — выбираем опцию кликом.
        for input_id, container, value in (
            ("ui-select-nightsMin", "nights-left", nmin),
            ("ui-select-nightsMax", "nights-right", nmax),
        ):
            try:
                await page.click(f"#{input_id}", timeout=6000)
            except PWTimeout:
                # контрол ночей недоступен в этом режиме (напр. иная форма отелей) — пропускаем
                return
            await page.wait_for_timeout(500)
            # Список ночей — прокручиваемый/виртуализованный: нужный пункт (напр. 14)
            # появляется в DOM только при прокрутке. Скроллим контейнер по шагам и ищем.
            clicked = await page.evaluate(
                """async ([cls, value]) => {
                    const sleep = ms => new Promise(r => setTimeout(r, ms));
                    const cont = document.querySelector('.uis-select__options_nights.' + cls);
                    if (!cont) return false;
                    const find = () => [...cont.querySelectorAll('li')]
                        .find(e => e.textContent.trim() === String(value));
                    let el = find();
                    if (!el) {
                        cont.scrollTop = 0;
                        await sleep(40);
                        const step = Math.max(40, cont.clientHeight - 24);
                        for (let i = 0; i < 60; i++) {
                            el = find();
                            if (el) break;
                            const atEnd = cont.scrollTop + cont.clientHeight >= cont.scrollHeight - 2;
                            cont.scrollTop += step;
                            await sleep(45);
                            if (atEnd) { el = find(); break; }
                        }
                    }
                    if (el) { el.scrollIntoView({block: 'center'}); el.click(); return true; }
                    return false;
                }""",
                [container, value],
            )
            if not clicked:
                raise RuntimeError(f"значение ночей {value} не найдено в списке {container}")
            await page.wait_for_timeout(300)

    async def _select_tourists(self, page: Page, adults: int, children_ages: list[int]) -> None:
        await page.click("#touristSelector .tourist-current-select")
        await page.wait_for_timeout(500)
        label = page.locator(".adult-counter-label")
        # None-guard на re.search (см. tourvisor _select_tourists).
        m = re.search(r"\d+", await label.inner_text())
        current = int(m.group()) if m else 0
        while current != adults:
            if current < adults:
                await page.click("button.adult-counter-btn:not(.adult-counter-btn--minus)")
                current += 1
            else:
                await page.click("button.adult-counter-btn--minus")
                current -= 1
            await page.wait_for_timeout(200)
        for age in children_ages:
            await page.click("button.child-counter__add-btn")
            await page.wait_for_selector(".child-counter__list")
            # выбираем пункт по распарсенному числу (текст вида «7 лет», «1 год», «2 года»)
            clicked = await page.evaluate(
                """(age) => {
                    const items = [...document.querySelectorAll('.child-counter__list .child-counter__list__item')];
                    const el = items.find(e => parseInt((e.textContent || '').trim(), 10) === age);
                    if (el) { el.click(); return true; }
                    return false;
                }""",
                age,
            )
            if not clicked:
                raise RuntimeError(f"возраст ребёнка {age} не найден в списке")
            await page.wait_for_timeout(300)
        # закрыть блок
        await page.click("#touristSelector .tourist-current-select")
        await page.wait_for_timeout(300)

    async def _select_operators(self, page: Page, operators: list[str]) -> None:
        await page.click(".uis-text_tour-operator")
        await page.wait_for_timeout(700)
        # Снять «Все туроператоры» (по умолчанию отмечено) — это первый чекбокс списка
        # БЕЗ имени оператора (или с текстом «Все»). Клик настоящий (Playwright),
        # т.к. JS-клик по React-чекбоксу не всегда срабатывает.
        try:
            master_idx = await page.evaluate(
                """() => {
                    const labels = [...document.querySelectorAll("label[class*='tour-operator']")];
                    return labels.findIndex(l => {
                        const nm = ((l.querySelector('.slsf-white-space-nowrap') || l).textContent || '').trim();
                        return !nm || /^все/i.test(nm);
                    });
                }"""
            )
            if master_idx is not None and master_idx >= 0:
                master = page.locator("label[class*='tour-operator']").nth(master_idx)
                inp = master.locator("input")
                if await inp.count() and await inp.is_checked():
                    await master.click()
                    await page.wait_for_timeout(500)
        except Exception as exc:
            # Раньше — немой except:pass. Если «Все туроператоры» не снялся, в выдаче
            # останутся ВСЕ операторы, и «лучшая цена» могла прийти от незапрошенного
            # (audit P2). Пост-фильтр офферов небезопасен без живой сверки имён панели
            # (латиница _OPERATOR_MAP vs возможная кириллица) — пока хотя бы делаем сбой
            # видимым в логе, а не молчим.
            log.warning("Sletat: не удалось снять «Все туроператоры»: %s", exc)
        # Имя оператора теперь в `.slsf-white-space-nowrap` (раньше было `.slsf-text-bold`).
        # Матч ищем в JS (без проблем с кавычками в именах вроде Let's Fly), а клик —
        # настоящий, через Playwright (JS-клик по React-чекбоксу не всегда срабатывает).
        for name in operators:
            try:
                await page.fill(".uis-text_tour-operator", "")
                await page.type(".uis-text_tour-operator", name, delay=40)
                await page.wait_for_timeout(700)
                idx = await page.evaluate(
                    """(name) => {
                        const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        const want = norm(name);
                        const opName = l => norm((l.querySelector('.slsf-white-space-nowrap') || l).textContent);
                        const enabled = l => !/disabled/.test(l.className || '');
                        const labels = [...document.querySelectorAll("label[class*='tour-operator']")];
                        let i = labels.findIndex(l => enabled(l) && opName(l) === want);
                        if (i < 0) i = labels.findIndex(l => enabled(l) && opName(l).includes(want));
                        return i;
                    }""",
                    name,
                )
                if idx is None or idx < 0:
                    log.info("Sletat: оператор «%s» не найден/недоступен — пропуск", name)
                    continue
                label = page.locator("label[class*='tour-operator']").nth(idx)
                cls = await label.get_attribute("class") or ""
                if "checked" not in cls:
                    await label.click()
                    await page.wait_for_timeout(300)
            except Exception:
                continue
        await page.keyboard.press("Escape")

    async def _select_stars(self, page: Page, stars: list[int]) -> None:
        try:
            await page.click("#hotelCategoryOpenButton", timeout=4000)
            await page.wait_for_timeout(400)
            for s in stars:
                btn = page.locator(
                    f"xpath=//div[@id='hotelCategoryContainer']//button[contains(@class,'hot-buttons__button')"
                    f" and contains(normalize-space(text()), '{s}')]"
                )
                if await btn.count():
                    await btn.first.click()
                    await page.wait_for_timeout(200)
            await page.keyboard.press("Escape")
        except PWTimeout:
            pass

    async def _select_meals(self, page: Page, meals: list[str]) -> None:
        try:
            await page.click("#mealsOpenButton", timeout=4000)
            await page.wait_for_timeout(400)
            for code in meals:
                btn_text = _MEAL_BTN.get(code)
                if not btn_text:
                    continue
                btn = page.locator(
                    f"xpath=//div[@id='mealsContainer']//button[contains(@class,'hot-buttons__button')"
                    f" and normalize-space(text())='{btn_text}']"
                )
                if await btn.count():
                    await btn.first.click()
                    await page.wait_for_timeout(200)
            await page.keyboard.press("Escape")
        except PWTimeout:
            pass

    async def _select_resorts(self, page: Page, resorts: list[str]) -> None:
        """Открыть «Курорт» и отметить курорты по названию (клик по тексту пункта,
        а не по кнопке «+», которая раскрывает под-курорты). best-effort."""
        try:
            await page.click("#ui-select-resort", timeout=5000)
            await page.wait_for_timeout(800)
        except PWTimeout:
            return
        for resort in resorts:
            title = page.locator(
                f"xpath=//span[contains(@class,'uis-checkbox__label-title')"
                f" and normalize-space(text())='{resort}']"
            )
            if await title.count():
                await title.first.click()
                await page.wait_for_timeout(250)
            else:
                log.info("Sletat: курорт «%s» не найден в списке — пропуск", resort)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)

    async def _select_rating(self, page: Page, rating_min: float) -> None:
        """Отметить минимальный рейтинг отеля (flat-checkbox 7+/8+/9+ в slsf-rating-container).
        Берём ближайшую снизу ступень, доступную на сайте (7/8/9)."""
        step = 9 if rating_min >= 9 else 8 if rating_min >= 8 else 7
        loc = page.locator(
            f"xpath=//*[contains(@class,'slsf-rating-container')]//label[normalize-space(.)='{step}+']"
        )
        if await loc.count():
            await loc.first.click(force=True)  # flat-checkbox: клик по label переключает
            await page.wait_for_timeout(300)

    async def _select_price(self, page: Page, pmin, pmax) -> None:
        """Ввести диапазон цен в поля «от»/«до» (input.uis-text_price-input)."""
        inps = page.locator("input.uis-text_price-input")
        count = await inps.count()
        if pmin is not None and count >= 1:
            await inps.nth(0).fill(str(int(pmin)))
            await page.wait_for_timeout(200)
        if pmax is not None and count >= 2:
            await inps.nth(1).fill(str(int(pmax)))
            await page.wait_for_timeout(200)
        # снять фокус, чтобы значение применилось
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(200)

    async def _select_currency(self, page: Page, currency: str) -> None:
        """Сменить валюту: readonly-инпут #ui-select-currency_selector открывает список;
        кликаем опцию с ТОЧНЫМ кодом (RUB/USD/EUR уникальны — не путаются с ночами/курортами)."""
        try:
            await page.click("#ui-select-currency_selector", timeout=4000)
            await page.wait_for_timeout(400)
        except PWTimeout:
            return
        clicked = await page.evaluate(
            """(cur) => {
                const opts = [...document.querySelectorAll('.uis-select__options-item')]
                    .filter(o => (o.textContent || '').trim() === cur);
                const vis = opts.find(o => o.offsetParent !== null) || opts[0];
                if (vis) { vis.click(); return true; }
                return false;
            }""",
            currency,
        )
        if not clicked:
            log.info("Sletat: валюта «%s» не найдена в списке — пропуск", currency)
        await page.wait_for_timeout(300)

    async def _toggle_flight_flags(self, page: Page, params: SearchParams) -> None:
        async def set_flag(text: str, enable: bool) -> None:
            if not enable:
                return
            loc = page.locator(
                f"xpath=//label[contains(@class,'uis-checkbox__label_flight-info') and contains(., '{text}')]"
            )
            if await loc.count() == 0:
                return
            cls = await loc.first.get_attribute("class") or ""
            if "uis-checkbox__label_checked" not in cls:
                await loc.first.click()
                await page.wait_for_timeout(200)

        await set_flag("Чартерные", params.charter_only)
        await set_flag("Прямые", params.direct_only)
        await set_flag("С трансфером", params.with_transfer)
        # «Моментальное подтверждение» — тоже flight-info-чекбокс (label_instantApprove).
        await set_flag("Моментальное подтверждение", params.instant_confirmation)

    async def _click_search(self, page: Page) -> None:
        await page.click("[data-testid='b2b.search-form.search-btn']")

    # --- верификация формы перед поиском ---

    async def _verify_and_fix(self, page: Page, params: SearchParams) -> bool | None:
        """Сверить значения формы с параметрами; при расхождении переустановить шаг
        один раз, и если снова не совпало — прервать поиск (не искать по неверному).

        Возвращает, применился ли фильтр по оператору (None = проверка не делалась).
        Раньше расхождение по операторам просто логировалось: панель виртуализована, выбор
        best-effort, и для сравнения площадок это было терпимо — имена операторов есть в
        блинчике, так что цифры по ТО самокорректировались. Здесь терпеть нельзя: карточки
        отелей имени оператора не несут, и при неприменившемся фильтре список отелей — это
        отели ВСЕХ операторов. Вывод о том, каких отелей у оператора нет, стал бы ложным,
        поэтому статус проверки уходит наружу и делает прогон недостоверным.
        """
        problems = await self._verify_form(page, params)
        operators_ok: bool | None = None if not params.operators else not any(
            p[0] == "operators" for p in problems)
        if not problems:
            return operators_ok
        for field, _, _ in problems:
            try:
                await self._refill_field(page, params, field)
            except Exception:
                pass
        problems = await self._verify_form(page, params)
        if params.operators:
            operators_ok = not any(p[0] == "operators" for p in problems)
        # Операторы — мягкий фильтр: расхождение по ним НЕ прерывает поиск (иначе
        # виртуализованная панель роняла бы прогоны), но помечает результат недостоверным.
        blocking = [p for p in problems if p[0] != "operators"]
        if blocking:
            raise FormVerificationError(blocking)
        if problems:
            log.warning("Sletat: операторы применены не полностью — результат помечен "
                        "недостоверным: %s", problems)
        return operators_ok

    async def _safe_val(self, page: Page, selector: str):
        try:
            return await page.input_value(selector, timeout=2000)
        except Exception:
            return UNKNOWN

    async def _safe_text(self, page: Page, selector: str):
        # text_content (а не inner_text): значение может быть в скрытом/триггерном узле.
        try:
            loc = page.locator(selector)
            if await loc.count() == 0:
                return UNKNOWN
            return await loc.first.text_content()
        except Exception:
            return UNKNOWN

    async def _read_checked_operators(self, page: Page) -> set[str] | object:
        try:
            await page.click(".uis-text_tour-operator")
            await page.wait_for_timeout(500)
            names = await page.evaluate(
                """() => [...document.querySelectorAll("label[class*='tour-operator']")]
                    .filter(l => { const i = l.querySelector('input'); return i && i.checked; })
                    .map(l => ((l.querySelector('.slsf-white-space-nowrap') || l).textContent || '').trim())
                    .filter(Boolean)"""
            )
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
            return set(names)
        except Exception:
            return UNKNOWN

    async def _verify_form(self, page: Page, params: SearchParams) -> list[tuple[str, object, object]]:
        problems: list[tuple[str, object, object]] = []

        def check(field, expected, actual, ok: bool) -> None:
            if actual is UNKNOWN:
                return
            if not ok:
                problems.append((field, expected, actual))

        hotels = params.search_mode == "hotels"

        if not hotels:
            city = await self._safe_val(page, "input.excludeClickOutside")
            check("departure", params.departure_city, city, text_contains(params.departure_city, city) if city is not UNKNOWN else True)

        country = await self._safe_val(page, "#ui-select-country-to")
        if country is UNKNOWN or not norm(country):
            country = await self._safe_text(page, "#ui-select-country-to")
        check("country", params.destination_country, country, text_contains(params.destination_country, country) if country is not UNKNOWN else True)

        nmin = await self._safe_val(page, "#ui-select-nightsMin")
        nmax = await self._safe_val(page, "#ui-select-nightsMax")
        if nmin is not UNKNOWN and nmax is not UNKNOWN:
            check("nights", f"{params.nights_min}-{params.nights_max}", f"{nmin}-{nmax}",
                  exact(params.nights_min, nmin) and exact(params.nights_max, nmax))

        tourists = await self._safe_text(page, ".tourist-current-select")
        check("tourists", f"{params.adults} взрослых", tourists,
              text_contains(f"{params.adults} взросл", tourists) if tourists is not UNKNOWN else True)

        if params.operators:
            expected = {_OPERATOR_MAP.get(o.lower(), o) for o in params.operators}
            actual = await self._read_checked_operators(page)
            check("operators", expected, actual, set_equal(expected, actual) if actual is not UNKNOWN else True)

        if params.hotel_stars:
            stars_txt = await self._safe_text(page, "#hotelCategoryContainer .hotel-category-current-select")
            ok = stars_txt is UNKNOWN or all(str(s) in norm(stars_txt) for s in params.hotel_stars)
            check("stars", params.hotel_stars, stars_txt, ok)

        if params.meals:
            meals_txt = await self._safe_text(page, "#mealsContainer .hotel-category-current-select")
            check("meals", params.meals[0], meals_txt, text_contains(params.meals[0], meals_txt) if meals_txt is not UNKNOWN else True)

        # Цена: под параллельной нагрузкой ввод в поля «от»/«до» может «не доехать»
        # (гонка) — тогда поиск шёл бы БЕЗ ценового фильтра и вернул туры дороже лимита.
        # Сверяем по значению полей (цифры), при расхождении _refill_field переустановит.
        if params.price_min is not None or params.price_max is not None:
            try:
                inps = page.locator("input.uis-text_price-input")
                cnt = await inps.count()
                pmin_v = await inps.nth(0).input_value() if cnt >= 1 else ""
                pmax_v = await inps.nth(1).input_value() if cnt >= 2 else ""
            except Exception:
                pmin_v = pmax_v = UNKNOWN
            if pmin_v is not UNKNOWN:
                dmin = re.sub(r"\D", "", pmin_v or "")
                dmax = re.sub(r"\D", "", pmax_v or "")
                ok = True
                if params.price_min is not None:
                    ok = ok and dmin == str(int(params.price_min))
                if params.price_max is not None:
                    ok = ok and dmax == str(int(params.price_max))
                check("price", f"{params.price_min}/{params.price_max}", f"{pmin_v}/{pmax_v}", ok)

        return problems

    async def _refill_field(self, page: Page, params: SearchParams, field: str) -> None:
        if field == "departure":
            await self._select_departure_city(page, params.departure_city)
        elif field == "country":
            await self._select_country(page, params.destination_country)
        elif field == "nights":
            await self._set_nights(page, params.nights_min, params.nights_max)
        elif field == "tourists":
            await self._select_tourists(page, params.adults, params.children_ages)
        elif field == "operators":
            await self._select_operators(page, params.operators)
        elif field == "stars":
            await self._select_stars(page, params.hotel_stars)
        elif field == "meals":
            await self._select_meals(page, params.meals)
        elif field == "price":
            await self._select_price(page, params.price_min, params.price_max)

    # --- ожидание / сортировка / парсинг ---

    async def _wait_for_completion(
        self, page: Page, timeout_s: int = 120,
        op_seen: dict | None = None, op_start: float | None = None,
    ) -> None:
        """Завершение = текст статуса и число карточек стабильны (спиннеры не в счёт).

        Если переданы `op_seen`/`op_start` — попутно фиксируем, за сколько секунд у
        каждого оператора в блинчике появилась цена (best-effort, для колонки «Скорость»).
        """
        deadline = time.monotonic() + timeout_s
        last = None
        stable = 0
        while time.monotonic() < deadline:
            await page.wait_for_timeout(1500)
            if op_seen is not None and op_start is not None:
                try:
                    await self._ensure_panel_open(page)  # держим блинчик открытым — иначе цены не прогружаются
                    rows = await page.evaluate(
                        """() => [...document.querySelectorAll('li.blinchik__operator-item')].map(it => ({
                            name: (it.querySelector('label')?.textContent || '').trim(),
                            price: (it.querySelector('.blinchik__price .sr-currency-rub, .sr-currency-rub')?.textContent || '').trim(),
                        }))"""
                    )
                    t = round(time.monotonic() - op_start, 1)
                    for r in rows:
                        if r["name"] and r["price"] and r["name"] not in op_seen:
                            op_seen[r["name"]] = t
                except Exception:
                    pass
            try:
                items = await page.locator(".search-result__list-item").count()
            except Exception:
                items = -1
            try:
                status = (await page.locator(".search-status__tours-count").first.inner_text()).strip()
            except Exception:
                status = ""
            try:
                not_found = await page.locator(".tour-not-found-message").count()
            except Exception:
                not_found = 0
            key = (items, status)
            stable = stable + 1 if key == last else 0
            last = key
            if stable >= 3 and (items > 0 or not_found > 0):
                return

    def _operator_offers(
        self, rows: list[dict], op_seen: dict, hotel_offers: list[HotelOffer],
    ) -> list[OperatorOffer]:
        """Из строк блинчика (с ценой): оператор → мин. цена + скорость (op_seen) + отель (по цене)."""
        best: dict[str, tuple[Decimal, str]] = {}
        for r in rows:
            name = (r.get("name") or "").strip()
            price = _parse_price(r.get("price") or "")
            if not name or price is None:
                continue
            if name not in best or price < best[name][0]:
                best[name] = (price, (r.get("price") or "").strip())
        # отель сопоставляем по точному совпадению цены (best-effort)
        hotel_by_price: dict[Decimal, str] = {}
        for h in hotel_offers:
            hotel_by_price.setdefault(h.price, h.hotel_name)
        out = [
            OperatorOffer(
                provider=self.name, operator=name, price=price, currency="RUB",
                hotel_name=hotel_by_price.get(price), load_seconds=op_seen.get(name),
                raw_label=raw,
            )
            for name, (price, raw) in best.items()
        ]
        out.sort(key=lambda o: o.price)
        return out

    async def _open_operator_panel(self, page: Page) -> None:
        """Гарантированно РАЗВЕРНУТЬ панель операторов (blinchik) для скриншота выдачи.

        Открытость контролирует React: класс `blinchik_closed` (панель уезжает за левый
        край, x<0) возвращается при реконсиляции, поэтому снятие класса само по себе
        не держится. Помимо снятия класса ФОРСИРУЕМ позицию инлайн-стилем с !important —
        его React по className не трогает, и панель остаётся на экране до скриншота.
        (Кнопок внутри `.blinchik` нет — кликать нечего; управляем классами/стилем.)
        """
        try:
            await page.evaluate(
                """() => {
                    const b = document.querySelector('.blinchik');
                    if (!b) return;
                    b.classList.remove('blinchik_closed');
                    b.classList.add('blinchik_maximized', 'blinchik_visible');
                    b.style.setProperty('transform', 'none', 'important');
                    b.style.setProperty('left', '0', 'important');
                    b.style.setProperty('right', 'auto', 'important');
                    b.style.setProperty('margin-left', '0', 'important');
                }"""
            )
            await page.wait_for_timeout(500)
        except Exception:
            pass

    async def _sort_by_price(self, page: Page) -> None:
        try:
            await page.click(
                "xpath=//li[contains(@class,'uis-button-group__button') and normalize-space(text())='Цена']",
                timeout=4000,
            )
            await page.wait_for_timeout(2500)
        except PWTimeout:
            pass

    async def _ensure_panel_open(self, page: Page) -> None:
        """Открыть блинчик, если он закрыт (idempotent — не перелистывает состояние).
        Держим панель открытой, пока засекаем скорость прогрузки цен по операторам."""
        try:
            await page.evaluate(
                """() => {
                    const b = document.querySelector('.blinchik');
                    if (b && b.classList.contains('blinchik_closed')) {
                        const btn = b.querySelector('.blinchik__hide-button, .blinchik__show-button');
                        if (btn) btn.click();
                        b.classList.remove('blinchik_closed');
                    }
                }"""
            )
        except Exception:
            pass

    async def _parse_blinchik(self, page: Page) -> dict:
        """Снять блинчик с группировкой по статусу оператора.

        Возвращает {priced: [{name, price}], no_tours: [name], not_responding: [name]}.
        Группы разделены заголовками `li.blinchik__operator-list-title` («Туров нет»,
        «Оператор не отвечает»); операторы после заголовка относятся к его группе.
        """
        return await page.evaluate(
            r"""() => {
                const res = {priced: [], no_tours: [], not_responding: []};
                // На живой странице блинчик — в `.blinchik`; на офлайн-фикстуре может быть
                // голый список без обёртки → fallback на весь документ.
                const b = document.querySelector('.blinchik') || document;
                const norm = s => (s || '').replace(/\s+/g, ' ').trim();
                let group = 'priced';
                b.querySelectorAll('.blinchik__operator-list-title, li.blinchik__operator-item').forEach(el => {
                    if (el.classList.contains('blinchik__operator-list-title')) {
                        const t = norm(el.textContent).toLowerCase();
                        group = /туров нет/.test(t) ? 'no_tours'
                              : (/не отвеча/.test(t) ? 'not_responding' : 'priced');
                        return;
                    }
                    const name = norm(el.querySelector('label')?.textContent);
                    const price = norm(el.querySelector('.blinchik__price .sr-currency-rub, .sr-currency-rub')?.textContent);
                    if (!name) return;
                    if (price || group === 'priced') res.priced.push({name, price});
                    else if (group === 'no_tours') res.no_tours.push(name);
                    else if (group === 'not_responding') res.not_responding.push(name);
                });
                return res;
            }"""
        )

    async def _load_all_hotels(self, page: Page, cap: int) -> bool:
        """Домотать ленту выдачи, пока карточки прибавляются. True = упёрлись в лимит.

        Лимит — предохранитель от бесконечной ленты. О его срабатывании обязан узнать
        вызывающий код: тихо обрезанная выдача выглядит как полная, а значит порождает
        пропуски, которых нет.
        """
        seen = -1
        for _ in range(_SCROLL_ROUNDS):
            try:
                count = await page.locator(".search-result__list-item").count()
            except Exception:
                return False
            if count >= cap:
                log.warning("Sletat: выдача обрезана лимитом в %d отелей", cap)
                return True
            if count == seen:
                return False  # лента кончилась
            seen = count
            await page.mouse.wheel(0, 20_000)
            await page.wait_for_timeout(900)
        log.warning("Sletat: лента не сошлась за %d прокруток — выдача может быть неполной",
                    _SCROLL_ROUNDS)
        return True

    async def _parse_hotels(self, page: Page) -> list[HotelOffer]:
        rows = await page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('.search-result__list-item').forEach(it => {
                    // Имя отеля берём из JSON-LD карточки (каноничное), затем из .search-result-text
                    // (title/текст). НЕ из *-name-subscription — это слот бейджа («N оценок» /
                    // «Этот отель искали»), который маскировал реальное имя.
                    let name = '';
                    const ld = it.querySelector('script[type="application/ld+json"]');
                    if (ld) { try { name = (JSON.parse(ld.textContent).name || '').trim(); } catch (e) {} }
                    if (!name) {
                        const el = it.querySelector('.search-result-text_hotel, .search-result-text');
                        name = (el ? (el.getAttribute('title') || el.textContent || '') : '').trim();
                    }
                    const cat = it.querySelector('.search-result-category');
                    let stars = 0;
                    if (cat) { const m = (cat.className || '').match(/_stars-(\\d)/); if (m) stars = +m[1]; }
                    const rating = (it.querySelector('.search-result-rating')?.textContent || '').trim();
                    const destination = (it.querySelector('.search-result__full-group-destination')?.textContent || '').trim();
                    const price = (it.querySelector('.search-result__full-price')?.textContent || '').trim();
                    const operators = (it.querySelector('.search-result__full-group-operators-amount')?.textContent || '').trim();
                    if (name && price) out.push({name, stars, rating, destination, price, operators});
                });
                return out;
            }"""
        )
        return build_hotel_offers(self.name, rows)

    async def _safe_screenshot(self, page: Page) -> str | None:
        # Верхняя часть страницы во всю ширину (форма + «нашли N туров» + первые
        # результаты), но не вся бесконечная лента — так скриншот информативен и читабелен.
        path = f"screenshots/sletat_{datetime.now():%Y%m%d_%H%M%S_%f}.png"
        return await _capture_top(page, path)
