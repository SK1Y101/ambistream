from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from os import fsync, rename
from pathlib import Path
from shutil import copyfile
from typing import Optional
from unicodedata import normalize

import yt_dlp
from mutagen.id3 import APIC, ID3

from .db import (
    CACHE,
    COVERS,
    FALLBACK,
    PINNED,
    QUEUE,
    RECENT,
    Database,
    FallbackSong,
    RuleSet,
    Song,
    SongBase,
)
from .logging import get_logger

LOG = get_logger("songs")

YDL_OPTS = {
    "no_warnings": True,
    "noplaylist": True,
    "writethumbnail": True,
    "convertthumbnails": "jpg",
    "outtmpl": str(CACHE) + "/" + "%(title)s.%(ext)s",
    "format": "bestaudio/best",
    "postprocessors": [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0",
        },
        {
            "key": "EmbedThumbnail",
        },
    ],
}


def recency_distance(a: datetime, b: datetime) -> float:
    return 1 - min(1, abs((a - b).total_seconds()) / (86400 * 7))


def dequeue_distance(a: datetime, b: datetime) -> float:
    return 1 - min(1, abs((a - b).total_seconds()) / (86400 * 1))


MAX_RECENCY_DISTANCE = recency_distance(datetime.min, datetime.min)
MAX_DEQUEUE_DISTANCE = dequeue_distance(datetime.min, datetime.min)


class SongHandler:
    def __init__(self, cache_size: int = 100):
        self.database = Database()

        self.max_cache = cache_size

        LOG.debug(f"songs initialised with max_cache={self.max_cache}")

    def download_song(self, url: str, download_to: Path | None = None) -> SongBase:
        song = None
        LOG.debug(f"Downloading song from URL: {url}")
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            if info := ydl.extract_info(url, download=True):
                filepath = (
                    Path(ydl.prepare_filename(info)).expanduser().with_suffix(".mp3")
                )
                song = SongBase(
                    url=url,
                    title=normalize("NFC", info.get("title")),
                    duration=timedelta(seconds=info.get("duration")),
                    filepath=filepath,
                    artwork=filepath,
                )
        if song:
            move_to = download_to or RECENT

            new_path = move_to / song.filepath.name
            if new_path.exists():
                song.filepath.unlink()
            else:
                self.safe_move(song.filepath, move_to)

            song.filepath = move_to / song.filepath.name
            self.ensure_art(song)

            LOG.debug(f"downloaded '{song.title}' to {song.filepath}")
            return song
        raise RuntimeError(f"Could not download song from {url}")

    def ensure(self, song: Song | FallbackSong | SongBase) -> None:
        LOG.debug(f"ensuring existence of {song.title}")
        if not song.filepath.exists():
            LOG.info(f"song missing, downloading {song.url}")
            new = self.download_song(song.url, song.filepath.parent)
            song.url = new.url
            song.title = new.title
            song.duration = new.duration
            song.filepath = new.filepath
            song.artwork = new.artwork
            if isinstance(song, Song):
                self.database.update(song)
            if isinstance(song, FallbackSong):
                self.database.update_fallback(song)

    def remove_song(self, song: Song) -> bool:
        self.database.remove(song)
        if song.filepath.exists():
            song.filepath.unlink()
        if song.artwork.exists():
            song.artwork.unlink()
        return True

    def remove_fallback(self, song: FallbackSong) -> bool:
        self.database.remove_fallback(song)
        if song.filepath.exists():
            song.filepath.unlink()
        if song.artwork.exists():
            song.artwork.unlink()
        return True

    def remove_from_queue(self, name: str) -> bool:
        for p in self._raw_queue:
            if name in [p.stem, p.name]:
                p.unlink(missing_ok=True)
                return True
        return False

    def delete_song(self, song: Song | FallbackSong | SongBase) -> None:
        if sng := self.database.song_by_url(song.url):
            self.database.remove(sng)
        if fb := self.database.fallback_by_url(song.url):
            self.database.remove_fallback(fb)
        song.filepath.unlink()
        song.artwork.unlink()

    def pin_song(self, song: Song) -> Song:
        song = self.database.song_by_obj(song)
        LOG.debug(f"pinning song '{song.title}'")

        if song.filepath.parent == FALLBACK:
            self.safe_copy(song.filepath, PINNED)
        elif song.filepath.parent != PINNED:
            self.safe_move(song.filepath, PINNED)
        song.pinned = True
        song.filepath = PINNED / song.filepath.name
        self.database.update(song)
        return song

    def unpin_song(self, song: Song) -> Song:
        song = self.database.song_by_obj(song)
        LOG.debug(f"unpinning song '{song.title}'")
        if song.filepath.parent == PINNED:
            self.safe_move(song.filepath, RECENT)
        song.pinned = False
        self.database.update(song)
        return song

    @property
    def _raw_covers(self) -> list[Path]:
        return sorted(
            list(COVERS.glob("**/*.png")) + list(COVERS.glob("**/*.jpg")),
            key=lambda x: x.stat().st_mtime,
        )

    @property
    def _raw_cache(self) -> list[Path]:
        return sorted(list(RECENT.glob("**/*.mp3")), key=lambda x: x.stat().st_mtime)

    @property
    def _raw_queue(self) -> list[Path]:
        return sorted(list(QUEUE.glob("**/*.mp3")), key=lambda x: x.stat().st_mtime)

    @property
    def cache_size(self) -> int:
        return len(self.get_recent_cache())

    @property
    def queue_songs(self) -> list[str]:
        return [s.name for s in self._raw_queue]

    @property
    def queue_size(self) -> int:
        return len(self._raw_queue)

    def get_recent_cache(self) -> list[Song]:
        songs = [
            s
            for s in self.database.songs.values()
            for p in self._raw_cache
            if s.filepath.expanduser() == p.expanduser() and s.last_played
        ]
        return songs

    def prune_cache(self) -> None:
        cache = self.get_recent_cache()
        if len(cache) > self.max_cache:
            LOG.debug(f"pruning cache, {len(cache) - self.max_cache} to remove")

        while len(self.get_recent_cache()) > self.max_cache:
            oldest = self._raw_cache[0]
            oldest.unlink()

    def cache_song(self, song: Song) -> None:
        self.ensure(song)
        song = self.database.song_by_obj(song)
        cache = self.get_recent_cache()
        LOG.debug(f"Adding song '{song.title}' to cache, " f"cache size: {len(cache)}")

        urls = {s.url for s in cache}
        if song.filepath.parent == FALLBACK:
            self.safe_copy(song.filepath, RECENT)
        elif song.url not in urls:
            self.safe_move(song.filepath, RECENT)
        song.filepath = RECENT / song.filepath.name

        self.database.update(song)

        if song.pinned:
            self.pin_song(song)
        self.prune_cache()

    def play_song(self, song: Song | FallbackSong | SongBase) -> None:
        """Add a song to the play queue."""
        if song.filepath.name not in self.queue_songs:
            LOG.debug(f"adding song '{song.title}' to play queue")
            self.ensure(song)
            self.safe_copy(song.filepath, QUEUE)
            return
        raise FileExistsError(f"Song {song.filepath.name} already queued")

    def promote_fallback(
        self, song: Song | SongBase, last_played: Optional[datetime] = None
    ) -> FallbackSong:
        LOG.debug(f"promoting song '{song.title}' to fallback.")
        sng = FallbackSong(
            url=song.url,
            title=song.title,
            duration=song.duration,
            filepath=song.filepath,
            artwork=song.artwork,
            last_played=(song.last_played if isinstance(song, Song) else None)
            or last_played,
        )

        fallback_filepath = FALLBACK / sng.filepath.name

        if sng.filepath.exists():
            if db := self.database.song_by_url(sng.url):
                self.safe_copy(db.filepath, fallback_filepath)

            elif sng.filepath.parent == QUEUE:
                self.safe_move(sng.filepath, fallback_filepath)

        sng.filepath = fallback_filepath
        self.ensure(sng)
        self.database.update_fallback(sng)

        return sng

    def promote_song(
        self,
        song: FallbackSong | SongBase,
        pinned: bool = False,
        play_count: int = 0,
        last_played: Optional[datetime] = None,
        last_dequeued: Optional[datetime] = None,
        weather: Optional[RuleSet] = None,
        time: Optional[RuleSet] = None,
    ) -> Song:
        """Called by the web ui to promote a song to the database."""
        LOG.debug(f"promoting song '{song.title}' to main library.")
        sng = Song(
            url=song.url,
            title=song.title,
            duration=song.duration,
            filepath=song.filepath,
            artwork=song.artwork,
            pinned=pinned,
            play_count=play_count,
            weather=weather or RuleSet(),
            time=time or RuleSet(),
            last_played=last_played,
            last_dequeued=last_dequeued,
        )
        song_filepath = RECENT / sng.filepath.name

        if sng.filepath.exists():
            if fb := self.database.fallback_by_url(sng.url):
                self.safe_copy(fb.filepath, song_filepath)

            elif sng.filepath.parent == QUEUE:
                self.safe_move(sng.filepath, song_filepath)

        sng.filepath = song_filepath
        self.ensure(sng)
        self.database.update(sng)

        if sng.pinned:
            return self.pin_song(sng)

        return sng

    def allow_song(
        self,
        song: Song,
        weather: str,
        time: Optional[str] = None,
    ) -> bool:
        """Called by the orchestrator to determine if the provided weather/time/themes
        are valid for the chosen song to be played"""
        if song.weather.ban and weather and weather in song.weather.ban:
            return False
        if song.time.ban and time and time in song.time.ban:
            return False
        return True

    def valid_songs(
        self,
        weather: str,
        time: Optional[str] = None,
    ) -> list[Song]:
        """Return a list of all songs that aren't explicitly banned from the
        provided weather/time/themes."""
        LOG.debug(f"fetching valid songs for weather={weather} time={time}")
        return [
            song
            for song in self.database.songs.values()
            if self.allow_song(song, weather, time)
        ]

    @property
    def all_songs(self) -> list[Song]:
        return list(self.database.songs.values())

    @property
    def all_fallback(self) -> list[FallbackSong]:
        return list(self.database.fallback_songs.values())

    def get_song(self, identifier: str) -> Song | None:
        if url_song := self.database.song_by_url(identifier):
            return url_song
        elif title_song := self.database.song_by_name(identifier):
            return title_song
        elif name_song := self.database.song_by_filename(identifier):
            return name_song
        return None

    def get_fallback(self, identifier: str) -> FallbackSong | None:
        if url_fallback := self.database.fallback_by_url(identifier):
            return url_fallback
        elif title_fallback := self.database.fallback_by_name(identifier):
            return title_fallback
        elif name_fallback := self.database.fallback_by_filename(identifier):
            return name_fallback
        return None

    def get_song_by_title(self, title: str) -> tuple[Song | FallbackSong, bool, bool]:
        song = self.database.song_by_name(title)
        fallback = self.database.fallback_by_name(title)
        found_obj = song or fallback

        if not (found_obj):
            raise NameError(f"Could not find song or fallback '{title}'")

        return (
            found_obj,
            song is not None,
            fallback is not None,
        )

    def get_song_by_url(
        self, url: str
    ) -> tuple[Song | FallbackSong | SongBase, bool, bool]:
        song = self.database.song_by_url(url)
        fallback = self.database.fallback_by_url(url)

        return (
            song or fallback or self.download_song(url, RECENT),
            song is not None,
            fallback is not None,
        )

    def update_song(self, song: Song) -> None:
        if song.pinned:
            self.pin_song(song)
        else:
            self.unpin_song(song)
        self.database.update(song)

    def update_fallback(self, song: FallbackSong) -> None:
        self.database.update_fallback(song)

    def as_song_obj(
        self, song_or_path: Song | FallbackSong | SongBase | Path
    ) -> Song | FallbackSong | SongBase:
        if isinstance(song_or_path, Path):
            return SongBase(
                url="",
                title="",
                duration=timedelta(),
                filepath=song_or_path,
                artwork=COVERS / song_or_path.name,
            )
        return song_or_path

    def update_art(
        self, artpath: Path, song_or_path: Song | FallbackSong | SongBase | Path
    ) -> None:
        if isinstance(song_or_path, (Path, SongBase)):
            return

        song_or_path.artwork = artpath
        if isinstance(song_or_path, Song):
            self.database.update(song_or_path)
        elif isinstance(song_or_path, FallbackSong):
            self.database.update_fallback(song_or_path)
        return

    def artwork_from_embed(self, path: Path) -> None | tuple[bytes, str]:
        try:
            for tag in ID3(path).values():
                if isinstance(tag, APIC):
                    return tag.data, tag.mime  # type: ignore [attr-defined]
        except Exception:
            pass
        return None

    def find_artfile(
        self, song_or_path: SongBase | Song | FallbackSong | Path
    ) -> Path | None:
        LOG.debug("Fetching artwork if not found")
        song = self.as_song_obj(song_or_path=song_or_path)
        assert isinstance(
            song, (Song, FallbackSong, SongBase)
        ), f"song is {type(song)}?"
        path = song.filepath
        artpath = song.artwork

        for candidate in (
            path.with_suffix(".jpg"),
            path.with_suffix(".png"),
            artpath.with_suffix(".jpg"),
            artpath.with_suffix(".png"),
            COVERS / path.with_suffix(".jpg").name,
            COVERS / path.with_suffix(".png").name,
            COVERS / artpath.with_suffix(".jpg").name,
            COVERS / artpath.with_suffix(".png").name,
        ):
            if candidate.exists():
                return candidate

        return None

    def art_data(self, path: Path) -> None | tuple[bytes, str]:
        if not path.exists():
            return None

        mime = {".png": "image/png", ".jpg": "image/jpeg"}.get(path.suffix, "")
        return path.read_bytes(), mime

    def ensure_art(self, song: SongBase | Song | FallbackSong) -> None:
        if song.artwork.exists():
            return

        if artfile := self.find_artfile(song):
            self.update_art(artfile, song)
            return

        if art_data := self.artwork_from_embed(song.filepath):
            extension = {"image/png": ".png", "image/jpeg": ".jpg"}.get(
                art_data[1], ".jpg"
            )
            song.artwork = song.artwork.with_suffix(extension)
            song.artwork.write_bytes(art_data[0])

            if song.artwork.parent != COVERS:
                new_path = COVERS / song.artwork.name
                if new_path.exists():
                    song.artwork.unlink()
                else:
                    self.safe_move(song.artwork, COVERS)
                song.artwork = new_path

            self.update_art(song.artwork, song)
            return

        raise FileNotFoundError(f"Could not find artwork file for {song}")

    def get_song_artwork(
        self, song: SongBase | Song | FallbackSong
    ) -> None | tuple[bytes, str]:
        self.ensure_art(song)
        return self.art_data(song.artwork)

    def get_artwork_from_file(self, path: Path) -> None | tuple[bytes, str]:
        if artfile := self.find_artfile(path):
            return self.art_data(artfile)
        return None

    def safe_copy(self, path: Path, to_path: Path) -> None:
        if to_path.is_dir():
            to_path = to_path / path.name

        with tempfile.NamedTemporaryFile(dir=to_path.parent, delete=False) as tmp:
            tmp_name = tmp.name
            copyfile(path, tmp_name)
            tmp.flush()
            fsync(tmp.fileno())
        rename(tmp_name, to_path)

    def safe_move(self, path: Path, to_path: Path) -> None:
        self.safe_copy(path=path, to_path=to_path)
        path.unlink()
