"""Тесты провайдера Турвизора через его JSON-эндпоинты."""

import asyncio
from unittest import mock

from pegasgap.providers import tourvisor_api

# ------------------------------- сбор страниц выдачи -------------------------------

def _page(codes):
    """Ответ modresult: витрина отдаёт выдачу С НАЧАЛА, а не только прирост."""
    return {"data": {"status": {"finished": 1},
                     "block": [{"operator": 12,
                                "hotel": [{"id": c, "price": 1000} for c in codes]}]}}


class _FakePages:
    """Подставной клиент: modsearch выдаёт следующий requestid, modresult — свою страницу."""

    def __init__(self, pages, stop_after=None):
        self.pages, self.stop_after, self.starts = pages, stop_after, 0

    async def get(self, url, params=None, **_):
        params = params or {}
        if "modsearch" in url:
            self.starts += 1
            if self.stop_after is not None and self.starts > self.stop_after:
                return _Resp({"result": {}})          # витрина: дальше ничего нет
            return _Resp({"result": {"requestid": 100 + self.starts}})
        idx = min(self.starts, len(self.pages) - 1)
        return _Resp(_page(self.pages[idx]))


class _Resp:
    status_code = 200

    def __init__(self, payload): self._p = payload
    def raise_for_status(self): return None
    def json(self): return self._p


def _collect(pages, stop_after=None, max_pages=10):
    from pegasgap.providers import tourvisor_api as tv
    provider = tv.TourvisorApiProvider()
    client = _FakePages(pages, stop_after)
    with mock.patch.object(tv, "POLL_INTERVAL_S", 0), \
         mock.patch.object(tv, "MAX_PAGES", max_pages):
        return asyncio.run(provider._collect_pages(client, 100, "https://tourvisor.ru/tours/"))


def test_pages_are_followed_until_nothing_new_arrives():
    """Живая сверка по Турции: первая страница 15 отелей, обход страниц — 177. Сравнивать
    свой полный каталог с первой страницей эталона значит не видеть пропусков вовсе."""
    blocks, finished, complete = _collect([[1, 2], [1, 2, 3, 4], [1, 2, 3, 4]])
    assert finished and complete
    assert tourvisor_api._hotel_codes(blocks) == {1, 2, 3, 4}


def test_page_limit_marks_the_result_truncated():
    """Упёрлись в предел, а прирост ещё шёл — выдача неполная, и молчать об этом нельзя:
    недогруженный отель неотличим от отсутствующего."""
    blocks, finished, complete = _collect([[1], [1, 2], [1, 2, 3]], max_pages=2)
    assert finished
    assert not complete
    assert tourvisor_api._hotel_codes(blocks) == {1, 2}


def test_shop_saying_there_is_no_next_page_is_completeness():
    blocks, finished, complete = _collect([[1, 2]], stop_after=0)
    assert finished and complete
    assert tourvisor_api._hotel_codes(blocks) == {1, 2}


def test_foreign_block_means_the_filter_did_not_apply():
    """Витрина размечает блоки идентификатором ТО, поэтому чужой блок означает ровно
    одно: серверный фильтр не применился, и выдача недобрана."""
    ours = [{"operator": 12, "hotel": []}]
    mixed = [{"operator": 12, "hotel": []}, {"operator": 11, "hotel": []}]
    assert tourvisor_api._blocks_are_ours(ours, 12)
    assert not tourvisor_api._blocks_are_ours(mixed, 12)


def test_empty_result_is_not_evidence_against_the_filter():
    assert tourvisor_api._blocks_are_ours([], 12)
