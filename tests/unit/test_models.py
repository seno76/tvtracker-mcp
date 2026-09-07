"""Модели источника: очистка, slug, классификация эпизодов.

Всё, что здесь проверяется, — это первая граница бюджета контекста. Если сюда просочится
`image` или `_links`, они окажутся в БД, а оттуда в контексте модели.
"""

from __future__ import annotations

import pytest

from tvtracker.tvmaze.models import Episode, Show, make_slug, strip_html, truncate

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<p>Employees <b>split</b> memories.</p>", "Employees split memories."),
        ("<p>Tom &amp; Jerry</p>", "Tom & Jerry"),
        ("<p>Line one</p>\n<p>Line two</p>", "Line one Line two"),
        (None, ""),
        ("", ""),
    ],
)
def test_strip_html(raw: str | None, expected: str) -> None:
    assert strip_html(raw) == expected


def test_truncate_cuts_on_word_boundary() -> None:
    assert truncate("один два три четыре", 11) == "один два…"


def test_truncate_leaves_short_text_alone() -> None:
    assert truncate("коротко", 200) == "коротко"


@pytest.mark.parametrize(
    ("name", "premiered", "expected"),
    [
        ("Severance", "2022-02-18", "severance-2022"),
        ("The Office", "2005-03-24", "the-office-2005"),
        ("Law & Order: SVU", "1999-09-20", "law-order-svu-1999"),
        ("Тьма", "2017-12-01", "тьма-2017"),  # нелатиница сохраняется, а не схлопывается
        ("!!!", "2020-01-01", "show-2020"),  # совсем без букв — единственный фолбэк
        ("Unaired", None, "unaired"),
    ],
)
def test_make_slug(name: str, premiered: str | None, expected: str) -> None:
    assert make_slug(name, premiered) == expected


def test_show_parse_drops_junk_and_flattens_nested_fields() -> None:
    show = Show.parse(
        {
            "id": 67,
            "name": "Severance",
            "premiered": "2022-02-18",
            "genres": ["Drama", "Science-Fiction"],
            "averageRuntime": 49,
            "rating": {"average": 7.6},
            "network": None,
            "webChannel": {"name": "Apple TV+", "officialSite": "https://example"},
            "summary": "<p>Memories are <b>split</b>.</p>",
            "image": {"medium": "https://example/x.jpg"},
            "_links": {"self": {"href": "https://example"}},
            "externals": {"imdb": "tt11280740"},
        }
    )

    assert show.slug == "severance-2022"
    assert show.rating == 7.6
    assert show.network == "Apple TV+"
    assert show.summary_clean == "Memories are split."
    assert not hasattr(show, "image")
    assert "image" not in show.model_dump()


def test_show_parse_survives_null_rating_and_missing_network() -> None:
    show = Show.parse({"id": 1, "name": "Nameless", "rating": {"average": None}})

    assert show.rating is None
    assert show.network is None
    assert show.avg_runtime is None


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        ({"season": 2, "number": 3, "type": "regular"}, "regular"),
        ({"season": 1, "number": None, "type": "significant_special"}, "special"),
        ({"season": 1, "number": 12, "type": "insignificant_special"}, "insignificant"),
        ({"season": 1, "number": 5}, "regular"),
    ],
)
def test_episode_kind_classification(payload: dict[str, object], kind: str) -> None:
    """Без `kind` бэклог требует досмотреть рождественский спешл, которого нет в сюжете."""
    assert Episode.parse(payload).kind == kind


def test_episode_code_is_human_readable() -> None:
    assert Episode.parse({"season": 2, "number": 3}).code == "2x03"
    assert Episode.parse({"season": 2026, "number": 178}).code == "2026x178"
    assert Episode.parse({"season": 1, "number": None}).code == "1xS"
