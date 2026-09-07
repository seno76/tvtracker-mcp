"""Рейтинговый профиль по сезонам и выбор, что включить.

Две вещи, которых нет ни в одном клиенте TVmaze, хотя данные для них в API лежат:

* **профиль по сезонам** — у каждой серии есть `rating.average`, и по нему видно,
  где сериал раскачивается и где проседает. Формулировка «перетерпи до 3x05»
  невозможна без этой агрегации;
* **ранжирование бэклога** — решение «что включить», а не список кандидатов.

Слой чистый: ни SQL, ни `now()` внутри.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import mean

from tvtracker.domain.backlog import EpisodeRow, ShowBacklog, episode_minutes, fits_in

# Разница средних, начиная с которой сезоны считаются разными по качеству.
# Меньше 0.3 балла — шум выборки, а не тренд.
MEANINGFUL_GAP = 0.3


@dataclass(frozen=True)
class SeasonRating:
    season: int
    average: float
    episodes: int


def season_profile(episodes: list[EpisodeRow]) -> list[SeasonRating]:
    """Средняя оценка серий по сезонам. Сезоны без оценок выпадают, а не дают ноль."""
    by_season: dict[int, list[float]] = defaultdict(list)
    for episode in episodes:
        if episode.rating is not None:
            by_season[episode.season].append(episode.rating)

    return [
        SeasonRating(season=season, average=round(mean(values), 1), episodes=len(values))
        for season, values in sorted(by_season.items())
    ]


def trend(profile: list[SeasonRating]) -> str:
    """Куда идёт сериал: `растёт`, `проседает`, `ровно` или `мало данных`."""
    if len(profile) < 2:
        return "мало данных"
    gap = profile[-1].average - profile[0].average
    if gap >= MEANINGFUL_GAP:
        return "растёт"
    if gap <= -MEANINGFUL_GAP:
        return "проседает"
    return "ровно"


@dataclass(frozen=True)
class Suggestion:
    """Один вариант «что включить» вместе с обоснованием в одну строку."""

    slug: str
    name: str
    episode: EpisodeRow
    minutes: int
    reason: str


def _reason(backlog: ShowBacklog, minutes: int, taste_rating: int | None) -> str:
    bits = [f"{minutes} мин"]
    if backlog.count > 1:
        bits.append(f"накопилось {backlog.count}")
    if taste_rating is not None:
        bits.append(f"твоя оценка сериала {taste_rating}")
    return ", ".join(bits)


def choose(
    backlogs: list[ShowBacklog],
    minutes: int,
    runtimes: dict[str, int | None],
    ratings: dict[str, int | None],
    limit: int = 3,
) -> list[Suggestion]:
    """Выбирает 2–3 варианта под свободное время.

    Порядок ранжирования: сначала то, что уложится по времени; внутри — по своей оценке
    сериала, затем по размеру долга. Это решение, а не поиск: пользователь спросил
    «что включить», а не «покажи всё, что подходит».
    """
    suggestions: list[Suggestion] = []
    for backlog in backlogs:
        fallback = runtimes.get(backlog.slug)
        episode = fits_in(backlog, minutes, fallback)
        if episode is None:
            continue
        length = episode_minutes(episode, fallback)
        rating = ratings.get(backlog.slug)
        suggestions.append(
            Suggestion(
                slug=backlog.slug,
                name=backlog.name,
                episode=episode,
                minutes=length,
                reason=_reason(backlog, length, rating),
            )
        )

    suggestions.sort(key=lambda s: (-(ratings.get(s.slug) or 0), -s.minutes))
    return suggestions[:limit]
