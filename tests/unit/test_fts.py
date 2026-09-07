"""Экранирование и режимы FTS5 — граница между вводом пользователя и грамматикой SQLite."""

from __future__ import annotations

import pytest

from tvtracker.index.fts import escape_fts, looks_like_description

pytestmark = pytest.mark.unit


def test_words_are_joined_by_or_not_and() -> None:
    """Запрос-описание — пересказ своими словами; требовать все слова значит не найти ничего."""
    assert escape_fts("memories split work") == '"memories" OR "split" OR "work"'


def test_match_all_mode_keeps_implicit_and() -> None:
    assert escape_fts("memories split", match_all=True) == '"memories" "split"'


@pytest.mark.parametrize(
    "dangerous",
    ['foo" OR bar', "NEAR(a b)", "a AND b NOT c", "prefix*", "(group)", '"""'],
)
def test_fts_operators_never_survive_escaping(dangerous: str) -> None:
    """Спецсинтаксис FTS5 не должен выполняться: он превращается в обычные слова."""
    escaped = escape_fts(dangerous)

    assert "*" not in escaped
    assert "(" not in escaped
    assert " AND " not in escaped
    assert "NOT" not in escaped.replace('"NOT"', "")


def test_inner_quotes_are_doubled() -> None:
    assert escape_fts('say "hi"') == '"say" OR "hi"'


def test_empty_query_yields_nothing_to_search() -> None:
    assert escape_fts("!!! ???") == ""


@pytest.mark.parametrize(
    ("query", "is_description"),
    [
        ("Severance", False),
        ("The Bear", False),
        ("Breaking Bad", False),
        ("сериал про офис где сотрудникам разделяют память", True),
        ("employees have their memories split", True),
    ],
)
def test_auto_mode_heuristic(query: str, is_description: bool) -> None:
    assert looks_like_description(query) is is_description
