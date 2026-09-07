"""Профиль вкуса: агрегат по истории просмотра.

Основание любой рекомендации. Считается по локальному трекингу, поэтому в TVmaze такого
нет в принципе. Слой чистый: на вход — список записей, на выход — структура.

Честная оговорка зашита в саму структуру: `rated_count` выносится наружу и печатается
в ответе. Без него модель выдаёт статистику из четырёх сериалов за устойчивый вкус.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean

# Ниже трёх оценок средняя по жанру — это не профиль, а случайность.
MIN_RATINGS_PER_GENRE = 2
LIKED_THRESHOLD = 7.5


@dataclass(frozen=True)
class TrackedShow:
    """Одна запись трекинга вместе с метаданными сериала."""

    slug: str
    name: str
    state: str
    genres: list[str] = field(default_factory=list)
    rating: int | None = None
    avg_runtime: int | None = None
    network: str | None = None
    language: str | None = None


@dataclass(frozen=True)
class GenreTaste:
    genre: str
    average: float
    count: int


@dataclass(frozen=True)
class TasteProfile:
    """То, что уходит в ресурс `tracking://taste`."""

    total: int
    rated_count: int
    liked: list[GenreTaste]
    cool: list[GenreTaste]
    completion_rate: float | None
    preferred_runtime: tuple[int, int] | None
    networks: list[tuple[str, int]]
    languages: list[tuple[str, int]]
    recent_ratings: list[tuple[str, int, str]]


def _genre_tastes(shows: list[TrackedShow]) -> list[GenreTaste]:
    by_genre: dict[str, list[int]] = defaultdict(list)
    for show in shows:
        if show.rating is None:
            continue
        for genre in show.genres:
            by_genre[genre].append(show.rating)

    tastes = [
        GenreTaste(genre=genre, average=round(mean(ratings), 1), count=len(ratings))
        for genre, ratings in by_genre.items()
        if len(ratings) >= MIN_RATINGS_PER_GENRE
    ]
    return sorted(tastes, key=lambda t: (-t.average, -t.count, t.genre))


def _top_counts(values: list[str | None], limit: int = 3) -> list[tuple[str, int]]:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        if value:
            counts[value] += 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]


def _preferred_runtime(shows: list[TrackedShow]) -> tuple[int, int] | None:
    """Диапазон длительности у сериалов, которые понравились.

    Считается только по оценённым выше порога: длина брошенного ничего не говорит
    о предпочтениях, кроме того, что дело было не в ней.
    """
    runtimes = [
        show.avg_runtime
        for show in shows
        if show.avg_runtime and show.rating is not None and show.rating >= LIKED_THRESHOLD
    ]
    if len(runtimes) < MIN_RATINGS_PER_GENRE:
        return None
    return (min(runtimes), max(runtimes))


def build_profile(shows: list[TrackedShow]) -> TasteProfile:
    """Собирает профиль. Пустая история даёт пустой профиль, а не деление на ноль."""
    rated = [show for show in shows if show.rating is not None]
    tastes = _genre_tastes(shows)

    finished = sum(1 for show in shows if show.state == "finished")
    dropped = sum(1 for show in shows if show.state == "dropped")
    closed = finished + dropped
    completion = round(finished / closed, 2) if closed else None

    recent = [(show.name, show.rating, show.state) for show in rated if show.rating is not None][:6]

    return TasteProfile(
        total=len(shows),
        rated_count=len(rated),
        liked=[t for t in tastes if t.average >= LIKED_THRESHOLD],
        cool=[t for t in tastes if t.average < LIKED_THRESHOLD],
        completion_rate=completion,
        preferred_runtime=_preferred_runtime(shows),
        networks=_top_counts([show.network for show in shows]),
        languages=_top_counts([show.language for show in shows], limit=4),
        recent_ratings=recent,
    )
