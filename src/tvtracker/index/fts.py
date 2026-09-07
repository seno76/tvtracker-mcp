"""Поиск по корпусу: FTS5 и BM25.

Главная функция сервера — **поиск по тексту описания**: в TVmaze такого запроса нет
вообще, там ищут только по названию. Здесь он делается BM25-ранжированием по
`summary_clean`, `name` и жанрам.

Отдельная забота — экранирование. Строка пользователя попадает в MATCH-выражение, а у
FTS5 своя грамматика: кавычки, `*`, `NEAR`, `AND`/`OR`/`NOT`, скобки. Незакавыченный
ввод либо роняет запрос, либо тихо выполняет чужой оператор. Поэтому каждое слово
оборачивается в кавычки, а сам ввод разбирается на токены нами, а не SQLite.

В запросах ниже f-строкой подставляется только заранее собранный фрагмент `WHERE`
из фиксированных шаблонов с `?`. Значения фильтров всегда идут параметрами — отсюда
подавления `S608`: линтер видит f-строку в SQL, но подставляемого ввода в ней нет.

Справочник: https://www.sqlite.org/fts5.html
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from tvtracker.log import get_logger

logger = get_logger(__name__)

# Слово для FTS: буквы и цифры любого алфавита. Всё остальное — разделители,
# а значит и вся спецсинтаксис FTS5 в ввод пользователя не попадает.
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)

# Запрос-описание обычно длиннее и содержит служебные слова; запрос-название короткий.
# Порог в четыре слова взят по замерам на 20 размеченных запросах из evals.
_DESCRIPTION_WORDS = 4


@dataclass(frozen=True)
class SearchFilters:
    """Фильтры каталога. Значения приходят из справочника `catalog://facets`."""

    genre: str | None = None
    status: str | None = None
    language: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    min_rating: float | None = None


def escape_fts(query: str, match_all: bool = False) -> str:
    """Превращает произвольную строку в безопасное выражение FTS5.

    Каждое слово — отдельная фраза в кавычках; внутренние кавычки удваиваются, как того
    требует синтаксис строк FTS5. Пустой результат означает, что искать не по чему.

    По умолчанию слова объединяются через `OR`, а не через подразумеваемый `AND`.
    Разница принципиальная: запрос-описание — это пересказ своими словами, и требовать
    совпадения всех десяти слов значит не найти ничего. Отсев мусора делает BM25:
    редкие слова («surgically», «memories») весят несопоставимо больше служебных.
    """
    tokens = _TOKEN.findall(query)
    quoted = ['"{}"'.format(token.replace('"', '""')) for token in tokens]
    return " ".join(quoted) if match_all else " OR ".join(quoted)


def looks_like_description(query: str) -> bool:
    """Отличает фразу-описание от названия.

    Названия короткие («Severance», «The Bear»), описания — предложения («сериал про офис,
    где сотрудникам разделяют память»). Ошибка в эту сторону не фатальна: режим `auto`
    сначала пробует точное совпадение по названию и только потом идёт в корпус.
    """
    return len(_TOKEN.findall(query)) >= _DESCRIPTION_WORDS


def _filter_sql(filters: SearchFilters) -> tuple[str, list[object]]:
    """Собирает WHERE из фильтров. Значения всегда идут параметрами, не в текст SQL."""
    clauses: list[str] = []
    params: list[object] = []

    if filters.genre:
        # Жанры лежат JSON-массивом; сравниваем без учёта регистра по элементам массива.
        clauses.append(
            "EXISTS (SELECT 1 FROM json_each(s.genres_json) WHERE lower(value) = lower(?))"
        )
        params.append(filters.genre)
    if filters.status:
        clauses.append("lower(s.status) = lower(?)")
        params.append(filters.status)
    if filters.language:
        clauses.append("lower(s.language) = lower(?)")
        params.append(filters.language)
    if filters.year_from is not None:
        clauses.append("CAST(substr(s.premiered, 1, 4) AS INTEGER) >= ?")
        params.append(filters.year_from)
    if filters.year_to is not None:
        clauses.append("CAST(substr(s.premiered, 1, 4) AS INTEGER) <= ?")
        params.append(filters.year_to)
    if filters.min_rating is not None:
        clauses.append("s.rating >= ?")
        params.append(filters.min_rating)

    return (" AND " + " AND ".join(clauses) if clauses else ""), params


def search_by_title(
    conn: sqlite3.Connection,
    query: str,
    filters: SearchFilters,
    limit: int,
) -> tuple[list[sqlite3.Row], int]:
    """Поиск по названию: сначала точные и префиксные совпадения, потом остальные.

    Сортировка по `weight` (популярность у TVmaze) внутри одинаково точных совпадений —
    иначе «The Office» отдаёт малоизвестный ремейк вперёд оригинала.
    """
    where, params = _filter_sql(filters)
    like = f"{query.strip()}%"
    sql = f"""
        SELECT s.*,
               CASE WHEN lower(s.name) = lower(?) THEN 0
                    WHEN lower(s.name) LIKE lower(?) THEN 1
                    ELSE 2 END AS exactness
        FROM shows AS s
        WHERE (lower(s.name) = lower(?) OR lower(s.name) LIKE lower(?)){where}
        ORDER BY exactness, s.weight DESC NULLS LAST, s.rating DESC NULLS LAST
        LIMIT ?
    """  # noqa: S608
    head = [query.strip(), like, query.strip(), f"%{query.strip()}%"]
    rows = conn.execute(sql, [*head, *params, limit]).fetchall()
    total = _count_title_matches(conn, query, where, params)
    logger.debug("title search", extra={"hits": len(rows), "total": total})
    return rows, total


def _count_title_matches(
    conn: sqlite3.Connection, query: str, where: str, params: list[object]
) -> int:
    sql = f"""
        SELECT count(*) AS n FROM shows AS s
        WHERE (lower(s.name) = lower(?) OR lower(s.name) LIKE lower(?)){where}
    """  # noqa: S608
    row = conn.execute(sql, [query.strip(), f"%{query.strip()}%", *params]).fetchone()
    return int(row["n"])


def search_by_description(
    conn: sqlite3.Connection,
    query: str,
    filters: SearchFilters,
    limit: int,
) -> tuple[list[sqlite3.Row], int]:
    """Поиск по тексту описаний. То, ради чего сервер вообще существует.

    Веса BM25 подобраны так, чтобы совпадение в названии било совпадение в описании,
    но не отменяло его: человек, который помнит сюжет, а не название, всё равно должен
    получить нужный сериал в первой тройке.
    """
    match = escape_fts(query)
    if not match:
        return [], 0

    where, params = _filter_sql(filters)
    sql = f"""
        SELECT s.*, bm25(shows_fts, 8.0, 4.0, 2.0, 1.0) AS score
        FROM shows_fts
        JOIN shows AS s ON s.rowid = shows_fts.rowid
        WHERE shows_fts MATCH ?{where}
        ORDER BY score
        LIMIT ?
    """  # noqa: S608
    count_sql = f"""
        SELECT count(*) AS n
        FROM shows_fts JOIN shows AS s ON s.rowid = shows_fts.rowid
        WHERE shows_fts MATCH ?{where}
    """  # noqa: S608
    rows = conn.execute(sql, [match, *params, limit]).fetchall()
    total = int(conn.execute(count_sql, [match, *params]).fetchone()["n"])
    logger.debug("description search", extra={"hits": len(rows), "total": total})
    return rows, total


def browse(
    conn: sqlite3.Connection,
    filters: SearchFilters,
    limit: int,
) -> tuple[list[sqlite3.Row], int]:
    """Просмотр каталога по одним фильтрам, без поискового запроса.

    Третий сценарий `show_search`: «комедии до 30 минут, которые уже закончились» —
    здесь искать нечего, надо отобрать и отранжировать. Порядок — по рейтингу,
    при равенстве по популярности.
    """
    where, params = _filter_sql(filters)
    condition = where[len(" AND ") :] if where else "1 = 1"
    rows = conn.execute(
        f"""
        SELECT s.* FROM shows AS s
        WHERE {condition}
        ORDER BY s.rating DESC NULLS LAST, s.weight DESC NULLS LAST
        LIMIT ?
        """,  # noqa: S608
        [*params, limit],
    ).fetchall()
    total = int(
        conn.execute(
            f"SELECT count(*) AS n FROM shows AS s WHERE {condition}",  # noqa: S608
            params,
        ).fetchone()["n"]
    )
    logger.debug("catalog browse", extra={"hits": len(rows), "total": total})
    return rows, total


def nearest_titles(conn: sqlite3.Connection, query: str, limit: int = 3) -> list[str]:
    """Пара ближайших названий для сообщения о пустой выдаче.

    Ошибка должна чинить следующий вызов, а не констатировать неудачу: имена рядом —
    это то, что модель подставит во вторую попытку.
    """
    tokens = _TOKEN.findall(query)
    if not tokens:
        return []
    rows = conn.execute(
        """
        SELECT name FROM shows
        WHERE lower(name) LIKE lower(?)
        ORDER BY weight DESC NULLS LAST
        LIMIT ?
        """,
        (f"%{tokens[0]}%", limit),
    ).fetchall()
    return [str(row["name"]) for row in rows]
