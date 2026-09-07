"""Как обработчики добираются до зависимостей.

Путей два, и это не небрежность, а следствие того, что умеет SDK.

**Инструменты** получают контекст параметром: `ctx.request_context.lifespan_context`.
Это штатный механизм, он переживёт переход на транспорт с несколькими сессиями.

**Статическим ресурсам** SDK контекст не отдаёт вовсе: `Context` инжектируется только
в инструменты и в шаблонные ресурсы, а `tracking://backlog` — статический URI. Поэтому
lifespan кладёт собранный контекст сюда, а ресурсы читают его отсюда.

Оба пути ведут к одному и тому же объекту и живут в одном модуле нарочно: если разложение
зависимостей поменяется, менять придётся здесь, а не в пяти инструментах и пяти ресурсах.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from mcp.server.mcpserver import Context

_app: Any = None


@contextmanager
def use_app(app: Any) -> Iterator[Any]:
    """Делает контекст доступным ресурсам на время работы сервера."""
    global _app
    previous, _app = _app, app
    try:
        yield app
    finally:
        _app = previous


def current_app() -> Any:
    """Контекст приложения для ресурсов.

    Обращение до старта сервера — ошибка сборки, а не ситуация времени выполнения,
    поэтому она падает явно, а не возвращает `None` для тихого падения ниже по стеку.
    """
    if _app is None:
        raise RuntimeError("Контекст приложения недоступен: сервер ещё не запущен.")
    return _app


def app_from(ctx: Context[Any]) -> Any:
    """Контекст приложения для инструментов — штатным путём SDK, из запроса."""
    return ctx.request_context.lifespan_context
