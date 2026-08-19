"""Командный интерфейс.

Три команды под три сценария использования:

* `scan`   — точечно проверить одно направление (разбор конкретной жалобы);
* `sweep`  — регулярный обход матрицы направлений (то, что вешается на расписание);
* `report` — что накопилось за период.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from pegasgap import report as reporting
from pegasgap import storage
from pegasgap.catalog import fetch_catalog, resolve_country_id
from pegasgap.diagnosis import diagnose
from pegasgap.gaps import detect
from pegasgap.linking import load_links
from pegasgap.logging_setup import configure_logging
from pegasgap.models import PEGAS, GapKind, ScanResult, SearchParams
from pegasgap.orchestrator import CHECKED, REFERENCE, run_pair
from pegasgap.providers.sletat_api import GATEWAY_CITY
from pegasgap.ranking import (
    RouteVolume,
    VolumeProbe,
    client_factory,
    reference_has_operator,
    to_yaml_routes,
)
from pegasgap.scenarios import DEFAULT_CONFIG, load_matrix

app = typer.Typer(add_completion=False, help="Мониторинг пропусков туроператора на выдаче Слетать.")


def _force_utf8_output() -> None:
    """Перевести вывод в UTF-8.

    Консоль Windows по умолчанию работает в cp1251, где нет ни «→», ни рамок таблиц —
    и любой отчёт валится с UnicodeEncodeError уже после того, как оба поиска отработали.
    Терять результат двухминутного прогона на кодировке вывода недопустимо, поэтому
    переключаем потоки принудительно, а `errors="replace"` страхует от экзотики.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # поток не текстовый или перенаправлен
                pass


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Подтянуть переменные из .env, не перекрывая уже заданные в окружении.

    Свои десять строк вместо зависимости: формат тривиален, а доступы к шлюзу нужны
    буквально в двух переменных. Приоритет окружения над файлом — чтобы разовый запуск
    с другими доступами не требовал править файл.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


_force_utf8_output()
_load_dotenv()
console = Console()


async def _run_one(params: SearchParams, operator: str, headless: bool,
                   with_diagnosis: bool = True) -> ScanResult:
    results = await run_pair(params, headless=headless)
    scan = detect(params, results.get(REFERENCE), results.get(CHECKED), operator=operator)
    if with_diagnosis and scan.gaps_of(GapKind.HOTEL):
        # Разбор причин стоит запроса справочника, поэтому делаем его только когда есть
        # что разбирать: на чистом прогоне это была бы плата ни за что.
        await _diagnose(scan)
    return scan


async def _diagnose(scan: ScanResult) -> None:
    """Проставить отельным пропускам причину по справочникам. Ошибки не фатальны."""
    country_id = await resolve_country_id(scan.params.destination_country)
    catalog = await fetch_catalog(country_id) if country_id else []
    # Чтение базы блокирующее — уводим в поток, чтобы не морозить цикл событий, когда
    # обход идёт параллельно.
    links = await asyncio.to_thread(load_links)
    diagnose(scan, catalog, links)


async def _run_many(items: list[SearchParams], operator: str | None, headless: bool,
                    jobs: int) -> list[ScanResult]:
    """Прогнать сценарии с ограничением параллельности.

    Каждый сценарий поднимает два браузера, поэтому `jobs` — это удвоенное число
    одновременных Chromium. Без ограничения десяток сценариев съел бы всю память машины.
    """
    sem = asyncio.Semaphore(max(1, jobs))
    done = 0

    async def one(p: SearchParams) -> ScanResult:
        nonlocal done
        async with sem:
            # Оператор берётся из самого сценария: обход многооператорный,
            # и один на всех означал бы разбор чужой выдачи.
            scan = await _run_one(p, operator or p.operators[0], headless)
        done += 1
        console.print(f"[dim]({done}/{len(items)}) {p.departure_city} → "
                      f"{p.destination_country}, {p.search_mode}: "
                      f"находок {len(scan.gaps)}[/dim]")
        return scan

    return list(await asyncio.gather(*(one(p) for p in items)))


def _persist_and_show(scans: list[ScanResult], db: Path, csv_path: Path | None,
                      html_path: Path | None) -> None:
    """Сохранить прогоны, показать их и, если попросили, выгрузить в файлы."""
    all_new: set[str] = set()
    with storage.session(db) as conn:
        for scan in scans:
            # Новизну считаем ДО сохранения: иначе прогон запишет сам себя в историю
            # и все находки станут «уже виденными».
            fresh = {g.key() for g in storage.new_gaps(conn, scan)}
            all_new |= fresh
            storage.save_scan(conn, scan)
            reporting.render_scan(scan, fresh, console)

    total = sum(len(s.gaps) for s in scans)
    shaky = [s for s in scans if not s.trustworthy]
    console.print(f"\n[bold]Итого:[/bold] находок {total}, из них новых {len(all_new)}; "
                  f"прогонов {len(scans)}, недостоверных {len(shaky)}")
    if shaky:
        console.print("[red]Недостоверные прогоны в сводку по находкам не засчитаны — "
                      "сначала разберитесь с причинами выше.[/red]")

    if csv_path:
        reporting.write_csv(scans, csv_path, all_new)
        console.print(f"CSV: {csv_path}")
    if html_path:
        reporting.write_html(scans, html_path, all_new)
        console.print(f"HTML: {html_path}")


@app.command()
def scan(
    country: str = typer.Option(..., "--country", "-c", help="Страна назначения"),
    departure: str = typer.Option("Москва", "--from", "-f", help="Город вылета"),
    date_from: datetime = typer.Option(..., "--date-from", formats=["%Y-%m-%d"],
                                       help="Начало окна вылета, ГГГГ-ММ-ДД"),
    date_to: datetime = typer.Option(None, "--date-to", formats=["%Y-%m-%d"],
                                     help="Конец окна вылета; по умолчанию +7 дней"),
    nights: int = typer.Option(7, "--nights", "-n", help="Ночей (минимум)"),
    nights_max: int = typer.Option(None, "--nights-max", help="Ночей (максимум); по умолчанию = --nights"),
    adults: int = typer.Option(2, "--adults", "-a"),
    mode: str = typer.Option("tours", "--mode", "-m", help="tours (с перелётом) | hotels (без)"),
    operator: str = typer.Option(PEGAS, "--operator", help="Проверяемый туроператор"),
    headless: bool = typer.Option(True, "--headless/--show", help="Скрытый браузер или видимый"),
    db: Path = typer.Option(storage.DEFAULT_DB, "--db"),
    csv_path: Path = typer.Option(None, "--csv", help="Выгрузить находки в CSV"),
    html_path: Path = typer.Option(None, "--html", help="Выгрузить отчёт в HTML"),
) -> None:
    """Точечно проверить одно направление."""
    configure_logging(logging.INFO)
    if mode not in ("tours", "hotels"):
        raise typer.BadParameter("режим должен быть tours или hotels", param_hint="--mode")
    start = date_from.date()
    end = date_to.date() if date_to else start + timedelta(days=7)
    params = SearchParams(
        departure_city=departure, destination_country=country,
        date_from=start, date_to=end,
        nights_min=nights, nights_max=nights_max or nights,
        adults=adults, search_mode=mode,  # type: ignore[arg-type]
        operators=[operator],
    )
    scans = [asyncio.run(_run_one(params, operator, headless))]
    _persist_and_show(scans, db, csv_path, html_path)


@app.command()
def sweep(
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", help="Файл матрицы сценариев"),
    jobs: int = typer.Option(2, "--jobs", "-j", help="Сколько сценариев одновременно "
                                                     "(каждый — это два браузера)"),
    headless: bool = typer.Option(True, "--headless/--show"),
    db: Path = typer.Option(storage.DEFAULT_DB, "--db"),
    csv_path: Path = typer.Option(None, "--csv"),
    html_path: Path = typer.Option(None, "--html"),
) -> None:
    """Обойти всю матрицу направлений из файла сценариев."""
    configure_logging(logging.INFO)
    matrix = load_matrix(config)
    items = matrix.build(date.today())
    console.print(f"[bold]Обход:[/bold] {len(items)} сценариев, "
                  f"операторы: {', '.join(matrix.operators)}, "
                  f"параллельно {jobs}")
    scans = asyncio.run(_run_many(items, None, headless, jobs))
    _persist_and_show(scans, db, csv_path, html_path)


@app.command()
def top(
    limit: int = typer.Option(12, "--top", "-n", help="Сколько направлений показать"),
    departure: str = typer.Option(GATEWAY_CITY, "--from", "-f", help="Город вылета"),
    nights: int = typer.Option(7, "--nights"),
    offset: int = typer.Option(30, "--offset-days", help="Через сколько дней вылет"),
    operator_id: int = typer.Option(3, "--operator-id", help="ID оператора в справочнике"),
) -> None:
    """Найти направления с наибольшим объёмом у оператора.

    Печатает готовый блок `routes:` для scenarios.yaml. Автоматически он НЕ применяется:
    список направлений, который меняется сам, — плохое свойство для мониторинга, потому
    что направление может тихо выпасть из наблюдения и никто этого не заметит. Решение
    остаётся за человеком и видно в истории изменений конфига.
    """
    configure_logging(logging.WARNING)   # замер шумный, а нужен только его результат
    routes, skipped, absent = asyncio.run(
        _measure_top(limit, departure, nights, offset, operator_id))

    table = Table(title=f"Направления по объёму оператора · вылет из «{departure}»",
                  header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("страна")
    table.add_column("предложений", justify="right")
    for i, volume in enumerate(routes, 1):
        table.add_row(str(i), volume.country, str(volume.rows))
    console.print(table)

    if absent:
        console.print(f"\n[yellow]Пропущены — оператора нет на эталоне ({len(absent)}):[/yellow]")
        for volume in absent:
            console.print(f"  • {volume.country} [dim]— у нас {volume.rows} предложений, "
                          f"а на витрине по этому оператору ничего[/dim]")
        console.print("[dim]Мониторить такое нечем: эталон пуст, сравнивать не с чем, "
                      "а квоту поисков направление тратит наравне с рабочим.[/dim]")

    if skipped:
        console.print(f"\n[yellow]Не удалось замерить ({len(skipped)}):[/yellow]")
        for volume in skipped[:15]:
            console.print(f"  • {volume.country} [dim]— {volume.error} "
                          f"({volume.seconds:.1f} с)[/dim]")
        console.print("[dim]Если дело в скорости — такое направление в регулярный обход "
                      "лучше не брать: времени съест много, находок даст столько же.[/dim]")

    console.print("\n[bold]Вставьте в scenarios.yaml[/bold] "
                  "[dim](вместо блоков countries и departure_cities)[/dim]:\n")
    console.print(to_yaml_routes(routes))


async def _measure_top(limit: int, departure: str, nights: int, offset: int,
                       operator_id: int) -> tuple[list[RouteVolume], list[RouteVolume],
                                                  list[RouteVolume]]:
    """Три шага: отсечь страны без оператора, замерить объём, сверить с эталоном.

    Первый шаг почти бесплатный и снимает четыре пятых работы: из сотни стран у
    оператора активны единицы. Третий отсекает направления, где сравнивать не с чем.
    """
    probe = VolumeProbe(operator_id=operator_id, nights=nights, offset_days=offset)
    async with client_factory() as client:
        await probe.load_refdata(client)

        console.print("[dim]1/2 ищу страны, где оператор активен…[/dim]")
        countries = await probe.active_countries(client)
        if not countries:
            raise typer.BadParameter("оператор не активен ни в одной стране")
        console.print(f"[dim]    активен в {len(countries)}: {', '.join(countries)}[/dim]")

        console.print(f"[dim]2/3 замеряю объём по {len(countries)} странам…[/dim]")
        measured = await probe.rank(client, [(departure, c) for c in countries])

    with_volume = [v for v in measured if v.has_volume]
    console.print(f"[dim]3/3 проверяю, есть ли оператор на эталоне "
                  f"({len(with_volume)} направлений)…[/dim]")
    # Наш объём — только половина ответа. Направление, где оператора нет на витрине,
    # мониторить бессмысленно: сравнивать не с чем, а квоту поисков оно жжёт наравне с
    # рабочим. Живой пример — ОАЭ: у нас 9289 предложений, у него на витрине ноль.
    checked = await asyncio.gather(*(
        reference_has_operator(v.country, PEGAS) for v in with_volume))
    with_volume = [replace(v, on_reference=ok) for v, ok in zip(with_volume, checked,
                                                                strict=True)]

    return ([v for v in with_volume if v.on_reference is not False][:limit],
            [v for v in measured if not v.measured],
            [v for v in with_volume if v.on_reference is False])


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="Слушать на этом адресе"),
    port: int = typer.Option(8000, "--port", "-p"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", help="Файл матрицы для обхода"),
    db: Path = typer.Option(storage.DEFAULT_DB, "--db"),
    reload: bool = typer.Option(False, "--reload", help="Автоперезапуск при правках кода"),
) -> None:
    """Поднять веб-интерфейс (дашборд на http://host:port/)."""
    configure_logging(logging.INFO)
    try:
        import uvicorn
    except ImportError as exc:
        raise typer.BadParameter(
            "не установлены веб-зависимости: pip install -e \".[web]\"") from exc
    from pegasgap.web import create_app

    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if not dist.is_dir():
        console.print("[yellow]Фронтенд не собран — интерфейс будет недоступен.[/yellow]")
        console.print("[yellow]Соберите: cd frontend && npm install && npm run build[/yellow]")
    console.print(f"[bold]Дашборд:[/bold] http://{host}:{port}/")
    uvicorn.run(create_app(db_path=db, config_path=config), host=host, port=port,
                reload=reload, log_level="info")


@app.command()
def report(
    days: int = typer.Option(7, "--days", "-d", help="За сколько последних дней"),
    standing: int = typer.Option(3, "--standing", help="Со скольких повторов находка "
                                                       "считается застарелой"),
    db: Path = typer.Option(storage.DEFAULT_DB, "--db"),
) -> None:
    """Сводка по накопленной истории."""
    since = datetime.now() - timedelta(days=days)
    with storage.session(db) as conn:
        counts = storage.summary_since(conn, since)
        runs = [r for r in storage.runs_since(conn, since) if r["trustworthy"]]
        reporting.render_summary(counts, since, len(runs), console)

        old = storage.standing_gaps(conn, standing)
        if old:
            console.print(f"\n[bold]Застарелые находки[/bold] (повторились ≥{standing} раз) — "
                          f"{len(old)}; это системные дыры, а не свежие регрессии:")
            for row in old[:25]:
                console.print(f"  • [dim]{row['scenario_key']}[/dim] — {row['gap_key']} "
                              f"(с {row['first_seen'][:10]}, раз: {row['times_seen']})")
            if len(old) > 25:
                console.print(f"  [dim]…и ещё {len(old) - 25}[/dim]")


if __name__ == "__main__":
    app()


async def _fill_links(db: Path) -> tuple[int, int]:
    """Проставить прогонам ссылку на витрину, а находкам — id отеля на ней.

    Данных для этого достаточно и задним числом: параметры прогона сохранены целиком, а
    имя отеля в находке — это имя С ВИТРИНЫ, значит оно ищется в её же справочнике.
    Возвращает (сколько прогонов, сколько находок).
    """
    import json

    import httpx

    from pegasgap.providers import get_provider, load_providers
    from pegasgap.providers.tourvisor_api import _find_id, _items

    load_providers()
    factory = get_provider("tourvisor_api")
    provider = factory() if isinstance(factory, type) else factory

    runs_done = gaps_done = 0
    async with httpx.AsyncClient(timeout=90) as client:
        lists = await provider._reference(client)
        cities = _items(lists, "departures", "departure")
        countries = _items(lists, "allcountry", "country")
        # Словарь отелей страны тянется по два мегабайта, поэтому строго по одному разу.
        by_country: dict[int, dict[str, int]] = {}

        with storage.session(db) as conn:
            rows = conn.execute(
                "SELECT id, params_json, operator FROM runs WHERE reference_url IS NULL"
            ).fetchall()
            for row in rows:
                params = json.loads(row["params_json"])
                city_id = _find_id(cities, params["departure_city"])
                country_id = _find_id(countries, params["destination_country"])
                if not city_id or not country_id:
                    continue
                operator_id = provider._operator_id(lists, row["operator"])
                url = provider._page_url(
                    SearchParams(**params), city_id, country_id, operator_id)
                conn.execute("UPDATE runs SET reference_url = ? WHERE id = ?",
                             (url, row["id"]))
                runs_done += 1

                if country_id not in by_country:
                    hotels = await provider._hotels(client, country_id)
                    by_country[country_id] = {
                        str(h.get("name", "")).strip().casefold(): hid
                        for hid, h in hotels.items() if h.get("name")}
                index = by_country[country_id]
                for gap in conn.execute(
                        "SELECT id, hotel_name FROM gaps "
                        "WHERE run_id = ? AND reference_hotel_id IS NULL", (row["id"],)):
                    hid = index.get(str(gap["hotel_name"]).strip().casefold())
                    if hid:
                        conn.execute("UPDATE gaps SET reference_hotel_id = ? WHERE id = ?",
                                     (hid, gap["id"]))
                        gaps_done += 1
    return runs_done, gaps_done


@app.command("fill-links")
def fill_links(
    db: Path = typer.Option(storage.DEFAULT_DB, "--db", help="Файл базы"),
) -> None:
    """Дозаполнить ссылки на витрину в уже сохранённых прогонах.

    Нужна один раз после появления самих ссылок: прогоны, сделанные раньше, о них не
    знают, а перепроверятся они нескоро — и до тех пор треть отчёта осталась бы без
    ссылки без всякой на то причины.
    """
    runs, gaps = asyncio.run(_fill_links(db))
    console.print(f"[bold]Готово:[/bold] прогонов {runs}, находок {gaps}")
