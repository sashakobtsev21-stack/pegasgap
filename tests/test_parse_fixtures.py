"""Офлайн-тесты парсинга на сохранённых снимках выдачи (без сети).

Ловят регрессии селекторов: площадки меняют вёрстку молча, и первым признаком становится
не ошибка, а внезапно опустевший отчёт — «пропусков нет» вместо «мы разучились читать
страницу». Эти тесты отличают одно от другого.
"""

from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.async_api import async_playwright

from pegasgap.providers.sletat import SletatProvider
from pegasgap.providers.tourvisor import TourvisorProvider

FX = Path(__file__).parent / "fixtures"


async def _parse(html: str, fn):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="domcontentloaded")
            return await fn(page)
        finally:
            await browser.close()


async def test_sletat_hotels_fixture_parses():
    html = (FX / "sletat_hotels.html").read_text(encoding="utf-8")
    offers = await _parse(html, SletatProvider()._parse_hotels)
    assert len(offers) >= 3
    assert all(o.price > 0 for o in offers)
    assert all(o.hotel_name for o in offers)
    assert any(o.stars for o in offers)


async def test_sletat_blinchik_splits_operator_statuses():
    """Разделение «Туров нет» / «Оператор не отвечает» — главный диагностический сигнал
    инструмента. Если парсинг групп сломается, оба случая схлопнутся в «нет цен»."""
    html = (FX / "sletat_operators.html").read_text(encoding="utf-8")
    blink = await _parse(html, SletatProvider()._parse_blinchik)
    assert isinstance(blink, dict)
    assert {"priced", "no_tours", "not_responding"} <= set(blink)
    assert blink["priced"], "в фикстуре есть операторы с ценами — парсер их потерял"


async def test_tourvisor_results_fixture_parses():
    html = (FX / "tourvisor_results.html").read_text(encoding="utf-8")
    offers = await _parse(html, TourvisorProvider()._parse_hotels)
    assert len(offers) >= 3
    assert all(o.price > 0 for o in offers)
    assert all(o.hotel_name for o in offers)
