from __future__ import annotations

from datetime import datetime
from pathlib import Path
from random import choices
from time import sleep
from typing import Optional

from yaml import safe_load

from .logging import get_logger
from .songs import (
    MAX_DEQUEUE_DISTANCE,
    MAX_RECENCY_DISTANCE,
    Song,
    SongHandler,
    dequeue_distance,
    recency_distance,
)
from .weather import (
    MAX_TIME_DIST,
    MAX_WEATHER_DIST,
    WeatherHandler,
    get_time,
    get_weather,
    time_distance,
    weather_distance,
)

LOG = get_logger("orchestrator")


class config:
    def __init__(
        self, weather_api_key: str, location: dict[str, Optional[str]]
    ) -> None:
        self.api_key = weather_api_key
        self.country = location.get("country")
        self.city = location.get("city")

        self.citycountry = (self.city is not None) and (self.country is not None)

        if not self.citycountry:
            raise ValueError("You must define a complete location pair (city/country)")


def read_config() -> config:
    cfg = Path("config.yaml")
    if not cfg.exists():
        raise FileNotFoundError("Could not find the configuration file")

    with cfg.open() as f:
        if data := safe_load(f):
            return config(**data)

    raise ValueError("Could not read configuration file")


def update_config(WH: WeatherHandler) -> None:
    cfg = read_config()
    WH.api_key = cfg.api_key
    if cfg.city and cfg.country:
        WH.update_latlon(city=cfg.city, country=cfg.country)


def scale_scores(weather: float, time: float, recency: float, dequeued: float) -> float:
    return weather + time + recency * 0.1 + dequeued * 0.05


MAX_SCORE = scale_scores(
    MAX_WEATHER_DIST, MAX_TIME_DIST, MAX_RECENCY_DISTANCE, MAX_DEQUEUE_DISTANCE
)


def sort_songs(SH: SongHandler, WH: WeatherHandler) -> dict[float, list[Song]]:
    weather = WH.weather
    LOG.debug(
        f"ranking songs for weather={weather.weather.name} time={weather.time.name}"
    )
    now = datetime.now()

    songs = SH.all_songs

    if all_valid := SH.valid_songs(
        weather=weather.weather.name, time=weather.time.name
    ):
        songs = all_valid
        LOG.debug(f"found {len(songs)} valid songs")
    elif weather_valid := SH.valid_songs(weather=weather.weather.name):
        songs = weather_valid
        LOG.warning(
            f"some songs have the current time banned, fallback to "
            f"{len(songs)} songs valid for the current weather."
        )
    elif not songs:
        LOG.warning("No songs found, add some to the database and try again.")
        return {}
    else:
        LOG.warning(
            f"all {len(songs)} songs have both the weather and time banned, "
            "fallback to all."
        )

    # remove any songs already in the queue, we can't have duplicate files
    queue = SH.queue_songs
    unqueued_songs = [song for song in songs if song.filepath.name not in queue]

    if not unqueued_songs:
        LOG.error("No songs are valid to be played")
        return {}

    LOG.debug(f"scoring {len(unqueued_songs)} candidate songs")

    ranking: dict[float, list[Song]] = {}
    for song in unqueued_songs:
        weather_score = (
            min(
                weather_distance(weather.weather, get_weather(pref))
                for pref in song.weather.prefer
            )
            if song.weather.prefer
            else MAX_WEATHER_DIST
        )

        time_score = (
            min(
                time_distance(weather.time, get_time(pref)) for pref in song.time.prefer
            )
            if song.time.prefer
            else MAX_TIME_DIST
        )

        recency_score = (
            recency_distance(now, song.last_played) if song.last_played else 0.0
        )

        dequeue_score = (
            dequeue_distance(now, song.last_dequeued) if song.last_dequeued else 0.0
        )

        score = scale_scores(
            weather=weather_score,
            time=time_score,
            recency=recency_score,
            dequeued=dequeue_score,
        )

        if score in ranking:
            ranking[score].append(song)
        else:
            ranking[score] = [song]

        LOG.debug(
            f" - score {song.title}: weather={weather_score:.2f} "
            f"time={time_score:.2f} "
            f"recency={recency_score:.2f} "
            f"dequeued={dequeue_score:.2f} "
            f"total={score:.2f}"
        )

    LOG.debug(
        f"ranked {len(unqueued_songs)} with scores "
        f"range {min(ranking):.2f}-{max(ranking):.2f}"
    )

    return dict(sorted(ranking.items()))


def choose_song(
    ranking: dict[float, list[Song]], subset_length: Optional[int] = None
) -> Song:
    song_list: list[Song] = []
    weight_list: list[float] = []

    for _rank, songs in ranking.items():
        if subset_length and len(song_list) > subset_length:
            LOG.debug(
                f"subset length ({subset_length}) exceeded, "
                f"truncating valid range to {len(songs)} songs"
            )
            break
        song_list.extend(songs)
        weight_list.extend([max(0, MAX_SCORE - _rank)] * len(songs))

    LOG.debug(
        f"weighted pool size {len(song_list)} with weight range="
        f"{min(weight_list):.2f}-{max(weight_list):.2f}"
    )

    choice = choices(range(len(song_list)), weight_list, k=1)[0]
    song, weight = song_list[choice], weight_list[choice]
    LOG.debug(
        f"selected song={song} weight={weight}:.2f last_played={song.last_played}"
    )

    return song


def main():
    config = read_config()
    WH = WeatherHandler(api_key=config.api_key)
    SH = SongHandler(cache_size=100)
    queue_size = 5

    update_config(WH)

    LOG.debug(
        f"orchestrator initialising: cache size={SH.cache_size},"
        f" target queue={queue_size} location={WH.lat}/{WH.lon}"
    )

    LOG.info("orchestrator running")

    try:
        while True:
            LOG.debug(f"current queue {SH.queue_size} target {queue_size}")
            update_config(WH)

            if SH.queue_size < queue_size:
                LOG.info(
                    f"queue size ({SH.queue_size}) is smaller than the target "
                    f"({queue_size}), queuing a new song to play"
                )

                playable_songs = sort_songs(SH=SH, WH=WH)
                if not playable_songs:
                    sleep(60)
                    continue

                chosen_song = choose_song(ranking=playable_songs)
                LOG.debug(
                    f"queueing song: {chosen_song.title} (queue size={SH.queue_size})"
                )
                SH.play_song(chosen_song)

            sleep(5)
    except KeyboardInterrupt:
        WH.close()
        LOG.info("orchestrator shutdown")
    except Exception as e:
        LOG.exception(e, exc_info=True)
        WH.error()


if __name__ == "__main__":
    main()
