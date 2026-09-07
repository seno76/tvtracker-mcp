"""Пять инструментов сервера.

Правило консолидации: инструмент — это сценарий, а не эндпоинт. Все изменения состояния
собраны в один `tracking_update` с enum-действием вместо шести отдельных инструментов,
а поиск по названию, по описанию и фильтрация каталога — это один `show_search`.

Общее для всех: на вход и выход идут slug и человекочитаемые строки, ответ — текст,
каждый сериал помечен своим состоянием отслеживания.
"""

from __future__ import annotations

from mcp.server import MCPServer

from tvtracker.tools import (
    episode_search,
    show_profile,
    show_search,
    tracking_update,
    watch_next,
)

__all__ = ["register_tools"]


def register_tools(server: MCPServer) -> None:
    """Регистрирует инструменты на сервере. Порядок — тот, в каком их читает человек."""
    show_search.register(server)
    show_profile.register(server)
    episode_search.register(server)
    tracking_update.register(server)
    watch_next.register(server)
