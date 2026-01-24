from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import RLock
from time import sleep, time
from typing import Generator, Optional, TypedDict

from filelock import FileLock, Timeout
from yaml import safe_dump, safe_load

from .db import CACHE
from .logging import get_logger

LOG = get_logger("state")


class Source(Enum):
    song = "song"
    fallback = "fallback"
    queue = "queue"


class Owner(Enum):
    player = "player"
    orchestrator = "orchestrator"
    weather = "weather"
    backend = "backend"
    state = "state"


class Operation(Enum):
    enqueue = "enqueue"
    dequeue = "dequeue"
    clear_queue = "clear_queue"
    remove_queue = "remove_queue"
    set_current = "set_current"
    clear_current = "clear_current"
    set_history = "set_history"
    clear_history = "clear_history"
    set_weather = "set_weather"
    clear_weather = "clear_weather"
    set_status = "set_status"
    clear_status = "clear_status"


class Status(Enum):
    unknown = "unknown"

    # normal operations
    startup = "startup"
    running = "running"
    shutdown = "shutdown"

    # issues
    error = "error"

    # program specific


OP = Operation


class CurrentScheme(TypedDict):
    url: str
    title: str
    filepath: str
    source: str
    started_at: str


class HistoryScheme(TypedDict):
    url: str
    title: str
    filepath: str
    source: str
    played_at: str


class QueueScheme(TypedDict):
    url: str
    title: str
    filepath: str
    source: str
    queued_at: str


class WeatherScheme(TypedDict):
    condition: str
    description: str
    temperature: float
    sunrise: str
    sunset: str
    midday: str
    timeperiod: str
    fetched_at: str
    valid_until: str


class StateScheme(TypedDict):
    current: Optional[CurrentScheme]
    history: list[HistoryScheme]
    queue: list[QueueScheme]
    weather: Optional[WeatherScheme]
    last_update_op: Optional[str]
    last_update_by: Optional[str]
    statuses: dict[str, str]


@dataclass
class QueuedSong:
    url: str
    title: str
    filepath: Path
    source: Source
    queued_at: datetime

    @classmethod
    def from_dict(cls, data: QueueScheme) -> QueuedSong:
        return QueuedSong(
            url=data["url"],
            title=data["title"],
            filepath=Path(data["filepath"]),
            source=Source(data["source"]),
            queued_at=datetime.fromisoformat(data["queued_at"]),
        )

    def to_dict(self) -> QueueScheme:
        return {
            "url": self.url,
            "title": self.title,
            "filepath": str(self.filepath),
            "source": self.source.name,
            "queued_at": self.queued_at.isoformat(),
        }

    def to_current(self) -> CurrentSong:
        return CurrentSong(
            url=self.url,
            title=self.title,
            filepath=self.filepath,
            source=self.source,
            started_at=datetime.now(),
        )


@dataclass
class CurrentSong:
    url: str
    title: str
    filepath: Path
    source: Source
    started_at: datetime

    @classmethod
    def from_dict(cls, data: CurrentScheme) -> CurrentSong:
        return CurrentSong(
            url=data["url"],
            title=data["title"],
            filepath=Path(data["filepath"]),
            source=Source(data["source"]),
            started_at=datetime.fromisoformat(data["started_at"]),
        )

    def to_dict(self) -> CurrentScheme:
        return {
            "url": self.url,
            "title": self.title,
            "filepath": str(self.filepath),
            "source": self.source.name,
            "started_at": self.started_at.isoformat(),
        }

    def to_history(self) -> HistorySong:
        return HistorySong(
            url=self.url,
            title=self.title,
            filepath=self.filepath,
            source=self.source,
            played_at=self.started_at,
        )


@dataclass
class HistorySong:
    url: str
    title: str
    filepath: Path
    source: Source
    played_at: datetime

    @classmethod
    def from_dict(cls, data: HistoryScheme) -> HistorySong:
        return HistorySong(
            url=data["url"],
            title=data["title"],
            filepath=Path(data["filepath"]),
            source=Source(data["source"]),
            played_at=datetime.fromisoformat(data["played_at"]),
        )

    def to_dict(self) -> HistoryScheme:
        return {
            "url": self.url,
            "title": self.title,
            "filepath": str(self.filepath),
            "source": self.source.name,
            "played_at": self.played_at.isoformat(),
        }


@dataclass
class Weather:
    condition: str
    description: str
    temperature: float
    sunrise: datetime
    sunset: datetime
    midday: datetime
    timeperiod: str
    fetched_at: datetime
    valid_until: datetime

    @classmethod
    def from_dict(cls, data: WeatherScheme) -> Weather:
        return Weather(
            condition=data["condition"],
            description=data["description"],
            temperature=data["temperature"],
            sunrise=datetime.fromisoformat(data["sunrise"]),
            sunset=datetime.fromisoformat(data["sunset"]),
            midday=datetime.fromisoformat(data["midday"]),
            timeperiod=data["timeperiod"],
            fetched_at=datetime.fromisoformat(data["fetched_at"]),
            valid_until=datetime.fromisoformat(data["valid_until"]),
        )

    def to_dict(self) -> WeatherScheme:
        return {
            "condition": self.condition,
            "description": self.description,
            "temperature": self.temperature,
            "sunrise": self.sunrise.isoformat(),
            "sunset": self.sunset.isoformat(),
            "midday": self.midday.isoformat(),
            "timeperiod": self.timeperiod,
            "fetched_at": self.fetched_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
        }


class State:
    def __init__(self, lock_timeout: int = 10, owner: Owner = Owner.state) -> None:
        self.current: Optional[CurrentSong] = None
        self.history: list[HistorySong] = []
        self.queue: list[QueuedSong] = []
        self.weather: Optional[Weather] = None
        self.last_update_op: Optional[Operation] = None
        self.last_update_by: Optional[Owner] = None
        self.statuses: dict[Owner, Status] = {}

        self.max_history = 10
        self.owner = owner

        self.state_file = CACHE / "runtime.state"

        self._lock_file = self.state_file.with_suffix(".lock")
        self._lock = FileLock(str(self._lock_file), timeout=lock_timeout)

        self.release_stale_lock()

        self._lock_internal = RLock()

        self._last_modified = 0.0

        LOG.debug(
            f"state initialising: state={self.state_file} "
            f"exists={self.state_file.exists()} "
            f"owner={self.owner}"
        )

        with self.acquire_lock():
            self._read()

    def release_stale_lock(self) -> None:
        if self._lock_file.exists():
            if time() - self._lock_file.stat().st_mtime > 2 * self._lock.timeout:
                LOG.warning("stale lock file detected, forcing release")
                self._lock.release(True)
                self._lock_file.unlink()

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
                        self._reload_if_changed()
                        yield
                        return
            except Timeout:
                attempt += 1
                sleep(retry_delay)
        raise Timeout(f"Could not aquire state lock after {retries} attempts")

    def _reload_if_changed(self) -> None:
        if not self.state_file.exists():
            return

        if self.state_file.stat().st_mtime > self._last_modified:
            self._read()

    def _read(self) -> None:
        state: StateScheme = {
            "current": None,
            "history": [],
            "queue": [],
            "weather": None,
            "last_update_op": None,
            "last_update_by": None,
            "statuses": {},
        }

        if self.state_file.exists():
            with self.state_file.open() as f:
                try:
                    if data := safe_load(f):
                        state = data
                    else:
                        LOG.debug(f"empty file: {self.state_file}")
                except Exception as e:
                    LOG.error(f"Failed to read {self.state_file}: {e}")

        if current := state.get("current"):
            self.current = CurrentSong.from_dict(current)
        if weather := state.get("weather"):
            self.weather = Weather.from_dict(weather)

        self.last_update_op = OP(lu) if (lu := state.get("last_update_op")) else None
        self.last_update_by = Owner(lb) if (lb := state.get("last_update_by")) else None
        self.history = [HistorySong.from_dict(h) for h in state.get("history", [])]
        self.queue = [QueuedSong.from_dict(q) for q in state.get("queue", [])]
        self.statuses = {
            Owner(ow): Status(st) for ow, st in state.get("statuses", {}).items()
        }

        self._last_modified = (
            self.state_file.stat().st_mtime if self.state_file.exists() else 0.0
        )

        LOG.debug(f"read completed: " f"last_modified={self._last_modified}")

    def _write(self) -> None:
        db: StateScheme = self._snapshot

        temp_file = self.state_file.with_suffix(".tmp")

        with temp_file.open("w", encoding="utf-8") as f:
            safe_dump(db, f, allow_unicode=True)
        try:
            temp_file.replace(self.state_file)
            self._last_modified = self.state_file.stat().st_mtime

            LOG.debug(
                f"write completed: "
                f"last_modified={self._last_modified} "
                f"operation={self.last_update_op} "
                f"by={self.last_update_by}"
            )

        except Exception as e:
            LOG.error(f"Failed to write {self.state_file}: {e}")

    def _touch(self, operation: Operation) -> None:
        self.last_update_op = operation
        self.last_update_by = self.owner
        self._write()

    @property
    def _snapshot(self) -> StateScheme:
        return {
            "current": c.to_dict() if (c := self.current) else None,
            "queue": [q.to_dict() for q in self.queue],
            "history": [h.to_dict() for h in self.history],
            "weather": w.to_dict() if (w := self.weather) else None,
            "last_update_op": lo.name if (lo := self.last_update_op) else None,
            "last_update_by": lb.name if (lb := self.last_update_by) else None,
            "statuses": {ow.name: st.name for ow, st in self.statuses.items()},
        }

    def get_snapshot(self) -> StateScheme:
        with self.acquire_lock():
            return self._snapshot

    def get_current(self) -> Optional[CurrentSong]:
        with self.acquire_lock():
            return self.current

    def set_current(self, song: CurrentSong) -> None:
        with self.acquire_lock():
            self.current = song
            self._touch(OP.set_current)

    def clear_current(self) -> None:
        with self.acquire_lock():
            self.current = None
            self._touch(OP.clear_current)

    def get_queue(self) -> list[QueuedSong]:
        with self.acquire_lock():
            return self.queue

    def clear_queue(self) -> None:
        with self.acquire_lock():
            self.queue = []
            self._touch(OP.clear_queue)

    def enqueue(self, song: QueuedSong) -> None:
        with self.acquire_lock():
            self.queue.append(song)
            self._touch(OP.enqueue)

    def dequeue(self) -> QueuedSong:
        with self.acquire_lock():
            q = self.queue.pop(0)
            self._touch(OP.dequeue)
            return q

    def remove_from_queue(self, url_or_title: str) -> bool:
        with self.acquire_lock():
            obj_idx: Optional[int] = None
            for i, q in enumerate(self.queue):
                if url_or_title in [q.url, q.title]:
                    obj_idx = i
                    break

            if obj_idx is not None:
                self.queue.pop(obj_idx)
                self._touch(OP.remove_queue)
                return True
            LOG.debug(f"'{url_or_title}' not found when removing from state queue")
            return False

    def append_history(self, song: HistorySong) -> None:
        with self.acquire_lock():
            self.history.append(song)
            self.history = self.history[-self.max_history :]
            self._touch(OP.set_history)

    def get_history(self, limit: Optional[int] = None) -> list[HistorySong]:
        with self.acquire_lock():
            return self.history[: limit or self.max_history]

    def clear_history(self) -> None:
        with self.acquire_lock():
            self.history = []
            self._touch(OP.clear_history)

    def get_weather(self) -> Weather | None:
        with self.acquire_lock():
            return self.weather

    def set_weather(self, weather: Weather) -> None:
        with self.acquire_lock():
            self.weather = weather
            self._touch(OP.set_weather)

    def clear_weather(self) -> None:
        with self.acquire_lock():
            self.weather = None
            self._touch(OP.clear_weather)

    def set_status(self, status: Status) -> None:
        with self.acquire_lock():
            self.statuses[self.owner] = status
            self._touch(OP.set_status)

    def clear_status(self) -> None:
        with self.acquire_lock():
            self.statuses[self.owner] = Status.unknown
            self._touch(OP.clear_status)
