from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from enum import Enum
from json import dumps
from logging import INFO
from pathlib import Path
from typing import AsyncGenerator, Generator, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from yt_dlp.utils import DownloadError

from .logging import get_logger
from .songs import FallbackSong, RuleSet, Song, SongBase, SongHandler
from .state import Owner, State, StateScheme

LOG = get_logger("backend")


class MessageType(Enum):
    data = "data"
    heartbeat = "data"


class Message:
    def __init__(self, type: MessageType = MessageType.heartbeat, data: str = ""):
        self.type = type
        self.data = data

    def __str__(self) -> str:
        return f"{self.type.value}: {self.data}\n\n"

    @property
    def emit(self) -> str:
        return str(self)


class Data(Message):
    def __init__(self, data: str):
        super().__init__(type=MessageType.data, data=data)


class NoData(Message):
    def __init__(self):
        super().__init__(type=MessageType.data, data=dumps({"event": "no_state"}))


class Heartbeat(Message):
    def __init__(self):
        super().__init__(type=MessageType.heartbeat, data=dumps({"event": "heartbeat"}))


class QueueRequest(BaseModel):
    url: str


class QueueDeleteRequest(BaseModel):
    title: str


class RuleScheme(BaseModel):
    prefer: Optional[list[str]] = None
    ban: Optional[list[str]] = None


class SongPutRequest(BaseModel):
    url: str
    title: str
    duration: float
    filepath: str
    artwork: str
    pinned: bool = False
    weather: Optional[RuleScheme] = None
    time: Optional[RuleScheme] = None


class SongDeleteRequest(BaseModel):
    identifier: str


class FallbackPutRequest(BaseModel):
    url: str
    title: str
    duration: float
    filepath: str
    artwork: str


class Backend:
    def __init__(self):
        self.state = State(owner=Owner.backend)
        self.song_handler = SongHandler()
        self.shutdown_event = asyncio.Event()

        LOG.debug(
            f"backend initialising: state={self.state} "
            f"song_handler={self.song_handler}"
        )

    def close(self) -> None:
        self.shutdown_event.set()
        self.state._lock.release()
        self.song_handler.database._lock.release()
        LOG.info("backend shutdown")

    async def sse_generator(self) -> AsyncGenerator[str, None]:
        old_snapshot: Optional[StateScheme] = None
        try:
            while not self.shutdown_event.is_set():

                if self.state.state_file.exists():
                    snapshot = self.state.get_snapshot()

                    if old_snapshot != snapshot:
                        old_snapshot = snapshot
                        payload = dumps({"event": "state_update", "state": snapshot})
                        yield Data(payload).emit
                    else:
                        yield Heartbeat().emit
                else:
                    yield NoData().emit

                await asyncio.sleep(1)
        except asyncio.CancelledError:
            LOG.debug("SSE client disconnected")
            raise

    @contextmanager
    def exception_handler(self) -> Generator[None, None, None]:
        try:
            yield
        except HTTPException as e:
            LOG.error(f"exception {e.status_code}: {e}")
            raise
        except DownloadError as e:
            LOG.error(f"Could not download: {e.msg}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Video not found"
            )
        except Exception as e:
            LOG.exception(e, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
            )

    def fetch_song_object(
        self, identifier: str
    ) -> tuple[Song | None, FallbackSong | None]:
        return self.song_handler.get_song(identifier), self.song_handler.get_fallback(
            identifier
        )

    def attach_routes(self, app: FastAPI) -> None:

        # Read only endpoints

        @app.get("/events")
        async def events() -> StreamingResponse:
            return StreamingResponse(
                self.sse_generator(), media_type="text/event-stream"
            )

        @app.get("/state")
        async def get_state() -> JSONResponse:
            with self.exception_handler():
                return JSONResponse(self.state.get_snapshot())

        @app.get("/queue")
        async def get_queue() -> JSONResponse:
            with self.exception_handler():
                return JSONResponse(self.state.get_queue())

        @app.get("/history")
        async def get_history() -> JSONResponse:
            with self.exception_handler():
                return JSONResponse(self.state.get_history())

        @app.get("/current")
        async def get_current() -> JSONResponse:
            with self.exception_handler():
                return JSONResponse(self.state.get_current())

        @app.get("/weather")
        async def get_weather() -> JSONResponse:
            with self.exception_handler():
                return JSONResponse(self.state.get_weather())

        @app.get("/songs")
        async def get_specific_song(
            url: Optional[str] = Query(None), title: Optional[str] = Query(None)
        ) -> JSONResponse:
            if (url is None) ^ (title is not None):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Must provide exactly one of `URL` or `title`",
                )

            with self.exception_handler():
                if url and (song_data := self.song_handler.get_song_by_url(url)):
                    song, main, fallback = song_data
                    return JSONResponse(
                        song.to_dict() | {"main": main, "fallback": fallback}
                    )

                if title and (song_data := self.song_handler.get_song_by_title(title)):
                    song, main, fallback = song_data
                    return JSONResponse(
                        song.to_dict() | {"main": main, "fallback": fallback}
                    )

                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"song {url} not found",
                )

        @app.get("/songs/all")
        async def get_all_songs() -> JSONResponse:
            with self.exception_handler():
                songs = self.song_handler.all_songs
                song_titles = {song.title for song in songs}

                fallback = self.song_handler.all_fallback

                return JSONResponse(
                    {
                        "songs": [s.to_dict() for s in songs],
                        "fallback": [
                            f.to_dict() for f in fallback if f.title not in song_titles
                        ],
                    }
                )

        @app.get("/songs/thumbnail")
        async def get_song_thumbnail(filepath: str = Query(...)) -> Response:
            with self.exception_handler():
                song_path = Path(filepath)
                song, fallback = self.fetch_song_object(identifier=song_path.name)

                if artfile := self.song_handler.find_artfile(
                    song or fallback or song_path
                ):
                    data, mime = self.song_handler.art_data(artfile)
                    return Response(content=data, media_type=mime)

                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No artwork for {song or fallback or song_path.name}",
                )

        @app.get("/songs/main")
        async def get_main_song(identifier: str = Query(...)) -> JSONResponse:
            with self.exception_handler():
                if song := self.song_handler.get_song(identifier):
                    return JSONResponse(song.to_dict())
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"song {identifier} not found",
                )

        @app.get("/songs/fallback")
        async def get_fallback_song(identifier: str = Query(...)) -> JSONResponse:
            with self.exception_handler():
                if song := self.song_handler.get_fallback(identifier):
                    return JSONResponse(song.to_dict())
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"song {identifier} not found",
                )

        # operations

        @app.post("/queue")
        async def add_to_queue(payload: QueueRequest = Body(...)) -> Response:
            with self.exception_handler():
                song, fallback = self.fetch_song_object(payload.url)
                song_object = (
                    song or fallback or self.song_handler.download_song(payload.url)
                )
                try:
                    self.song_handler.play_song(song_object)
                except FileExistsError:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Cannot queue song that already is in the queue",
                    )
                return Response(status_code=status.HTTP_202_ACCEPTED)

        @app.delete("/queue")
        async def delete_from_queue(
            payload: QueueDeleteRequest = Body(...),
        ) -> Response:
            with self.exception_handler():
                if not self.song_handler.remove_from_queue(payload.title):
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND, detail="song not found"
                    )
                return Response(status_code=status.HTTP_204_NO_CONTENT)

        @app.put("/songs/main")
        async def update_song_metadata(payload: SongPutRequest = Body(...)) -> Response:
            with self.exception_handler():
                weather = (
                    RuleSet(prefer=weather.prefer, ban=weather.ban)
                    if (weather := payload.weather)
                    else None
                )
                time = (
                    RuleSet(prefer=time.prefer, ban=time.ban)
                    if (time := payload.time)
                    else None
                )

                if song := self.song_handler.get_song(payload.url):
                    song.pinned = payload.pinned
                    song.weather = weather
                    song.time = time
                    self.song_handler.update_song(song)
                    saved_song = song

                elif fallback := self.song_handler.get_fallback(payload.url):
                    saved_song = self.song_handler.promote_song(
                        fallback,
                        last_played=fallback.last_played,
                        pinned=payload.pinned,
                        weather=weather,
                        time=time,
                    )

                else:
                    song_from_state = SongBase(
                        url=payload.url,
                        title=payload.title,
                        duration=timedelta(seconds=payload.duration),
                        filepath=Path(payload.filepath),
                        artwork=Path(payload.artwork),
                    )
                    saved_song = self.song_handler.promote_song(
                        song_from_state,
                        pinned=payload.pinned,
                        weather=weather,
                        time=time,
                    )

                if saved_song:
                    self.song_handler.ensure(saved_song)

                return Response(status_code=status.HTTP_202_ACCEPTED)

        @app.delete("/songs/main")
        async def delete_from_songs(payload: SongDeleteRequest = Body(...)) -> Response:
            with self.exception_handler():
                if song := self.song_handler.get_song(payload.identifier):
                    self.song_handler.remove_song(song)
                    return Response(status_code=status.HTTP_204_NO_CONTENT)
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="song not found"
                )

        @app.put("/songs/fallback")
        async def update_fallback_metadata(
            payload: FallbackPutRequest = Body(...),
        ) -> Response:
            with self.exception_handler():
                if fallback := self.song_handler.get_fallback(payload.url):
                    saved_song = self.song_handler.update_fallback(fallback)

                elif song := self.song_handler.get_song(payload.url):
                    saved_song = self.song_handler.promote_fallback(
                        song,
                        last_played=song.last_played,
                    )

                else:
                    song_from_state = SongBase(
                        url=payload.url,
                        title=payload.url,
                        duration=timedelta(seconds=payload.duration),
                        filepath=Path(payload.filepath),
                        artwork=Path(payload.artwork),
                    )
                    saved_song = self.song_handler.promote_fallback(
                        song_from_state,
                    )

                if saved_song:
                    self.song_handler.ensure(saved_song)

                return Response(status_code=status.HTTP_202_ACCEPTED)

        @app.delete("/songs/fallback")
        async def delete_from_fallback(
            payload: SongDeleteRequest = Body(...),
        ) -> Response:
            with self.exception_handler():
                if song := self.song_handler.get_fallback(payload.identifier):
                    self.song_handler.remove_fallback(song)
                    return Response(status_code=status.HTTP_204_NO_CONTENT)
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="song not found"
                )


backend = Backend()


@asynccontextmanager
async def lifespan(app: FastAPI):
    LOG.debug("starting backend")
    yield
    LOG.debug("stopping backend")
    backend.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
backend.attach_routes(app)
app.mount("/", StaticFiles(directory="src/ui", html=True), name="static")

if __name__ == "__main__":
    import uvicorn

    LOG.debug("backend starting")
    config = uvicorn.Config(app=app, log_config=None, log_level=INFO, access_log=True)
    server = uvicorn.Server(config=config)
    server.run()
