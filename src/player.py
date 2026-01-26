from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from os import kill
from pathlib import Path
from random import choices
from signal import SIGTERM
from stat import S_ISFIFO
from subprocess import DEVNULL, Popen, run
from tempfile import NamedTemporaryFile
from threading import Event, Lock, Thread
from time import sleep
from typing import Callable, Optional

import watchdog.events
import watchdog.observers

from .db import CACHE, QUEUE, Database, FallbackSong, Song
from .logging import get_logger
from .state import CurrentSong as StateCurrent
from .state import (
    Owner,
)
from .state import QueuedSong as StateQueued
from .state import (
    Source,
    State,
    Status,
)

LOG = get_logger("player")
FALLBACK_SLICE_DURATION = 15

FIFO = CACHE / "fifo.pipe"


@dataclass
class Fallback:
    song: FallbackSong
    offset: float = 0.0


class QueueWatcher(watchdog.events.FileSystemEventHandler):
    def __init__(self, player: Player) -> None:
        self.player = player
        self.log = LOG.getChild("queue-watcher")

    def song_created(self, path) -> None:
        self.log.debug(f"new song added to queue {path.stem}")
        self.player.queue_song(path)

    def on_moved(
        self, event: watchdog.events.DirMovedEvent | watchdog.events.FileMovedEvent
    ) -> None:
        self.log.debug(f"move event: {event}")

        path = Path(str(event.dest_path))
        if path.suffix != ".mp3":
            return
        self.song_created(path)

    def on_created(
        self, event: watchdog.events.DirCreatedEvent | watchdog.events.FileCreatedEvent
    ) -> None:
        self.log.debug(f"create event: {event}")

        path = Path(str(event.src_path))
        if path.suffix != ".mp3":
            return
        self.song_created(path)

    def on_deleted(
        self, event: watchdog.events.FileDeletedEvent | watchdog.events.DirDeletedEvent
    ) -> None:
        self.log.debug(f"delete event: {event}")

        path = Path(str(event.src_path))

        if path.suffix != ".mp3":
            return

        LOG.debug(f"song requested to be removed from queue {path.stem}")
        self.player.remove_from_queue(path)


class Player:
    def __init__(self):
        self.database = Database()
        self.state = State(owner=Owner.player)
        self.state.set_status(Status.startup)

        self.current_song: Song | None = None
        self.song_queue: list[Path] = []
        self.shutdown = Event()

        self.queue_lock = Lock()

        self.observer: watchdog.observers.api.BaseObserver = None
        self.playback: Popen = None
        self.fallback: Fallback = None
        self.has_fallback = False

    @property
    def fallback_songs(self) -> list[FallbackSong]:
        return list(self.database.fallback_songs.values())

    @property
    def valid_fallback_songs(self) -> list[FallbackSong]:
        return [s for s in self.fallback_songs if s.filepath.exists()]

    def create_fifo(self) -> None:
        if FIFO.exists() and not S_ISFIFO(FIFO.stat().st_mode):
            LOG.error("removing non-fifo file at fifo queue path")
            FIFO.unlink()

        if FIFO.exists():
            FIFO.unlink()

        if not FIFO.exists():
            LOG.debug("creating fifo queue")
            run(
                ["mkfifo", str(FIFO)],
                stdin=DEVNULL,
                stdout=DEVNULL,
                stderr=DEVNULL,
                check=True,
            )

        if not S_ISFIFO(FIFO.stat().st_mode):
            raise RuntimeError(f"{FIFO.name} is not a fifo queue")
        LOG.debug(f"fifo ready at {FIFO}")

    def start_ffmpeg(self) -> None:
        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(FIFO),
            "-f",
            "pulse",
            "default",
        ]
        self.playback = Popen(
            cmd, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, start_new_session=True
        )
        LOG.debug(f"ffmpeg process started, pid={self.playback.pid}")

    def play_song(self, path: Path) -> None:
        try:
            self.current_song = self.database.song_by_filename(path.stem)
            current_song = (
                StateCurrent(
                    url=self.current_song.url,
                    title=self.current_song.title,
                    filepath=self.current_song.filepath,
                    source=Source.song,
                    started_at=datetime.now(),
                )
                if self.current_song
                else StateCurrent(
                    url="unknown",
                    title=path.stem,
                    filepath=path,
                    source=Source.queue,
                    started_at=datetime.now(),
                )
            )
            self.state.set_current(current_song)

            cmd = [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(current_song.filepath),
                "-f",
                "wav",
                str(FIFO),
            ]
            LOG.debug(f"pushing {current_song.filepath.stem} to ffmpeg {FIFO.name}")
            run(cmd, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, check=False)

            if self.current_song:
                self.database.add_played(self.current_song)
            self.current_song = None
            self.state.clear_current()
            self.state.append_history(current_song.to_history())
        finally:
            path.unlink(missing_ok=True)

    def choose_fallback(self) -> FallbackSong | None:
        now = datetime.now()

        weights: list[float] = []
        if fallback := self.valid_fallback_songs:

            for song in fallback:
                weights.append(
                    (now - song.last_played).total_seconds() / 86400
                    if song.last_played
                    else 0.0
                )

            max_dist = max(weights)

            choice = choices(fallback, [(1 + max_dist - w) for w in weights])

            return choice[0]
        return None

    def play_fallback(self) -> None:
        if not self.fallback_songs:
            LOG.error("No fallback songs have been created")
            sleep(1)
            return

        if not self.valid_fallback_songs:
            LOG.error("No fallback songs exist on disk")
            sleep(1)
            return

        fb = self.choose_fallback()
        if fb is None:
            LOG.error("No valid fallback songs could be chosen on disk")
            sleep(1)
            return

        self.has_fallback = True

        if not self.fallback:
            self.fallback = Fallback(fb)

        while True:
            duration = self.fallback.song.duration.total_seconds()
            LOG.info(f"playing fallback: {self.fallback.song.title}")
            current_fallback = StateCurrent(
                url=self.fallback.song.url,
                title=self.fallback.song.title,
                filepath=self.fallback.song.filepath,
                source=Source.fallback,
                started_at=datetime.now(),
            )
            self.state.set_current(current_fallback)

            with NamedTemporaryFile(delete=True) as tmp:
                tmp.write(self.fallback.song.filepath.read_bytes())
                tmp.flush()

                while self.fallback.offset < duration:
                    this_slice = min(
                        FALLBACK_SLICE_DURATION, duration - self.fallback.offset
                    )

                    if self.shutdown.is_set():
                        self.database.add_played(self.fallback.song)
                        self.state.clear_current()
                        return
                    if self.song_queue:
                        LOG.info("queue populated, abandoning fallback")
                        self.database.add_played(self.fallback.song)
                        self.state.clear_current()
                        return

                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-loglevel",
                        "error",
                        "-ss",
                        str(self.fallback.offset),
                        "-t",
                        str(this_slice),
                        "-i",
                        str(tmp.name),
                        "-f",
                        "wav",
                        str(FIFO),
                    ]
                    run(cmd, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, check=False)
                    self.fallback.offset += this_slice

            self.database.add_played(self.fallback.song)
            self.state.clear_current()

            fb = self.choose_fallback()
            if fb is None:
                LOG.error("No valid fallback songs could be chosen on disk")
                sleep(1)
                return
            self.fallback = Fallback(fb)

    def pop_next_song(self) -> Optional[Path]:
        with self.queue_lock:
            if not self.song_queue:
                return None
            path = self.song_queue.pop(0)
            self.state.dequeue()
            return path

    def fifo_writer(self) -> None:
        log = LOG.getChild("fifo")
        log.info("started")
        try:
            while not self.shutdown.is_set():
                path = self.pop_next_song()
                if not path:
                    if not self.has_fallback:
                        LOG.info("No songs in queue, using fallback.")
                    self.play_fallback()
                    continue

                self.has_fallback = False
                LOG.info(f"playing {path.stem}")

                try:
                    self.play_song(path)
                except Exception as e:
                    path.unlink(missing_ok=True)
                    log.error(f"failed to play {path.stem}: {e}")
        except Exception as e:
            log.error(f"caught exception: {e}, shutting down")
            self.shutdown.set()

    def start_watcher(self) -> None:
        self.observer = watchdog.observers.Observer()
        self.observer.schedule(QueueWatcher(self), str(QUEUE), recursive=False)
        self.observer.start()

    def queue_song(self, path: Path) -> None:
        with self.queue_lock:
            self.song_queue.append(path)

        queue_song = (
            StateQueued(
                url=q.url,
                title=q.title,
                filepath=q,
                source=Source.song,
                queued_at=datetime.now(),
            )
            if (q := self.database.song_by_name(path.stem))
            else StateQueued(
                url="unknown",
                title=path.stem,
                filepath=path,
                source=Source.queue,
                queued_at=datetime.now(),
            )
        )
        self.state.enqueue(queue_song)

    def remove_from_queue(self, path: Path) -> None:
        with self.queue_lock:
            idx: Optional[int] = None
            for i, q in enumerate(self.song_queue):
                if q.stem == path.stem:
                    idx = i
                    break

            if idx is not None:
                LOG.info(f"removed {path.stem} from queue (was at {idx})")
                self.song_queue.pop(idx)
                path.unlink(missing_ok=True)
                self.state.remove_from_queue(path.stem)
                if song := self.database.song_by_name(path.stem):
                    self.database.add_dequeued(song)

                return
            LOG.info(f"could not find {path.stem} in queue")

    @property
    def songs_in_queue(self) -> list[Path]:
        return list(QUEUE.glob("**/*.mp3"))

    def add_existing_to_queue(self) -> None:
        self.state.clear_queue()
        for path in sorted(self.songs_in_queue, key=lambda x: x.stat().st_mtime):
            self.queue_song(path)

    def safe_execute(self, label: str, function: Callable, **kwargs) -> None:
        LOG.debug(f"running {label}")
        try:
            function(**kwargs)
            LOG.debug(f"ran {label}")
        except Exception as e:
            LOG.error(f"exception during {label}: {e}")

    def kill_playback(self) -> None:
        kill(self.playback.pid, SIGTERM)

    def run(self) -> None:
        self.create_fifo()
        self.start_ffmpeg()
        self.start_watcher()
        self.add_existing_to_queue()

        LOG.debug(
            f"player initialising: fallback={len(self.fallback_songs)},"
            f" valid={len(self.valid_fallback_songs)},"
            f" existing queue={len(self.songs_in_queue)} fifo={FIFO}"
        )

        fifo_writer = Thread(target=self.fifo_writer)
        fifo_writer.start()

        LOG.info("player running")

        try:
            self.state.set_status(Status.running)
            while not self.shutdown.is_set():
                sleep(1)
        except KeyboardInterrupt:
            LOG.info("shutting down player")
        finally:
            self.state.set_status(Status.shutdown)
            self.state.clear_current()
            self.state.clear_queue()

            if self.current_song:
                self.database.add_played(self.current_song)

            self.safe_execute("shutdown set", self.shutdown.set)

            if self.playback and self.playback.poll() is None:
                self.safe_execute("playback terminate", self.kill_playback)

            if fifo_writer.is_alive():
                self.safe_execute("stop fifo", fifo_writer.join, timeout=20)

            if self.observer:
                self.safe_execute("oserver stop", self.observer.stop)
                self.safe_execute("oserver terminate", self.observer.join, timeout=20)

            if FIFO.exists():
                self.safe_execute("fifo unlink", FIFO.unlink, missing_ok=True)

            LOG.info("player shutdown")
            LOG.debug(
                f"shutdown: fifo={FIFO.exists()}, "
                f"ffmpeg={self.playback and self.playback.poll() is None}"
            )


def main() -> None:
    player = Player()
    try:
        player.run()
    except Exception as e:
        player.state.set_status(Status.error)
        LOG.error(e)


if __name__ == "__main__":
    main()
