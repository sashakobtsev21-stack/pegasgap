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


def test_empty_result_is_complete_not_truncated():
    """«У оператора тут туров нет» — полный ответ, а не недобор. Цикл постраничного сбора
    выходил по break и падал в ветку «упёрлись в предел», и каждое пустое направление
    помечалось обрезанным: живой пример — Абхазия у Pegas."""
    blocks, finished, complete = _collect(([],))
    assert finished and complete
    assert blocks == [{"operator": 12, "hotel": []}]


# --- Полнота выдачи -----------------------------------------------------------------

def test_one_page_that_never_grew_is_not_proof_of_completeness():
    """В режиме «отели» витрина отдаёт ровно 50 самых дешёвых, а `nextpage` возвращает ту
    же страницу. Прежний цикл видел «прирост иссяк» и объявлял выдачу собранной целиком —
    после чего обратная сторона сравнивала наши 209 отелей с их полусотней и выдавала
    полторы сотни «нет на Турвизоре» на прогон. Отель Britannia при этом был на ОБЕИХ
    площадках и по одной цене."""
    assert not tourvisor_api._page_is_whole(advanced=False, seen=set(range(50)))
    assert not tourvisor_api._page_is_whole(advanced=False, seen=set(range(16)))


def test_a_page_that_did_not_even_fill_is_complete():
    """Иначе любое маленькое направление навсегда осталось бы без обратной стороны."""
    assert tourvisor_api._page_is_whole(advanced=False, seen=set(range(7)))


def test_pagination_that_worked_proves_the_end():
    """Сдвинулась хоть раз — исчерпание прироста означает конец: страницы кончились."""
    assert tourvisor_api._page_is_whole(advanced=True, seen=set(range(594)))


async def test_page_that_loses_seen_hotels_marks_the_read_incomplete(monkeypatch):
    """Ответ `nextpage` кумулятивен — каждая страница содержит всё с начала. Страница,
    потерявшая уже виденные отели, означает сбой листания: молча заместить выдачу
    урезанной нельзя, недобор помечается."""
    provider = tourvisor_api.TourvisorApiProvider()

    def page(ids):
        return [{"operator": 12, "hotel": [{"id": i, "price": 100} for i in ids]}]

    pages = iter([(page([1, 2, 3]), True), (page([2]), True)])

    async def fake_await(self, client, request_id, referrer):
        return next(pages)

    async def fake_get(self, client, url, referrer=None, **query):
        return {"result": {"requestid": 777}}

    monkeypatch.setattr(tourvisor_api.TourvisorApiProvider, "_await_result", fake_await)
    monkeypatch.setattr(tourvisor_api.TourvisorApiProvider, "_get", fake_get)
    blocks, finished, complete = await provider._collect_pages(None, 1, "ref")
    assert finished and not complete            # честный недобор, не «полная выдача»
    assert tourvisor_api._hotel_codes(blocks) == {1, 2, 3}   # сохранили лучшее из виденного


def test_garbage_blocks_do_not_crash_the_builder():
    """Л3 плана: битый ответ обязан дать честный ноль, а не исключение посреди прогона."""
    junk = [
        {"operator": "мусор", "hotel": "не список"},
        {"hotel": [{"id": "не число", "price": "мусор"},
                   {"id": 5, "price": -10},
                   {"id": 6},
                   "строка вместо отеля"]},
        {},
        None,
    ]
    blocks = [b for b in junk if isinstance(b, dict)]
    assert tourvisor_api.build_hotel_offers(blocks, {}, None) == []
    assert tourvisor_api._hotel_codes(blocks) == {5, 6}
    assert tourvisor_api.offer_facts(blocks) == []


async def test_single_stall_is_retried_before_believing_the_end(monkeypatch):
    """Под нагрузкой витрина изредка отдаёт ту же страницу повторно, и один пустой
    прирост — ещё не конец: живой прогон так объявил полным листинг из 16 отелей при
    ~66 реальных. Конец подтверждается вторым пустым приростом подряд."""
    provider = tourvisor_api.TourvisorApiProvider()

    def page(ids):
        return [{"operator": 12, "hotel": [{"id": i, "price": 100} for i in ids]}]

    feed = iter([
        (page([1, 2]), True),          # первая страница
        (page([1, 2]), True),          # заикание — та же
        (page([1, 2, 3]), True),       # повтор принёс продолжение
        (page([1, 2, 3]), True),       # пусто раз
        (page([1, 2, 3]), True),       # пусто два — теперь конец
    ])

    async def fake_await(self, client, request_id, referrer):
        return next(feed)

    async def fake_get(self, client, url, referrer=None, **query):
        return {"result": {"requestid": 777}}

    monkeypatch.setattr(tourvisor_api.TourvisorApiProvider, "_await_result", fake_await)
    monkeypatch.setattr(tourvisor_api.TourvisorApiProvider, "_get", fake_get)
    blocks, finished, complete = await provider._collect_pages(None, 1, "ref")
    assert finished and complete
    assert tourvisor_api._hotel_codes(blocks) == {1, 2, 3}
