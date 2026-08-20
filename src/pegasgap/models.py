"""Модели данных.

Два слоя:

* **Поисковый** (`SearchParams`, `Offer`, `HotelOffer`, `ProviderResult`) — общий формат
  запроса и выдачи площадки. Перенесён из исходного проекта сравнения площадок, потому
  что от него зависят провайдеры; менять его без нужды незачем.
* **Слой пропусков** (`OperatorStatus`, `GapKind`, `HotelGap`, `ScanResult`) — то, ради
  чего существует этот инструмент: не «кто дороже», а «где у оператора есть предложения
  на эталонной площадке и нет на проверяемой».

Всё — чистые pydantic-модели без браузера, БД и сети.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

SearchMode = Literal["tours", "hotels"]
SortBy = Literal["price", "popularity"]
Platform = Literal["sletat", "tourvisor"]

# Проверяемый оператор. Имя каноническое — в том виде, как оно записано на Слетать;
# провайдер Турвизора переводит его в своё написание сам (нечёткий матчинг + алиасы).
# Держим одной константой, чтобы имя не расползлось строкой по модулям: задача про
# Pegas, но хардкодить его в десяти местах — значит закладывать будущую правку в десяти.
PEGAS = "Pegas Touristik"

# Допустимые коды питания (унифицированные; провайдер мапит на свои подписи).
MEAL_CODES = {"none", "BB", "HB", "FB", "AI", "UAI"}


class SearchParams(BaseModel):
    """Параметры поиска — единый формат для обеих площадок и обоих режимов.

    Каждый провайдер переводит эти поля в действия на своей форме; неподдерживаемые
    конкретной площадкой поля она перечисляет в `ProviderResult.unsupported_filters`.
    """

    # --- обязательное ---
    departure_city: str
    destination_country: str
    date_from: date
    date_to: date
    nights_min: int = Field(ge=1)
    nights_max: int = Field(ge=1)
    adults: int = Field(ge=1)
    children_ages: list[int] = Field(default_factory=list)

    # --- режим поиска ---
    search_mode: SearchMode = "tours"  # "hotels" = без перелёта, только проживание

    # --- назначение ---
    resorts: list[str] = Field(default_factory=list)  # курорты; пусто = любой

    # --- отель ---
    hotel_stars: list[int] = Field(default_factory=list)  # [3,4,5]; пусто = любая
    meals: list[str] = Field(default_factory=list)        # коды MEAL_CODES; пусто = любое
    hotel_types: list[str] = Field(default_factory=list)
    hotels: list[str] = Field(default_factory=list)
    hotel_rating_min: float | None = None

    # --- оператор ---
    # По умолчанию — проверяемый оператор, а НЕ «все». Пустой список означал бы поиск по
    # всем ТО: выдача перестала бы быть выдачей оператора, и любой вывод о пропусках стал
    # бы неверным. Дефолт закрывает это по построению.
    operators: list[str] = Field(default_factory=lambda: [PEGAS])

    # --- рейсы / опции тура ---
    charter_only: bool = False
    direct_only: bool = False
    no_stops: bool = False
    with_transfer: bool = False
    instant_confirmation: bool = False

    # --- цена / сортировка / валюта ---
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    currency: str = "RUB"
    sort_by: SortBy = "price"

    @model_validator(mode="after")
    def _validate(self) -> SearchParams:
        if self.date_to < self.date_from:
            raise ValueError("date_to не может быть раньше date_from")
        if self.nights_max < self.nights_min:
            raise ValueError("nights_max не может быть меньше nights_min")
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("возраст ребёнка должен быть в диапазоне 0..17")
        if any(s < 1 or s > 5 for s in self.hotel_stars):
            raise ValueError("звёздность должна быть в диапазоне 1..5")
        bad_meals = [m for m in self.meals if m not in MEAL_CODES]
        if bad_meals:
            raise ValueError(f"недопустимые коды питания: {bad_meals}; разрешены {sorted(MEAL_CODES)}")
        if self.price_min is not None and self.price_max is not None and self.price_max < self.price_min:
            raise ValueError("price_max не может быть меньше price_min")
        return self

    @property
    def total_tourists(self) -> int:
        return self.adults + len(self.children_ages)

    def scenario_key(self) -> str:
        """Стабильный ключ запроса — чтобы связывать один и тот же сценарий между прогонами."""
        kids = ",".join(str(a) for a in self.children_ages)
        return (f"{self.search_mode}|{self.departure_city}|{self.destination_country}"
                f"|{self.date_from:%Y-%m-%d}..{self.date_to:%Y-%m-%d}"
                f"|{self.nights_min}-{self.nights_max}|{self.adults}+{kids}")


class Offer(BaseModel):
    """Предложение в разрезе туроператора: оператор + минимальная цена."""

    provider: str
    operator: str
    price: Decimal
    currency: str = "RUB"
    raw_label: str = ""

    @property
    def label(self) -> str:
        return self.operator


class OperatorOffer(BaseModel):
    """Оператор + отель + цена + скорость появления цены.

    `load_seconds` — за сколько секунд после старта поиска у оператора появилась цена;
    best-effort, где недоступно (Турвизор) — None. `hotel_name` сопоставляется по цене
    (best-effort), может быть None.
    """

    provider: str
    operator: str
    price: Decimal
    currency: str = "RUB"
    hotel_name: str | None = None
    load_seconds: float | None = None
    raw_label: str = ""


class HotelOffer(BaseModel):
    """Предложение по отелю: отель + цена и характеристики."""

    provider: str
    hotel_name: str
    price: Decimal
    currency: str = "RUB"
    stars: int | None = None
    rating: float | None = None
    destination: str | None = None
    operators_count: int | None = None
    raw_label: str = ""
    # Чем именно является это предложение. Цена сама по себе двусмысленна: в окне вылета
    # у отеля десяток заездов с разной ценой, и «минимум по окну» с двух площадок легко
    # приходится на РАЗНЫЕ даты. Живой случай: Myra — наш минимум на заезд 23.10, у
    # эталона на 17.10, и «расхождение −11.8%» на деле было разницей между датами.
    checkin: date | None = None
    meal: str | None = None
    room: str | None = None
    # Минимальная цена по каждому заезду окна. Нужна, чтобы сравнивать цены площадок на
    # ОДНУ дату: минимум по всему окну каждая площадка выбирает сама, и он сплошь и рядом
    # приходится на разные дни — тогда «дороже на 9%» это разница дат, а не площадок.
    prices_by_date: dict[date, Decimal] = Field(default_factory=dict)

    @property
    def label(self) -> str:
        stars = f" {self.stars}*" if self.stars else ""
        return f"{self.hotel_name}{stars}"


PricedItem = Offer | HotelOffer


class NotApplicableError(RuntimeError):
    """Площадка ДЕТЕРМИНИРОВАННО не обслуживает такой запрос — это не сбой.

    Неподходящий режим, направление вне карт площадки или запрос за её жёсткими
    лимитами: повтор даст тот же отказ. Оркестратор такой отказ не ретраит.

    Для задачи пропусков различие принципиально: «Турвизор не умеет это направление» —
    не пропуск Слетать, и в отчёт как пропуск попадать не должно.
    """


# Отказ вида «не обслуживает ТАКОЙ запрос», а не случайный сбой. Первичный признак —
# isinstance(NotApplicableError); regex остаётся фолбэком для строк из БД/старых записей.
# Связка « на » в «недоступен на» обязательна, чтобы не зацепить транзиентное
# «сервис недоступен» — то должно ретраиться. «превышает лимит» — именно с «лимит»,
# чтобы не зацепить «Превышен таймаут» оркестратора.
_NOT_APPLICABLE_RE = re.compile(
    r"не поддерживается|доступно в режиме|режиме «Отели»|укажите курорт|не предлагается"
    r"|не найдена в списке|недоступ(?:ен|на) на |не найден в справочнике|превышает лимит",
    re.IGNORECASE)


def is_not_applicable_error(error: str | None) -> bool:
    """True, если отказ означает «площадка не обслуживает такой запрос» (а не сбой)."""
    return bool(error and _NOT_APPLICABLE_RE.search(error))


class ProviderResult(BaseModel):
    """Результат поиска на одной площадке в заданном режиме."""

    provider: str
    success: bool
    duration_seconds: float
    search_mode: SearchMode = "tours"
    offers: list[Offer] = Field(default_factory=list)
    hotel_offers: list[HotelOffer] = Field(default_factory=list)
    operator_offers: list[OperatorOffer] = Field(default_factory=list)
    # Группы панели операторов Слетать («блинчик»). Ради этих двух полей во многом всё и
    # затевалось: они отличают «плагин отработал и сказал туров нет» от «плагин не
    # ответил» — корни у этих случаев совершенно разные.
    operators_no_tours: list[str] = Field(default_factory=list)
    operators_not_responding: list[str] = Field(default_factory=list)
    operators_available: list[str] = Field(default_factory=list)
    # Расхождения между тем, что просили, и тем, что вернулось (см. `paramcheck`).
    # Непусто = площадка искала не то, и сравнивать выдачи нельзя.
    param_mismatches: list[str] = Field(default_factory=list)
    error: str | None = None
    # Пояснение к состоявшемуся ответу — не ошибка. Например «оператор отключён на
    # направлении»: поиск не выполнялся, но это определённый факт, а не сбой.
    note: str | None = None
    screenshot_path: str | None = None
    search_url: str | None = None
    # Идентификатор поиска на стороне площадки. Записываем всегда: по нему поиск
    # находится в логах плагина целиком — с тем, сколько туров дал оператор, что мы
    # отфильтровали и какие даты не искались вовсе. Без него прогон не отследить, а
    # доступ к логам есть не у каждого запуска.
    request_id: int | None = None
    unsupported_filters: list[str] = Field(default_factory=list)
    # Применился ли фильтр по оператору на самой площадке. None = проверка не делалась.
    # На Турвизоре карточки отелей не несут имени ТО, поэтому неприменившийся фильтр даёт
    # цены «минимум по всем операторам» — и любой вывод о пропусках на них будет ложным.
    operator_filter_verified: bool | None = None
    # Выдача упёрлась в лимит и получена не целиком. Критично именно для этого инструмента:
    # недогруженный отель неотличим от отсутствующего, то есть обрезка порождает ложные
    # пропуски. Тихо обрезанная выдача выглядит как полная — поэтому флаг обязателен.
    truncated: bool = False

    def priced_items(self) -> list[PricedItem]:
        """Позиции с ценой для сравнения: в турах — операторы, в отелях — отели."""
        if self.search_mode == "hotels":
            return list(self.hotel_offers)
        return list(self.offers) or list(self.hotel_offers)

    @property
    def cheapest(self) -> PricedItem | None:
        return min(self.priced_items(), key=lambda o: o.price, default=None)


# --------------------------------------------------------------------------------------
# Слой пропусков
# --------------------------------------------------------------------------------------


class OperatorStatus(StrEnum):
    """Что площадка сказала про проверяемого оператора в этом поиске."""

    PRICED = "priced"                    # есть предложения с ценой
    NO_TOURS = "no_tours"                # площадка явно ответила «туров нет»
    NOT_RESPONDING = "not_responding"    # площадка: «оператор не отвечает»
    ABSENT = "absent"                    # оператора нет в списке площадки вообще
    UNKNOWN = "unknown"                  # поиск не удался / разрез по ТО недоступен

    @property
    def has_offers(self) -> bool:
        return self is OperatorStatus.PRICED


class GapKind(StrEnum):
    """Класс расхождения. Порядок членов = порядок приоритета в отчёте."""

    FULL = "full"                      # на Турвизоре предложения есть, на Слетать ни одного
    NOT_RESPONDING = "not_responding"  # на Слетать оператор не ответил
    HOTEL = "hotel"                    # отель есть на Турвизоре, на Слетать у оператора нет
    PRICE = "price"                    # есть на обеих, цена расходится сверх порога
    REVERSE = "reverse"                # есть на Слетать, на Турвизоре нет

    @property
    def title(self) -> str:
        # Названия говорят, ЧЕГО нет и ГДЕ. Раз ни одна площадка больше не считается
        # эталоном, имя площадки обязано стоять в названии.
        #
        # Два отельных класса — зеркальные, и названы одинаково с точностью до площадки:
        # так симметрия видна прямо в списке. Отличаются они от полного пропуска словом
        # «отеля» против «туров» — то есть ровно тем, чем отличаются по смыслу: одна
        # запись против всего направления.
        return {
            GapKind.FULL: "Нет туров на Слетать",
            GapKind.NOT_RESPONDING: "Оператор не ответил",
            GapKind.HOTEL: "Отеля нет на Слетать",
            GapKind.PRICE: "Цена расходится",
            GapKind.REVERSE: "Отеля нет на Турвизоре",
        }[self]

    @property
    def hint(self) -> str:
        """Куда смотреть коллеге, который берёт этот случай в разбор."""
        return {
            GapKind.FULL: "плагин отработал и вернул пусто: направление/даты не заведены, "
                          "фильтр отсёк всё, либо поиск не дошёл до оператора",
            GapKind.NOT_RESPONDING: "таймаут, бан или падение плагина — смотреть логи поиска, "
                                    "не справочники",
            GapKind.HOTEL: "чаще всего нет линковки отеля или мислинк: отель не сопоставлен "
                           "с внутренним справочником",
            GapKind.PRICE: "наценка, состав тура (ночи/питание/номер) или курс валюты",
            GapKind.REVERSE: "либо у витрины неполная программа, либо мы показываем то, чего на рынке уже нет — вторая версия хуже",
        }[self]


class HotelDiagnosis(StrEnum):
    """Почему отеля не оказалось в нашей выдаче — по внутренним справочникам.

    Ради этого различия инструмент и затевался: «пропуска нет» без причины — это тикет,
    который некому взять. Каждое значение указывает на конкретное действие и на конкретную
    команду, которая его выполняет.
    """

    NOT_IN_CATALOG = "not_in_catalog"      # отеля нет в справочнике Слетать
    NOT_LINKED = "not_linked"              # отель есть, но у оператора нет линковки
    # Отель слинкован, но выключен в справочнике Слетать: сколько бы туров оператор
    # ни прислал, показан он не будет. Причина и исполнитель тут свои.
    CATALOG_DISABLED = "catalog_disabled"
    LINKED_NO_OFFER = "linked_no_offer"    # линковка есть, а тура в выдаче не было
    # Отель в справочнике опознан, но линковку проверить не удалось (нет доступа к базе).
    # Отдельно от UNKNOWN: половина ответа уже есть, и путать «мы кое-что выяснили» с
    # «мы не смотрели» значит терять то, что выяснили.
    IN_CATALOG_UNCHECKED = "in_catalog_unchecked"
    UNCERTAIN = "uncertain"                # со справочником уверенно не сопоставился
    UNKNOWN = "unknown"                    # проверка не выполнялась

    @property
    def title(self) -> str:
        return {
            HotelDiagnosis.NOT_IN_CATALOG: "нет в справочнике",
            HotelDiagnosis.NOT_LINKED: "нет линковки",
            HotelDiagnosis.CATALOG_DISABLED: "отель выключен",
            HotelDiagnosis.LINKED_NO_OFFER: "линкован, тура нет",
            HotelDiagnosis.IN_CATALOG_UNCHECKED: "есть в справочнике",
            HotelDiagnosis.UNCERTAIN: "не опознан",
            HotelDiagnosis.UNKNOWN: "не проверялось",
        }[self]

    @property
    def cause(self) -> str:
        """Вероятная причина словами — то, что читают в отчёте вместо ярлыка.

        Ярлык вроде «есть в справочнике» понятен тому, кто знает устройство разбора;
        человеку, открывшему отчёт, нужна фраза, из которой сразу видно, что случилось
        и куда идти.
        """
        return {
            HotelDiagnosis.NOT_IN_CATALOG: "отеля нет в справочнике Слетать — не заведён",
            HotelDiagnosis.NOT_LINKED: "отель в справочнике есть, но не связан с каталогом "
                                       "оператора",
            HotelDiagnosis.LINKED_NO_OFFER: "справочники в порядке — значит у оператора нет "
                                            "наличия либо поиск до него не дошёл",
            HotelDiagnosis.IN_CATALOG_UNCHECKED: "отель в справочнике есть; вероятнее всего "
                                                 "нет линковки с каталогом оператора",
            HotelDiagnosis.UNCERTAIN: "возможно, отель у нас есть под другим названием",
            HotelDiagnosis.UNKNOWN: "причина не разобрана — не было доступа к справочникам",
        }[self]

    @property
    def action(self) -> str:
        return {
            HotelDiagnosis.NOT_IN_CATALOG: "завести отель в справочнике Слетать",
            HotelDiagnosis.NOT_LINKED: "связать отель оператора с внутренним справочником",
            HotelDiagnosis.LINKED_NO_OFFER: "справочники в порядке — смотреть наличие "
                                            "у оператора и логи поиска",
            HotelDiagnosis.IN_CATALOG_UNCHECKED: "проверить линковку у оператора "
                                                 "(нужен доступ к плагинной базе)",
            HotelDiagnosis.UNCERTAIN: "сверить название вручную: возможно, отель у нас есть "
                                      "под другим именем",
            HotelDiagnosis.UNKNOWN: "запустить с доступом к справочникам",
        }[self]


class HotelGap(BaseModel):
    """Одно расхождение по отелю — единица разбора для коллеги."""

    kind: GapKind
    hotel_name: str                       # каноничное имя для отчёта
    stars: int | None = None
    resort: str | None = None
    reference_price: Decimal | None = None  # цена на эталонной площадке (Турвизор)
    checked_price: Decimal | None = None    # цена на проверяемой (Слетать)
    currency: str = "RUB"
    matched_name: str | None = None       # как отель называется на второй площадке
    note: str = ""
    # Разбор причины по внутренним справочникам. Заполняется отдельным проходом
    # (`diagnosis.diagnose`) уже после классификации: `detect` остаётся чистой функцией
    # без сети и БД, а диагноз требует и того, и другого.
    diagnosis: HotelDiagnosis = HotelDiagnosis.UNKNOWN
    reference_hotel_id: int | None = None  # ID отеля на витрине — для ссылки на её поиск
    catalog_id: int | None = None         # внутренний ID отеля в справочнике Слетать
    catalog_name: str | None = None       # как отель называется в справочнике
    # На какой заезд, питание и номер пришлась НАША цена. Без этого расхождение цены не
    # воспроизводится: в окне вылета у отеля десяток заездов с разной ценой, и по ссылке
    # на всё окно площадка покажет другое предложение — число из отчёта не сойдётся с тем,
    # что видит человек. Живой случай: Myra, наш минимум на заезд 23.10 (34 536), а
    # страница открывала 17.10 (39 154).
    # Заезд с ОБЕИХ сторон: минимум по окну берётся независимо, и даты легко
    # расходятся. Показать только свою — значит выдать разницу дат за разницу
    # площадок, а это первое, что надо исключить, глядя на расхождение цены.
    reference_checkin: date | None = None
    checked_checkin: date | None = None
    checked_meal: str | None = None
    checked_room: str | None = None

    @property
    def diff_abs(self) -> Decimal | None:
        if self.reference_price is None or self.checked_price is None:
            return None
        return self.checked_price - self.reference_price

    @property
    def diff_pct(self) -> float | None:
        if self.reference_price is None or self.checked_price is None or not self.reference_price:
            return None
        return float((self.checked_price - self.reference_price) / self.reference_price * 100)

    def key(self) -> str:
        """Стабильный ключ расхождения — чтобы отличать новый случай от висящего с прошлого раза."""
        return f"{self.kind.value}|{self.hotel_name.strip().lower()}|{self.stars or ''}"


class ScanResult(BaseModel):
    """Итог одного прогона одного запроса по обеим площадкам."""

    params: SearchParams
    operator: str = PEGAS
    run_at: datetime = Field(default_factory=datetime.now)
    reference: ProviderResult | None = None   # Турвизор — эталон
    checked: ProviderResult | None = None     # Слетать — проверяемая
    reference_status: OperatorStatus = OperatorStatus.UNKNOWN
    checked_status: OperatorStatus = OperatorStatus.UNKNOWN
    gaps: list[HotelGap] = Field(default_factory=list)
    # Отели, которые не удалось надёжно сопоставить между площадками. В пропуски они НЕ
    # попадают: ложный пропуск отправляет коллегу разбирать несуществующую проблему и
    # быстро убивает доверие к отчёту. Разбираются отдельной секцией.
    unmatched: list[str] = Field(default_factory=list)
    # Нарушенные инварианты прогона (несравнимые валюты, неприменившийся фильтр ТО,
    # недособранная выдача). Непустой список = находкам этого прогона верить нельзя.
    problems: list[str] = Field(default_factory=list)
    # Особенности прогона, которые НЕ ставят находки под сомнение, но нужны для их
    # правильного чтения. Отдельно от problems сознательно: свойство площадок (например,
    # разный объём программы оператора или недочитанный до конца эталон) — не поломка, и
    # если считать его поломкой, инструмент будет объявлять недостоверным каждый прогон.
    notes: list[str] = Field(default_factory=list)
    # Систематический сдвиг цен между площадками, % (медиана по сопоставленным парам).
    # Не находка, а фон: витрины считают цену на разной базе, и постоянная разница в
    # несколько процентов — их свойство. Находки по цене считаются ОТ этого фона.
    price_offset_pct: float | None = None
    # Сколько отелей эталона удалось сопоставить — мера доверия к разбору.
    matched_hotels: int = 0
    reference_hotels: int = 0
    # Ссылка на тот же поиск на витрине. Строит её провайдер: только он знает числовые
    # идентификаторы страны, города и оператора, которых больше нигде нет.
    reference_url: str | None = None
    # Идентификатор нашего поиска на площадке — ключ ко всем логам плагина
    # по этому прогону. Хранится всегда, даже когда читать логи нечем.
    checked_request_id: int | None = None
    # Размер НАШЕЙ выдачи. Без него неполный ответ шлюза неотличим от
    # реального пропуска: 42 отеля против обычных 347 выглядят одинаково.
    checked_hotels: int = 0

    @property
    def trustworthy(self) -> bool:
        """Можно ли показывать пропуски этого прогона как факты."""
        return not self.problems

    def gaps_of(self, kind: GapKind) -> list[HotelGap]:
        return [g for g in self.gaps if g.kind is kind]

    @property
    def summary(self) -> dict[str, int]:
        return {k.value: len(self.gaps_of(k)) for k in GapKind}
