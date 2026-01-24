from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from time import sleep, time
from typing import Any, Generator, Optional, TypedDict

from filelock import FileLock, Timeout
from yaml import safe_dump, safe_load

from .logging import get_logger

LOG = get_logger("database")

CACHE = Path("src/cache")
QUEUE = CACHE / "queue"
RECENT = CACHE / "recent"
PINNED = CACHE / "pinned"
FALLBACK = CACHE / "fallback"
COVERS = CACHE / "covers"

for p in [QUEUE, RECENT, PINNED, FALLBACK, COVERS]:
    p.mkdir(parents=True, exist_ok=True)


class RuleScheme(TypedDict):
    prefer: Optional[list[str]]
    ban: Optional[list[str]]


class FallbackScheme(TypedDict):
    url: str
    title: str
    duration: float
    filepath: str
    artwork: str
    last_played: Optional[str]


class SongScheme(TypedDict):
    url: str
    title: str
    duration: float
    filepath: str
    artwork: str
    pinned: bool
    play_count: int
    weather: RuleScheme
    time: RuleScheme
    last_played: Optional[str]
    last_dequeued: Optional[str]


class DBScheme(TypedDict):
    fallback: list[FallbackScheme]
    songs: list[SongScheme]


@dataclass
class RuleSet:
    prefer: Optional[list[str]] = None
    ban: Optional[list[str]] = None

    @classmethod
    def from_dict(cls, data: RuleScheme) -> RuleSet:
        return RuleSet(
            prefer=data.get("prefer", None),
            ban=data.get("ban", None),
        )

    def to_dict(self) -> RuleScheme:
        return {"prefer": self.prefer, "ban": self.ban}


@dataclass
class SongBase:
    url: str
    title: str
    duration: timedelta
    filepath: Path
    artwork: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "duration": self.duration.total_seconds(),
            "filepath": str(self.filepath),
            "artwork": str(self.artwork),
        }


@dataclass
class FallbackSong:
    url: str
    title: str
    duration: timedelta
    filepath: Path
    artwork: Path
    last_played: Optional[datetime]

    @classmethod
    def from_dict(cls, data: FallbackScheme) -> FallbackSong:
        return FallbackSong(
            url=data["url"],
            title=data["title"],
            duration=timedelta(seconds=data["duration"]),
            filepath=Path(data["filepath"]),
            artwork=Path(data["artwork"]),
            last_played=(
                datetime.fromisoformat(lp) if (lp := data.get("last_played")) else None
            ),
        )

    def to_dict(self) -> FallbackScheme:
        return {
            "url": self.url,
            "title": self.title,
            "duration": self.duration.total_seconds(),
            "filepath": str(self.filepath),
            "artwork": str(self.artwork),
            "last_played": lp.isoformat() if (lp := self.last_played) else None,
        }


@dataclass
class Song:
    url: str
    title: str
    duration: timedelta
    filepath: Path
    artwork: Path
    pinned: bool
    play_count: int
    weather: RuleSet
    time: RuleSet
    last_played: Optional[datetime]
    last_dequeued: Optional[datetime]

    @classmethod
    def from_dict(cls, data: SongScheme) -> Song:
        return Song(
            url=data["url"],
            title=data["title"],
            duration=timedelta(seconds=data["duration"]),
            filepath=Path(data["filepath"]),
            artwork=Path(data["artwork"]),
            pinned=data["pinned"],
            play_count=data["play_count"],
            weather=RuleSet.from_dict(data["weather"]),
            time=RuleSet.from_dict(data["time"]),
            last_played=(
                datetime.fromisoformat(lp) if (lp := data.get("last_played")) else None
            ),
            last_dequeued=(
                datetime.fromisoformat(lp)
                if (lp := data.get("last_dequeued"))
                else None
            ),
        )

    def to_dict(self) -> SongScheme:
        return {
            "url": self.url,
            "title": self.title,
            "duration": self.duration.total_seconds(),
            "filepath": str(self.filepath),
            "artwork": str(self.artwork),
            "pinned": self.pinned,
            "play_count": self.play_count,
            "weather": self.weather.to_dict(),
            "time": self.time.to_dict(),
            "last_played": lp.isoformat() if (lp := self.last_played) else None,
            "last_dequeued": lp.isoformat() if (lp := self.last_played) else None,
        }


class Database:
    def __init__(self, lock_timeout: int = 10) -> None:
        self._songs: dict[str, Song] = {}
        self._fallback_songs: dict[str, FallbackSong] = {}

        self.db_file = CACHE / "db.yaml"
        self._lock_file = self.db_file.with_suffix(".lock")
        self._lock = FileLock(str(self._lock_file), timeout=lock_timeout)

        self.release_stale_lock()

        self._lock_internal = RLock()

        self._last_modified = 0.0

        LOG.debug(
            f"database initialising: db={self.db_file} exists={self.db_file.exists()}"
        )

        with self.acquire_lock():
            self._read()

    def release_stale_lock(self) -> None:
        if self._lock_file.exists():
            if time() - self._lock_file.stat().st_mtime > 2 * self._lock.timeout:
                LOG.warning("stale lock file detected, forcing release")
                self._lock.release(True)
                self._lock_file.unlink()

    def _reload_if_changed(self) -> None:
        if not self.db_file.exists():
            return

        if self.db_file.stat().st_mtime > self._last_modified:
            self._read()

    @contextmanager
    def acquire_lock(
        self, retries: int = 3, retry_delay: float = 0.5
    ) -> Generator[None, tuple[int, float], None]:
        attempt = 0
        while attempt < retries:
            try:
                self.release_stale_lock()
                with self._lock:
                    with self._lock_internal:
                        self._reload_if_changed
                        yield
                        return
            except Timeout:
                attempt += 1
                sleep(retry_delay)
        raise Timeout(f"Could not aquire DB lock after {retries} attempts")

    def _read(self) -> None:
        db: DBScheme = {"songs": [], "fallback": []}
        if self.db_file.exists():
            with self.db_file.open() as f:
                try:
                    if data := safe_load(f):
                        db = data
                    else:
                        LOG.debug(f"empty file: {self.db_file}")
                except Exception as e:
                    LOG.error(f"Failed to read {self.db_file}: {e}")

        self._songs = {
            sng.url: sng for song in db["songs"] if (sng := Song.from_dict(song))
        }
        self._fallback_songs = {
            fb_sng.url: fb_sng
            for fb_song in db["fallback"]
            if (fb_sng := FallbackSong.from_dict(fb_song))
        }

        self._last_modified = (
            self.db_file.stat().st_mtime if self.db_file.exists() else 0.0
        )

        LOG.debug(
            f"read completed: songs={len(self._songs)} "
            f"fallback={len(self._fallback_songs)} "
            f"last_modified={self._last_modified}"
        )

    def _write(self) -> None:
        db: DBScheme = {
            "songs": [song.to_dict() for _, song in self._songs.items()],
            "fallback": [song.to_dict() for _, song in self._fallback_songs.items()],
        }

        temp_file = self.db_file.with_suffix(".tmp")

        with temp_file.open("w", encoding="utf-8") as f:
            safe_dump(db, f, allow_unicode=True)
        try:
            temp_file.replace(self.db_file)
            self._last_modified = self.db_file.stat().st_mtime
            LOG.debug(
                f"write completed: songs={len(self._songs)} "
                f"fallback={len(self._fallback_songs)} "
                f"last_modified={self._last_modified}"
            )
        except Exception as e:
            LOG.error(f"Failed to write {self.db_file}: {e}")

    @property
    def songs(self) -> dict[str, Song]:
        with self.acquire_lock():
            return self._songs

    @property
    def fallback_songs(self) -> dict[str, FallbackSong]:
        with self.acquire_lock():
            return self._fallback_songs

    def update(self, song: Song) -> None:
        with self.acquire_lock():
            self._songs[song.url] = song
            self._write()

    def update_fallback(self, song: FallbackSong) -> None:
        with self.acquire_lock():
            self._fallback_songs[song.url] = song
            self._write()

    def remove(self, song: Song) -> bool:
        with self.acquire_lock():
            if song.url in self.songs.keys():
                del self._songs[song.url]
                self._write()
                return True
        return False

    def remove_fallback(self, song: FallbackSong) -> bool:
        with self.acquire_lock():
            if song.url in self.fallback_songs.keys():
                del self._fallback_songs[song.url]
                self._write()
                return True
        return False

    # extra methods to update metadata

    def add_played(self, song: Song | FallbackSong) -> None:
        song.last_played = datetime.now()
        if isinstance(song, Song):
            self.update(song)
        elif isinstance(song, FallbackSong):
            self.update_fallback(song)

    def add_dequeued(self, song: Song) -> None:
        song.last_dequeued = datetime.now()
        self.update(song)

    # methods to find database objects

    def song_by_obj(self, obj: Song | FallbackSong) -> Song:
        if song := self.songs.get(obj.url):
            return song
        raise FileNotFoundError(f"Song {obj.title} not in database")

    def song_by_url(self, url: str) -> Song | None:
        if song := self.songs.get(url):
            return song
        return None

    def song_by_name(self, title: str) -> Song | None:
        for song in self.songs.values():
            if song.title == title:
                return song
        return None

    def song_by_filename(self, filename: str) -> Song | None:
        for song in self.songs.values():
            if song.filepath.stem == filename:
                return song
        return None

    def song_by_filepath(self, filepath: Path) -> Song | None:
        for song in self.songs.values():
            if song.filepath.samefile(filepath):
                return song
        return None

    def fallback_by_url(self, url: str) -> FallbackSong | None:
        for song in self.fallback_songs.values():
            if song.url == url:
                return song
        return None

    def fallback_by_name(self, title: str) -> FallbackSong | None:
        for song in self.fallback_songs.values():
            if song.title == title:
                return song
        return None

    def fallback_by_filename(self, filename: str) -> FallbackSong | None:
        for song in self.fallback_songs.values():
            if song.filepath.stem == filename:
                return song
        return None
