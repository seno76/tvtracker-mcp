"""Точка входа MCP-сервера.

Здесь только регистрация примитивов и подключение зависимостей — ни одной строчки
доменной логики. Смысл в том, что весь смысл сервера можно вынуть отсюда и запустить
как CLI или HTTP-API, не тронув ни `domain/`, ни `index/`.

Порядок в этом файле важен: `configure_logging()` вызывается **до** создания
`MCPServer`, потому что сервер сам зовёт `logging.basicConfig(...)`, а тот не трогает
уже существующие обработчики. Кто настроил первым, тот и выиграл.

SDK: https://py.sdk.modelcontextprotocol.io/
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server import MCPServer

from tvtracker.config import Settings, load_settings
from tvtracker.log import configure_logging, get_logger
from tvtracker.runtime import use_app
from tvtracker.storage import repo as repo_module
from tvtracker.storage.repo import Repo
from tvtracker.tvmaze.client import TVmazeClient

logger = get_logger(__name__)


class AppContext:
    """Всё, что живёт дольше одного вызова. Доступно в каждом обработчике.

    Обычный класс, а не `@dataclass`: `mcp dev` импортирует `server.py` в обход
    `sys.modules`, и `dataclasses` не может разрешить отложенные аннотации у класса,
    чей модуль там не зарегистрирован. Три поля не стоят несобираемой точки входа.
    """

    __slots__ = ("client", "repo", "settings")

    def __init__(self, settings: Settings, repo: Repo, client: TVmazeClient) -> None:
        self.settings = settings
        self.repo = repo
        self.client = client


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
    """Открывает БД и http-клиент один раз на весь срок жизни сервера."""
    settings = load_settings()
    conn = repo_module.connect(settings.db_path)
    client = TVmazeClient(settings)
    logger.info(
        "server ready",
        extra={"db": str(settings.db_path), "shows": Repo(conn).count_shows()},
    )
    try:
        with use_app(AppContext(settings=settings, repo=Repo(conn), client=client)) as app:
            yield app
    finally:
        await client.aclose()
        conn.close()
        logger.info("server stopped")


def build_server(settings: Settings | None = None) -> MCPServer:
    """Собирает сервер. Отдельная функция, чтобы тесты поднимали его на своей БД."""
    settings = settings or load_settings()
    configure_logging(settings.log_level, settings.log_format, settings.log_file)

    server = MCPServer("tvtracker", lifespan=lifespan)

    from tvtracker import prompts, resources
    from tvtracker.tools import register_tools

    register_tools(server)
    resources.register_resources(server)
    prompts.register_prompts(server)
    return server


def build_test_server(conn: sqlite3.Connection, settings: Settings) -> MCPServer:
    """Сервер поверх готового соединения — для тестов и `mcp dev` на временной базе."""

    @asynccontextmanager
    async def fixed_lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
        client = TVmazeClient(settings)
        try:
            with use_app(AppContext(settings=settings, repo=Repo(conn), client=client)) as app:
                yield app
        finally:
            await client.aclose()

    server = MCPServer("tvtracker", lifespan=fixed_lifespan)

    from tvtracker import prompts, resources
    from tvtracker.tools import register_tools

    register_tools(server)
    resources.register_resources(server)
    prompts.register_prompts(server)
    return server


mcp = build_server()


if __name__ == "__main__":
    mcp.run()
