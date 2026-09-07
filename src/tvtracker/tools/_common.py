"""Общая обвязка инструментов."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context


def app(ctx: Context[Any]) -> Any:
    """Достаёт `AppContext` из lifespan.

    Одно место на весь пакет: если разложение зависимостей поменяется, менять придётся
    здесь, а не в пяти инструментах.
    """
    return ctx.request_context.lifespan_context
