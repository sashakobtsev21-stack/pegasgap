"""Провайдеры площадок.

Две роли: `tourvisor` — эталон (что у оператора есть на рынке), `sletat` — проверяемая
сторона. Роли задаёт вызывающий код, сами провайдеры о них не знают.

Проверяемую сторону можно читать двумя способами, и они взаимозаменяемы по протоколу
`SearchProvider`:

* `sletat_api` — JSON-шлюз `module.sletat.ru`. Основной путь: статус оператора приходит
  фактом (`IsError`/`IsTimeout`/`RowsCount`), имя оператора есть в каждой строке выдачи,
  ломаться нечему. Требует логин и пароль в окружении.
* `sletat` — браузер. Запасной путь, живёт без доступов, но зависит от вёрстки формы
  (и на момент написания уже расходится с ней: селектор выбора страны не находится).
"""

import os

from pegasgap.providers.base import (
    SearchProvider,
    get_provider,
    list_providers,
    register_provider,
)

__all__ = [
    "SearchProvider",
    "checked_provider_name",
    "get_provider",
    "list_providers",
    "load_providers",
    "register_provider",
]


def load_providers() -> None:
    """Импортировать провайдеры, чтобы они зарегистрировались.

    Вынесено в функцию, чтобы базовый импорт пакета (модели, матчинг, классификация,
    отчёты) не требовал ни Playwright, ни httpx — они в опциональных группах.
    """
    from pegasgap.providers import sletat, sletat_api, tourvisor  # noqa: F401


def checked_provider_name() -> str:
    """Чем читать проверяемую сторону: шлюзом или браузером.

    По умолчанию — шлюзом, если заданы доступы: он и точнее, и быстрее. Явный выбор через
    `PEGASGAP_SLETAT_SOURCE=api|web` перекрывает автоопределение; это нужно, чтобы можно
    было осознанно сверить один и тот же запрос обоими путями.
    """
    choice = (os.environ.get("PEGASGAP_SLETAT_SOURCE") or "").strip().lower()
    if choice in ("api", "web"):
        return "sletat_api" if choice == "api" else "sletat"
    has_creds = bool(os.environ.get("SLETAT_LOGIN") and os.environ.get("SLETAT_PASSWORD"))
    return "sletat_api" if has_creds else "sletat"
