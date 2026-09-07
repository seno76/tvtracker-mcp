"""Бэклог: что вышло, но не просмотрено.

Сердце проекта. Такого ответа нет ни в TVmaze, ни в локальной базе поодиночке — он
рождается на их пересечении: позиция из трекинга минус вышедшие по `airstamp` серии,
с отброшенными спецвыпусками и суммированием длительности.

Слой чистый: ни SQL, ни сети, ни `datetime.now()` внутри — «сейчас» приходит аргументом.
Только так граничные случаи (серия вышла сегодня, но по UTC ещё не наступила) вообще
поддаются проверке.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tvtracker.domain.episodes import EpisodeRef

# Спецвыпуски и «незначительные» серии в долг не идут: иначе бэклог требует досмотреть
# рождественский спешл, которого нет в сюжетной линии.
COUNTED_KINDS = frozenset({"regular"})


@dataclass(frozen=True)
class EpisodeRow:
    """Эпизод в том виде, в каком он приходит из хранилища."""

    season: int
    number: int
    name: str = ""
    airstamp: str | None = None
    runtime: int | None = None
    rating: float | None = None
    kind: str = "regular"

    @property
    def ref(self) -> EpisodeRef:
        return EpisodeRef(self.season, self.number)


@dataclass(frozen=True)
class ShowBacklog:
    """Долг по одному сериалу."""

    slug: str
    name: str
    episodes: list[EpisodeRow]
    minutes: int
    latest_airstamp: str | None

    @property
    def count(self) -> int:
        return len(self.episodes)

    @property
    def span(self) -> str:
        """`2x04 … 2x10` для длинного хвоста, перечисление — для короткого."""
        if not self.episodes:
            return ""
        codes = [str(episode.ref) for episode in self.episodes]
        return f"{codes[0]} … {codes[-1]}" if len(codes) > 2 else ", ".join(codes)


def parse_airstamp(value: str | None) -> datetime | None:
    """Разбирает метку UTC из TVmaze (`2026-09-05T14:00:00+00:00`).

    Именно `airstamp`, а не `airdate`: вопрос «вышло уже или нет» корректно решается
    только по UTC-метке, иначе на стриминге и азиатских релизах ошибка в сутки.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def has_aired(episode: EpisodeRow, now: datetime) -> bool:
    """Серия без даты выхода считается невышедшей: анонс — это ещё не эпизод."""
    aired_at = parse_airstamp(episode.airstamp)
    return aired_at is not None and aired_at <= now


def episode_minutes(episode: EpisodeRow, fallback: int | None) -> int:
    """Длительность серии; при `runtime = null` берётся `averageRuntime` сериала.

    Нули лучше, чем выдумка: если нет ни того ни другого, время просто не суммируется,
    и в ответе это видно по отсутствию оценки.
    """
    return episode.runtime or fallback or 0


def unwatched(
    episodes: list[EpisodeRow],
    position: EpisodeRef | None,
    now: datetime,
) -> list[EpisodeRow]:
    """Вышедшие серии строго после текущей позиции.

    Позиция `None` означает «не начинал»: тогда долг — весь вышедший сериал.
    Сортировка по (сезон, номер) обязательна: дейли-шоу нумеруются по годам
    (`season: 2026, number: 178`), и порядок из БД для них не гарантирован.
    """
    counted = sorted(
        (e for e in episodes if e.kind in COUNTED_KINDS and has_aired(e, now)),
        key=lambda e: (e.season, e.number),
    )
    if position is None:
        return counted
    return [e for e in counted if e.ref > position]


def build_show_backlog(
    slug: str,
    name: str,
    episodes: list[EpisodeRow],
    position: EpisodeRef | None,
    now: datetime,
    avg_runtime: int | None = None,
) -> ShowBacklog:
    """Считает долг по одному сериалу."""
    pending = unwatched(episodes, position, now)
    minutes = sum(episode_minutes(e, avg_runtime) for e in pending)
    latest = max((e.airstamp for e in pending if e.airstamp), default=None)
    return ShowBacklog(
        slug=slug, name=name, episodes=pending, minutes=minutes, latest_airstamp=latest
    )


def sort_by_freshness(backlogs: list[ShowBacklog]) -> list[ShowBacklog]:
    """Свежее — выше.

    Сериалы без долга уходят в конец, но из выдачи не исчезают: «сезон досмотрен,
    продления нет» — это ответ, а не пустота.
    """

    def key(backlog: ShowBacklog) -> tuple[bool, float]:
        aired_at = parse_airstamp(backlog.latest_airstamp)
        return (backlog.count == 0, -aired_at.timestamp() if aired_at else 0.0)

    return sorted(backlogs, key=key)


def fits_in(
    backlog: ShowBacklog, minutes: int, avg_runtime: int | None = None
) -> EpisodeRow | None:
    """Первая невиденная серия, которая укладывается в свободное время с запасом 10 %.

    Именно первая, а не самая короткая: смотреть сериал вразбивку никто не станет.
    """
    if not backlog.episodes:
        return None
    candidate = backlog.episodes[0]
    length = episode_minutes(candidate, avg_runtime)
    if length == 0:
        return candidate
    return candidate if length <= minutes * 1.1 else None
