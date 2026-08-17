"""Отчёты: консоль, CSV, HTML.

Отчёт устроен вокруг того, как с ним будут работать: коллега берёт находку и идёт
разбираться. Поэтому порядок — по приоритету разбора, а не по алфавиту:

1. **Новые** находки впереди старых: свежий пропуск на вчера ещё работавшем направлении
   — вероятная регрессия, а трёхнедельный пропуск — известная дыра в справочниках.
2. Классы идут в порядке `GapKind`: сначала отказ целиком, потом отдельные отели, потом
   цены.
3. Если прогон недостоверен, это стоит **над** находками, а не сноской внизу: цифрами
   такого прогона пользоваться нельзя, и узнать об этом нужно до того, как их прочли.
"""

from __future__ import annotations

import csv
import html
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from pegasgap.models import GapKind, HotelDiagnosis, HotelGap, ScanResult

CSV_COLUMNS = [
    "run_at", "режим", "город вылета", "страна", "даты", "класс", "отель", "звёзды",
    "курорт", "цена эталон", "цена наша", "разница %", "новая",
    "причина", "id в справочнике", "что делать", "комментарий",
]


def _money(value) -> str:
    return f"{value:,.0f}".replace(",", " ") if value is not None else "—"


def _diff(gap: HotelGap) -> str:
    return f"{gap.diff_pct:+.1f}%" if gap.diff_pct is not None else "—"


def _cause(gap: HotelGap) -> str:
    """Разбор по справочникам — только там, где он есть смысл (отельные пропуски)."""
    if gap.kind is not GapKind.HOTEL or gap.diagnosis is HotelDiagnosis.UNKNOWN:
        return ""
    return gap.diagnosis.title


def render_scan(scan: ScanResult, new_keys: set[str] | None = None,
                console: Console | None = None) -> None:
    """Показать один прогон в консоли."""
    console = console or Console()
    new_keys = new_keys or set()
    p = scan.params

    console.print(
        f"\n[bold]{p.departure_city} → {p.destination_country}[/bold]  "
        f"{p.date_from:%d.%m} – {p.date_to:%d.%m}, {p.nights_min}–{p.nights_max} ноч., "
        f"режим «{'туры' if p.search_mode == 'tours' else 'отели'}», оператор {scan.operator}")
    console.print(
        f"эталон: [cyan]{scan.reference_status.value}[/cyan] ({scan.reference_hotels} отелей)   "
        f"наша выдача: [cyan]{scan.checked_status.value}[/cyan]   "
        f"сопоставлено: {scan.matched_hotels}"
        + (f"   систематический сдвиг цен: {scan.price_offset_pct:+.1f}%"
           if scan.price_offset_pct is not None else ""))

    # Недостоверность — над находками. Читатель должен узнать об этом раньше, чем
    # начнёт им верить, а не из сноски после таблицы.
    if not scan.trustworthy:
        console.print("\n[bold red]Прогон недостоверен — находки ниже использовать нельзя:[/bold red]")
        for problem in scan.problems:
            console.print(f"  [red]•[/red] {problem}")

    # Заметки — не сомнение в находках, а контекст для их чтения. Отдельным блоком и
    # спокойным цветом, чтобы не путались с настоящими проблемами.
    for note in scan.notes:
        console.print(f"[dim]ⓘ {note}[/dim]")

    if not scan.gaps:
        console.print("\n[green]Расхождений не найдено.[/green]")
    else:
        table = Table(show_lines=False, header_style="bold")
        table.add_column("")
        table.add_column("класс")
        table.add_column("отель")
        table.add_column("причина")
        table.add_column("эталон", justify="right")
        table.add_column("наша", justify="right")
        table.add_column("Δ", justify="right")
        table.add_column("комментарий", overflow="fold")
        for gap in sorted(scan.gaps, key=lambda g: (g.key() not in new_keys, g.kind)):
            is_new = gap.key() in new_keys
            table.add_row(
                "[yellow]new[/yellow]" if is_new else "",
                gap.kind.title,
                gap.hotel_name + (f" {gap.stars}*" if gap.stars else ""),
                _cause(gap),
                _money(gap.reference_price),
                _money(gap.checked_price),
                _diff(gap),
                gap.note,
            )
        console.print(table)

        # Подсказка по разбору — один раз на класс, а не в каждой строке.
        console.print()
        for kind in GapKind:
            found = scan.gaps_of(kind)
            if found:
                console.print(f"  [dim]{kind.title} ({len(found)}): {kind.hint}[/dim]")
        # Разбор по справочникам точнее общей подсказки класса: он говорит не «чаще всего
        # причина такая», а что именно с этим отелем и что с ним делать.
        causes = Counter(g.diagnosis for g in scan.gaps_of(GapKind.HOTEL)
                         if g.diagnosis is not HotelDiagnosis.UNKNOWN)
        for diagnosis, count in causes.most_common():
            console.print(f"  [dim]  └ {diagnosis.title} ({count}): {diagnosis.action}[/dim]")

    if scan.unmatched:
        console.print(f"\n[yellow]Требуют проверки — отели сопоставлены неуверенно "
                      f"({len(scan.unmatched)}), в пропуски не включены:[/yellow]")
        for line in scan.unmatched[:20]:
            console.print(f"  • {line}")
        if len(scan.unmatched) > 20:
            console.print(f"  [dim]…и ещё {len(scan.unmatched) - 20}[/dim]")


def render_summary(counts: dict[str, int], since: datetime, runs: int,
                   console: Console | None = None) -> None:
    """Сводка за период."""
    console = console or Console()
    console.print(f"\n[bold]Сводка с {since:%d.%m.%Y %H:%M}[/bold] — достоверных прогонов: {runs}")
    table = Table(header_style="bold")
    table.add_column("класс")
    table.add_column("находок", justify="right")
    table.add_column("куда смотреть", overflow="fold")
    for kind in GapKind:
        table.add_row(kind.title, str(counts.get(kind.value, 0)), kind.hint)
    console.print(table)


def write_csv(scans: Sequence[ScanResult], path: Path, new_keys: set[str] | None = None) -> Path:
    """Выгрузить находки в CSV (utf-8-sig — чтобы Excel не ломал кириллицу)."""
    new_keys = new_keys or set()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(CSV_COLUMNS)
        for scan in scans:
            p = scan.params
            for gap in scan.gaps:
                writer.writerow([
                    scan.run_at.isoformat(timespec="seconds"),
                    p.search_mode, p.departure_city, p.destination_country,
                    f"{p.date_from:%d.%m.%Y}–{p.date_to:%d.%m.%Y}",
                    gap.kind.title, gap.hotel_name, gap.stars or "", gap.resort or "",
                    gap.reference_price or "", gap.checked_price or "", _diff(gap),
                    "да" if gap.key() in new_keys else "",
                    _cause(gap), gap.catalog_id or "",
                    gap.diagnosis.action if _cause(gap) else "",
                    gap.note,
                ])
    return path


def write_html(scans: Sequence[ScanResult], path: Path,
               new_keys: set[str] | None = None) -> Path:
    """Один самодостаточный HTML-файл — чтобы отчёт можно было просто переслать."""
    new_keys = new_keys or set()
    path.parent.mkdir(parents=True, exist_ok=True)
    esc = html.escape
    parts = [
        "<!doctype html><meta charset='utf-8'>",
        "<title>Пропуски оператора</title>",
        "<style>",
        "body{font:14px/1.5 system-ui,sans-serif;margin:2rem;max-width:1200px}",
        "table{border-collapse:collapse;width:100%;margin:1rem 0}",
        "th,td{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;vertical-align:top}",
        "th{background:#f4f4f4}",
        ".new{background:#fff8d5}.bad{color:#b00;font-weight:600}",
        "h2{margin-top:2rem}.meta{color:#555}",
        "</style>",
        f"<h1>Пропуски оператора</h1><p class='meta'>Сформирован "
        f"{datetime.now():%d.%m.%Y %H:%M}</p>",
    ]
    for scan in scans:
        p = scan.params
        parts.append(
            f"<h2>{esc(p.departure_city)} → {esc(p.destination_country)}</h2>"
            f"<p class='meta'>{p.date_from:%d.%m.%Y}–{p.date_to:%d.%m.%Y}, "
            f"{p.nights_min}–{p.nights_max} ноч., режим "
            f"{'туры' if p.search_mode == 'tours' else 'отели'}, оператор {esc(scan.operator)}</p>")
        if not scan.trustworthy:
            problems = "".join(f"<li>{esc(x)}</li>" for x in scan.problems)
            parts.append(f"<p class='bad'>Прогон недостоверен — находки использовать нельзя:</p>"
                         f"<ul class='bad'>{problems}</ul>")
        if scan.notes:
            notes = "".join(f"<li>{esc(x)}</li>" for x in scan.notes)
            parts.append(f"<ul class='meta'>{notes}</ul>")
        if not scan.gaps:
            parts.append("<p>Расхождений не найдено.</p>")
            continue
        parts.append("<table><tr><th></th><th>Класс</th><th>Отель</th><th>Причина</th>"
                     "<th>Эталон</th><th>Наша</th><th>Δ</th><th>Комментарий</th></tr>")
        for gap in sorted(scan.gaps, key=lambda g: (g.key() not in new_keys, g.kind)):
            is_new = gap.key() in new_keys
            stars = f" {gap.stars}*" if gap.stars else ""
            parts.append(
                f"<tr class='{'new' if is_new else ''}'>"
                f"<td>{'новое' if is_new else ''}</td><td>{esc(gap.kind.title)}</td>"
                f"<td>{esc(gap.hotel_name)}{stars}</td><td>{esc(_cause(gap))}</td>"
                f"<td>{_money(gap.reference_price)}</td>"
                f"<td>{_money(gap.checked_price)}</td><td>{_diff(gap)}</td>"
                f"<td>{esc(gap.note)}</td></tr>")
        parts.append("</table>")
        if scan.unmatched:
            items = "".join(f"<li>{esc(x)}</li>" for x in scan.unmatched)
            parts.append(f"<p class='meta'>Сопоставлены неуверенно, в пропуски не включены "
                         f"({len(scan.unmatched)}):</p><ul class='meta'>{items}</ul>")
    path.write_text("".join(parts), encoding="utf-8")
    return path
