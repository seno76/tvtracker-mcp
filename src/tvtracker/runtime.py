"""Держатель контекста приложения.

Инструменты получают зависимости через `ctx.request_context.lifespan_context`, но
**статическим ресурсам SDK контекст не отдаёт**: `Context` инжектируется только в
шаблонные ресурсы и в инструменты, а `tracking://backlog` — статический URI.

Поэтому lifespan кладёт собранный контекст сюда, а ресурсы читают его отсюда. Сервер
однопользовательский и однопроцессный, соединение с БД одно на всё время жизни —
глобальный держатель здесь честнее, чем шаблонный URI, придуманный ради инъекции.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

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
    """Текущий контекст приложения.

    Обращение до старта сервера — это ошибка сборки, а не ситуация времени выполнения,
    поэтому она падает явно, а не возвращает `None` для тихого падения ниже по стеку.
    """
    if _app is None:
        raise RuntimeError("Контекст приложения недоступен: сервер ещё не запущен.")
    return _app
