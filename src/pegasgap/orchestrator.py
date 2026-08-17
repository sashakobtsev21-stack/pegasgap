"""Оркестрация: параллельный прогон обеих площадок по одному запросу.

Обе площадки идут одновременно и в одном временном окне — это не оптимизация, а
требование корректности: цены живые, и последовательный прогон сравнивал бы выдачу
Турвизора с выдачей Слетать, снятой на несколько минут позже.
"""

from __future__ import annotations

import asyncio
import logging
import os

from pegasgap.models import (
    NotApplicableError,
    ProviderResult,
    SearchParams,
    is_not_applicable_error,
)
from pegasgap.providers import (
    checked_provider_name,
    get_provider,
    load_providers,
    reference_provider_name,
)

log = logging.getLogger("pegasgap.orchestrator")

REFERENCE = "tourvisor"  # эталон: что у оператора вообще есть на рынке
CHECKED = "sletat"       # роль проверяемой стороны в отчёте (чем её читать — см. ниже)


async def run_pair(
    params: SearchParams,
    headless: bool = True,
    provider_timeout_s: float | None = None,
    provider_retries: int | None = None,
) -> dict[str, ProviderResult]:
    """Прогнать обе площадки параллельно. Возвращает {имя площадки: результат}.

    Падение одной площадки не валит прогон: любой сбой превращается в
    `ProviderResult(success=False)` с текстом ошибки.

    Сверху — жёсткий таймаут на площадку (`PEGASGAP_PROVIDER_TIMEOUT_S`, по умолчанию
    180 с): если внутренние таймауты не сработали (зависший браузер), `search()`
    отменяется и не подвешивает прогон.

    При СЛУЧАЙНОМ сбое — до `PEGASGAP_PROVIDER_RETRIES` повторов (по умолчанию 1):
    сетевая икота обычно проходит со второй попытки. НЕ повторяем успех, верхний таймаут
    (площадка зависла — второй такой же впустую) и детерминированные отказы
    «не обслуживает такой запрос» — повтор дал бы тот же ответ.
    """
    timeout_s = (provider_timeout_s if provider_timeout_s is not None
                 else float(os.environ.get("PEGASGAP_PROVIDER_TIMEOUT_S") or 180))
    retries = max(0, provider_retries if provider_retries is not None
                  else int(os.environ.get("PEGASGAP_PROVIDER_RETRIES") or 1))

    load_providers()
    # Ключи словаря — РОЛИ (эталон / проверяемая), а не способ чтения: классификации
    # безразлично, пришла выдача Слетать из шлюза или из браузера.
    sources = {
        REFERENCE: reference_provider_name(params.search_mode),
        CHECKED: checked_provider_name(),
    }
    instances = {role: get_provider(src)(headless=headless) for role, src in sources.items()}

    log.info("прогон: %s → %s, режим %s, %s..%s, оператор %s (эталон: %s, проверяемая: %s)",
             params.departure_city, params.destination_country, params.search_mode,
             params.date_from, params.date_to, ", ".join(params.operators) or "все",
             sources[REFERENCE], sources[CHECKED])

    async def _attempt(name: str, inst) -> tuple[ProviderResult, bool, bool]:
        """Одна попытка под жёстким таймаутом → (результат, был_таймаут, детерм_отказ)."""
        try:
            res = await asyncio.wait_for(inst.search(params), timeout=timeout_s)
            return res, False, False
        except TimeoutError:
            log.warning("%s: превышен таймаут %.0fс — площадка прервана", name, timeout_s)
            return ProviderResult(
                provider=name, success=False, duration_seconds=timeout_s,
                search_mode=params.search_mode,
                error=f"Превышен таймаут {timeout_s:.0f}с — площадка прервана оркестратором",
            ), True, False
        except NotApplicableError as exc:
            # Не сбой: площадка детерминированно не обслуживает такой запрос. Для задачи
            # пропусков это важно отдельно — отказ ЭТАЛОНА означает, что сравнивать не с
            # чем, и пропуск проверяемой стороне засчитывать нельзя.
            log.info("%s: не обслуживает запрос — %s", name, exc)
            return ProviderResult(
                provider=name, success=False, duration_seconds=0.0,
                search_mode=params.search_mode, error=str(exc)), False, True
        except Exception as exc:
            log.warning("%s упал: %s: %s", name, type(exc).__name__, exc)
            return ProviderResult(
                provider=name, success=False, duration_seconds=0.0,
                search_mode=params.search_mode, error=f"{type(exc).__name__}: {exc}"), False, False

    async def _run_one(name: str) -> ProviderResult:
        inst = instances[name]
        res, timed_out, not_applicable = await _attempt(name, inst)
        left = retries
        while (left > 0 and not res.success and not timed_out and not not_applicable
               and not is_not_applicable_error(res.error)):
            left -= 1
            log.info("%s: случайный сбой (%s) — повтор (осталось %d)", name, res.error, left)
            res, timed_out, not_applicable = await _attempt(name, inst)
        if res.success:
            log.info("%s: OK %.1fс, офферов %d, отелей %d",
                     name, res.duration_seconds, len(res.offers), len(res.hotel_offers))
        else:
            log.warning("%s: FAIL %.1fс — %s", name, res.duration_seconds, res.error)
        return res

    roles = list(sources)
    results = await asyncio.gather(*(_run_one(r) for r in roles))
    return dict(zip(roles, results, strict=True))
