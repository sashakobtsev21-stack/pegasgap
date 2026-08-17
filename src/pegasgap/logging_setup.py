"""Конфигурация логирования: text (по умолчанию) или JSON (env opt-in) — P3, 2026-06.

JSON-формат включается переменной `PEGASGAP_LOG_FORMAT=json` и удобен в проде
(стэк ELK/Loki/Cloud Logging парсит поля без regexp по plain-text). Без env-переменной
поведение прежнее — человекочитаемый формат (как `logging.basicConfig` ставил раньше).

Реализовано без внешних зависимостей (свой простой JsonFormatter): добавлять
`python-json-logger` в deps только ради этого — лишнее.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import ClassVar


class JsonFormatter(logging.Formatter):
    """Один JSON-объект на строку: ts (epoch), level, logger, msg + любые `extra`-поля."""

    _RESERVED: ClassVar[set[str]] = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": round(time.time(), 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Любые поля из logger.info("...", extra={"key": val}) — кладём верхним уровнем
        for k, v in record.__dict__.items():
            if k in self._RESERVED or k.startswith("_"):
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except TypeError:
                payload[k] = repr(v)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO, *, fmt: str | None = None) -> None:
    """Идемпотентная настройка корневого логгера (семантика как у logging.basicConfig).

    Если на корневом логгере уже есть обработчики (тесты с caplog, повторный вызов
    create_app, embedded usage) — НЕ перетираем их. Это важно для тестового captura.
    Иначе ставим один stdout-handler с JSON-форматом (если PEGASGAP_LOG_FORMAT=json
    или передан fmt='json') или с человекочитаемым (по умолчанию).

    `fmt` — явная перегрузка env-переменной для тестов / embedded-конфигурации
    (allowed: 'text' | 'json'); если None — читаем `PEGASGAP_LOG_FORMAT`.
    """
    root = logging.getLogger()
    if root.handlers:                          # уже сконфигурировано — не трогаем
        if root.level == 0 or root.level > level:
            root.setLevel(level)
        return
    if fmt is None:
        fmt = (os.environ.get("PEGASGAP_LOG_FORMAT") or "text").strip().lower()
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
    root.addHandler(handler)
    root.setLevel(level)
