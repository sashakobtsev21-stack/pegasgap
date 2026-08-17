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
from datetime import date, datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console

from pegasgap import report as reporting
from pegasgap import storage
from pegasgap.catalog import fetch_catalog, resolve_country_id
from pegasgap.diagnosis import diagnose
from pegasgap.gaps import detect
from pegasgap.linking import load_links
from pegasgap.logging_setup import configure_logging
from pegasgap.models import PEGAS, GapKind, ScanResult, SearchParams
from pegasgap.orchestrator import CHECKED, REFERENCE, run_pair
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


async def _run_many(items: list[SearchParams], operator: str, headless: bool,
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
            scan = await _run_one(p, operator, headless)
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
    console.print(f"[bold]Обход:[/bold] {len(items)} сценариев, оператор {matrix.operator}, "
                  f"параллельно {jobs}")
    scans = asyncio.run(_run_many(items, matrix.operator, headless, jobs))
    _persist_and_show(scans, db, csv_path, html_path)


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
