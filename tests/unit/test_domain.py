"""Домейн без бэклога: номера серий, рендер, профиль вкуса, вердикт по сезонам."""

from __future__ import annotations

import pytest

from tvtracker.domain.backlog import EpisodeRow, build_show_backlog
from tvtracker.domain.episodes import EpisodeCodeError, EpisodeRef, format_code, parse_code
from tvtracker.domain.format import ShowLine, no_results, render_results, render_show_line
from tvtracker.domain.taste import TrackedShow, build_profile
from tvtracker.domain.verdict import choose, season_profile, trend

pytestmark = pytest.mark.unit


# --- Номера серий -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [("2x03", (2, 3)), ("2X3", (2, 3)), (" 10x11 ", (10, 11)), ("2026x178", (2026, 178))],
)
def test_parse_code(code: str, expected: tuple[int, int]) -> None:
    assert parse_code(code) == EpisodeRef(*expected)


def test_cyrillic_x_is_accepted() -> None:
    """На русской раскладке «х» набирают чаще; отвергать это — тратить раунд на опечатку."""
    assert parse_code("2х03") == EpisodeRef(2, 3)


@pytest.mark.parametrize("bad", ["S02E03", "2-03", "", "две-три", "x03"])
def test_bad_code_message_shows_both_expected_and_given(bad: str) -> None:
    with pytest.raises(EpisodeCodeError) as excinfo:
        parse_code(bad)

    message = str(excinfo.value)
    assert 'Ожидаю "2x03"' in message
    assert f'"{bad}"' in message


def test_format_code_pads_the_number() -> None:
    assert format_code(2, 3) == "2x03"
    assert format_code(None, 3) is None


def test_episode_refs_compare_across_seasons() -> None:
    assert EpisodeRef(1, 10) < EpisodeRef(2, 1)


# --- Рендер -------------------------------------------------------------------------------


def test_show_line_has_six_fields_not_forty() -> None:
    line = render_show_line(
        ShowLine(
            slug="severance-2022",
            name="Severance",
            premiered="2022-02-18",
            status="Running",
            network="Apple TV+",
            genres=["Drama", "Science-Fiction"],
            avg_runtime=49,
            rating=7.6,
            summary="Сотрудникам разделяют память.",
            tracking_state="watching",
            tracking_position="2x03",
        )
    )

    assert line.splitlines() == [
        "Severance (2022–, Apple TV+, идёт) · Drama/Science-Fiction · 49 мин · 7.6",
        "Сотрудникам разделяют память.",
        "→ отслеживаю, остановился на 2x03",
    ]


def test_ended_show_shows_both_years() -> None:
    line = render_show_line(
        ShowLine(slug="a", name="A", premiered="2005-01-01", ended="2013-05-01")
    )

    assert "(2005–2013)" in line


def test_untracked_show_has_no_status_line() -> None:
    line = render_show_line(ShowLine(slug="a", name="A"))

    assert "→" not in line


def test_truncation_is_always_explained() -> None:
    """Без счётчика модель принимает первые восемь результатов за весь каталог."""
    shows = [ShowLine(slug=f"s{i}", name=f"Show {i}") for i in range(3)]

    body = render_results(shows, total=34, query="drama", hints=['genre="Drama"'])

    assert "Показаны 3 из 34." in body
    assert 'Сузить: genre="Drama".' in body


def test_full_result_has_no_counter() -> None:
    shows = [ShowLine(slug="a", name="A")]

    assert "Показаны" not in render_results(shows, total=1, query="a")


def test_empty_result_points_at_description_search() -> None:
    text = no_results("зззз", nearest=["Zoo", "Zorro"])

    assert "Zoo, Zorro" in text
    assert 'by="description"' in text


# --- Профиль вкуса ------------------------------------------------------------------------


def watched(
    name: str, genres: list[str], rating: int | None, state: str = "finished"
) -> TrackedShow:
    return TrackedShow(slug=name.lower(), name=name, state=state, genres=genres, rating=rating)


def test_genres_split_into_liked_and_cool() -> None:
    profile = build_profile(
        [
            watched("A", ["Mystery"], 9),
            watched("B", ["Mystery"], 8),
            watched("C", ["Reality"], 5),
            watched("D", ["Reality"], 6),
        ]
    )

    assert [t.genre for t in profile.liked] == ["Mystery"]
    assert [t.genre for t in profile.cool] == ["Reality"]


def test_single_rating_genre_is_not_a_taste() -> None:
    """Средняя по одной оценке — это не профиль, а случайность."""
    profile = build_profile([watched("A", ["Western"], 10), watched("B", ["Drama"], 8)])

    assert profile.liked == []


def test_completion_rate_counts_only_closed_shows() -> None:
    profile = build_profile(
        [
            watched("A", [], 8, state="finished"),
            watched("B", [], 5, state="dropped"),
            watched("C", [], None, state="watching"),
        ]
    )

    assert profile.completion_rate == 0.5


def test_empty_history_does_not_divide_by_zero() -> None:
    profile = build_profile([])

    assert profile.total == 0
    assert profile.completion_rate is None
    assert profile.preferred_runtime is None


def test_rated_count_is_exposed_for_the_honesty_caveat() -> None:
    """Без этой цифры модель выдаёт статистику из четырёх сериалов за устойчивый вкус."""
    profile = build_profile([watched("A", [], 9), watched("B", [], None)])

    assert (profile.total, profile.rated_count) == (2, 1)


# --- Вердикт ------------------------------------------------------------------------------


def rated(season: int, number: int, rating: float | None) -> EpisodeRow:
    return EpisodeRow(season=season, number=number, rating=rating)


def test_season_profile_averages_per_season() -> None:
    profile = season_profile([rated(1, 1, 8.0), rated(1, 2, 8.4), rated(2, 1, 6.0)])

    assert [(s.season, s.average, s.episodes) for s in profile] == [(1, 8.2, 2), (2, 6.0, 1)]


def test_seasons_without_ratings_are_skipped_not_zeroed() -> None:
    profile = season_profile([rated(1, 1, 8.0), rated(2, 1, None)])

    assert [s.season for s in profile] == [1]


@pytest.mark.parametrize(
    ("first", "last", "expected"),
    [(7.0, 8.5, "растёт"), (8.5, 7.0, "проседает"), (8.0, 8.1, "ровно")],
)
def test_trend_needs_a_meaningful_gap(first: float, last: float, expected: str) -> None:
    """Разница меньше 0.3 балла — шум выборки, а не тренд."""
    profile = season_profile([rated(1, 1, first), rated(2, 1, last)])

    assert trend(profile) == expected


def test_trend_of_a_single_season_is_honest_about_it() -> None:
    assert trend(season_profile([rated(1, 1, 8.0)])) == "мало данных"


def test_choose_ranks_by_own_rating_then_debt() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 9, 8, tzinfo=UTC)
    stamp = "2026-09-01T00:00:00+00:00"
    liked = build_show_backlog(
        "liked", "Liked", [EpisodeRow(1, 1, airstamp=stamp, runtime=50)], None, now
    )
    meh = build_show_backlog(
        "meh", "Meh", [EpisodeRow(1, 1, airstamp=stamp, runtime=50)], None, now
    )

    picks = choose([meh, liked], 60, {}, {"liked": 9, "meh": 5})

    assert [p.slug for p in picks] == ["liked", "meh"]
    assert "твоя оценка сериала 9" in picks[0].reason


def test_choose_drops_what_does_not_fit() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 9, 8, tzinfo=UTC)
    long_one = build_show_backlog(
        "long",
        "Long",
        [EpisodeRow(1, 1, airstamp="2026-09-01T00:00:00+00:00", runtime=90)],
        None,
        now,
    )

    assert choose([long_one], 30, {}, {}) == []
