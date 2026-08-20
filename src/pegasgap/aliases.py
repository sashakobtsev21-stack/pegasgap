"""Словарь синонимов названий отелей — подтверждённые вручную пары.

Матчер сводит написания автоматически, но упирается в случаи, где угадать нельзя, а
человеку видно сразу: «Erboy» и «ERBOY HOTEL SIRKECI» (приписка района), «Azure Villas
By Cornelia» и «CORNELIA AZURE VILLAS» (порядок слов), «Aqua Fantasy» с разной
звёздностью у площадок. Такие пары рождаются в колонке «возможно, другое имя»: человек
сверил — и фиксирует вывод строкой в `hotel_aliases.yaml`, чтобы инструмент больше
никогда не разводил эти написания.

Формат файла — группы синонимов, по одной на строку:

    - [Erboy, ERBOY HOTEL SIRKECI]

В группе может быть и больше двух написаний. Сюда попадают только СВЕРЕННЫЕ пары:
словарь сильнее любого правила матчинга, и ошибка в нём молча склеит два разных отеля.

Модуль отвечает только за чтение файла; что означает совпадение по словарю — решает
`matching`, иначе получился бы цикл импортов (нормализация имён живёт в matching).
Файл перечитывается по mtime, поэтому правка применяется без перезапуска — при
следующем прогоне.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

log = logging.getLogger("pegasgap.aliases")

DEFAULT_PATH = Path(os.environ.get("PEGASGAP_HOTEL_ALIASES") or "hotel_aliases.yaml")

# (mtime, группы). mtime -1 — файл не читали или его нет.
_CACHE: tuple[float, list[list[str]]] = (-1.0, [])


def raw_groups(path: Path | None = None) -> list[list[str]]:
    """Группы синонимов как в файле. Нет файла — пустой список, это не ошибка.

    Кеш по mtime: `stat` на вызов дёшев (вызовы идут по одному на прогон, не на
    сравнение), а правка файла подхватывается без перезапуска сервера.
    """
    global _CACHE
    target = path or DEFAULT_PATH
    try:
        mtime = target.stat().st_mtime
    except OSError:
        _CACHE = (-1.0, [])
        return []
    if mtime == _CACHE[0]:
        return _CACHE[1]

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError) as exc:
        # Битый файл не должен молча выключить словарь: оставляем прежние группы.
        log.warning("словарь синонимов не прочитан (%s) — работаю на прежнем",
                    type(exc).__name__)
        return _CACHE[1]

    groups = [
        [str(name).strip() for name in group if str(name).strip()]
        for group in raw
        if isinstance(group, list)
    ]
    groups = [g for g in groups if len(g) >= 2]
    _CACHE = (mtime, groups)
    log.info("словарь синонимов: групп %d (%s)", len(groups), target)
    return groups
