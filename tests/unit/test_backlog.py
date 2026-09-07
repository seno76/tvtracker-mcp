"""Бэклог: граничные случаи, перечисленные в дизайне поимённо.

Ни сети, ни БД, ни системного времени: «сейчас» приходит аргументом. Именно поэтому
случай «серия вышла сегодня, но по UTC ещё не наступила» вообще поддаётся проверке.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tvtracker.domain.backlog import (
    EpisodeRow,
    build_show_backlog,
    episode_minutes,
    fits_in,
    has_aired,
    sort_by_freshness,
    unwatched,
)
from tvtracker.domain.episodes import EpisodeRef

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def ep(
    season: int, number: int, *, airstamp: str | None = "2026-09-01T00:00:00+00:00", **kw: object
) -> EpisodeRow:
    return EpisodeRow(season=season, number=number, airstamp=airstamp, **kw)  # type: ignore[arg-type]


def test_unwatched_starts_strictly_after_the_position() -> None:
    episodes = [ep(1, 1), ep(1, 2), ep(1, 3)]

    pending = unwatched(episodes, EpisodeRef(1, 2), NOW)

    assert [e.ref.number for e in pending] == [3]


def test_never_started_show_owes_everything_aired() -> None:
    episodes = [ep(1, 1), ep(1, 2)]

    assert len(unwatched(episodes, None, NOW)) == 2


def test_unaired_episode_is_not_a_debt() -> None:
    """Анонс — это ещё не эпизод: в долг идут только вышедшие серии."""
    episodes = [ep(1, 1), ep(1, 2, airstamp="2026-12-01T00:00:00+00:00")]

    assert [e.ref.number for e in unwatched(episodes, None, NOW)] == [1]


def test_episode_airing_later_today_has_not_aired_yet() -> None:
    """Ровно тот случай, ради которого берётся `airstamp`, а не `airdate`."""
    later_today = ep(1, 1, airstamp="2026-09-08T20:00:00+00:00")

    assert has_aired(later_today, NOW) is False


def test_episode_without_airstamp_is_not_counted() -> None:
    assert has_aired(ep(1, 1, airstamp=None), NOW) is False


def test_specials_and_insignificant_are_excluded() -> None:
    """Иначе бэклог требует досмотреть рождественский спешл, которого нет в сюжете."""
    episodes = [ep(1, 1), ep(1, 2, kind="special"), ep(1, 3, kind="insignificant")]

    assert [e.ref.number for e in unwatched(episodes, None, NOW)] == [1]


def test_null_runtime_falls_back_to_average() -> None:
    assert episode_minutes(ep(1, 1, runtime=None), 45) == 45
    assert episode_minutes(ep(1, 1, runtime=52), 45) == 52


def test_missing_runtime_everywhere_counts_as_zero_not_as_a_guess() -> None:
    assert episode_minutes(ep(1, 1, runtime=None), None) == 0


def test_daily_show_numbering_by_year_sorts_correctly() -> None:
    """Дейли-шоу нумеруются по годам: `season: 2026, number: 178`."""
    episodes = [ep(2026, 178), ep(2026, 9), ep(2025, 300)]

    pending = unwatched(episodes, EpisodeRef(2026, 9), NOW)

    assert [(e.season, e.number) for e in pending] == [(2026, 178)]


def test_skipped_season_still_counted() -> None:
    """Пропущенный сезон — это долг, а не повод считать сериал досмотренным."""
    episodes = [ep(1, 1), ep(3, 1)]

    pending = unwatched(episodes, EpisodeRef(1, 1), NOW)

    assert [(e.season, e.number) for e in pending] == [(3, 1)]


def test_finished_show_has_no_debt() -> None:
    """Сериал закончился и долгов больше не будет — это ответ, а не пустота."""
    backlog = build_show_backlog(
        "shogun-2024", "Shogun", [ep(1, 1)], EpisodeRef(1, 1), NOW, avg_runtime=60
    )

    assert backlog.count == 0
    assert backlog.minutes == 0
    assert backlog.span == ""


def test_backlog_sums_minutes_and_reports_span() -> None:
    episodes = [ep(2, n, runtime=50) for n in range(4, 11)]

    backlog = build_show_backlog("severance-2022", "Severance", episodes, EpisodeRef(2, 3), NOW)

    assert backlog.count == 7
    assert backlog.minutes == 350
    assert backlog.span == "2x04 … 2x10"


def test_short_backlog_lists_episodes_instead_of_a_range() -> None:
    episodes = [ep(3, 9), ep(3, 10)]

    backlog = build_show_backlog("the-bear-2022", "The Bear", episodes, EpisodeRef(3, 8), NOW)

    assert backlog.span == "3x09, 3x10"


def test_freshness_sorting_puts_recent_first_and_empty_last() -> None:
    fresh = build_show_backlog(
        "a", "A", [ep(1, 1, airstamp="2026-09-05T00:00:00+00:00")], None, NOW
    )
    stale = build_show_backlog(
        "b", "B", [ep(1, 1, airstamp="2026-08-01T00:00:00+00:00")], None, NOW
    )
    done = build_show_backlog("c", "C", [ep(1, 1)], EpisodeRef(1, 1), NOW)

    assert [b.slug for b in sort_by_freshness([stale, done, fresh])] == ["a", "b", "c"]


def test_fits_in_allows_ten_percent_overrun() -> None:
    """Серия на 65 минут в «час свободен» укладывается: запас 10 % задан дизайном."""
    backlog = build_show_backlog("a", "A", [ep(1, 1, runtime=65)], None, NOW)

    assert fits_in(backlog, 60) is not None
    assert fits_in(backlog, 50) is None


def test_fits_in_returns_the_first_episode_not_the_shortest() -> None:
    """Смотреть сериал вразбивку никто не станет — предлагается следующая по порядку."""
    backlog = build_show_backlog("a", "A", [ep(1, 1, runtime=50), ep(1, 2, runtime=20)], None, NOW)

    chosen = fits_in(backlog, 60)

    assert chosen is not None
    assert chosen.number == 1


def test_fits_in_of_empty_backlog_is_none() -> None:
    backlog = build_show_backlog("a", "A", [], None, NOW)

    assert fits_in(backlog, 60) is None
