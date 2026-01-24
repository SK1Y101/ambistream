from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum, auto
from typing import Any, Optional

import requests

from .logging import get_logger
from .state import Owner, State, Status
from .state import Weather as StateWeather

LOG = get_logger("weather")


class TimePeriod(Enum):
    dawn = auto()
    morning = auto()
    day = auto()
    dusk = auto()
    evening = auto()
    night = auto()


@dataclass
class WeatherVector:
    intensity: float = 0.0
    obscurity: float = 0.0
    volatility: float = 0.0


class WeatherType(Enum):
    clear = WeatherVector(0.00, 0.00, 0.00)
    clouds = WeatherVector(0.05, 0.20, 0.05)
    rain = WeatherVector(0.70, 0.40, 0.25)
    drizzle = WeatherVector(0.40, 0.30, 0.15)
    thunderstorm = WeatherVector(0.90, 0.60, 0.80)
    snow = WeatherVector(0.60, 0.50, 0.30)
    mist = WeatherVector(0.15, 0.70, 0.10)
    smoke = WeatherVector(0.20, 0.75, 0.40)
    haze = WeatherVector(0.10, 0.65, 0.05)
    dust = WeatherVector(0.30, 0.70, 0.45)
    fog = WeatherVector(0.15, 0.75, 0.10)
    sand = WeatherVector(0.35, 0.75, 0.50)
    ash = WeatherVector(0.25, 0.80, 0.55)
    squall = WeatherVector(0.85, 0.65, 0.85)
    tornado = WeatherVector(1.00, 0.80, 1.00)

    # debug only, never used elsewise
    min = WeatherVector(0, 0, 0)
    max = WeatherVector(1, 1, 1)


def time_distance(a: TimePeriod, b: TimePeriod) -> float:
    return min(abs(a.value - b.value), abs(a.value + len(TimePeriod) - b.value)) / len(
        TimePeriod
    )


def weather_distance(a: WeatherType, b: WeatherType) -> float:
    return (
        (a.value.intensity - b.value.intensity) ** 2
        + (a.value.obscurity - b.value.obscurity) ** 2
        + (a.value.volatility - b.value.volatility) ** 2
    ) ** 0.5


def get_time(time: str) -> TimePeriod:
    return TimePeriod.__members__.get(time.lower(), TimePeriod.day)


def get_weather(name: str) -> WeatherType:
    return WeatherType.__members__.get(name.lower().split()[0], WeatherType.clear)


MAX_WEATHER_DIST = weather_distance(WeatherType.min, WeatherType.max)
MAX_TIME_DIST = len(TimePeriod) / 2


@dataclass
class Weather:
    _weather: str
    description: str
    temperature: float
    sunrise: float
    sunset: float

    @property
    def time(self) -> TimePeriod:
        now = datetime.now().astimezone()
        sunrise = self.local_sunrise
        sunset = self.local_sunset
        midday = self.local_midday

        if now <= sunrise - timedelta(minutes=30):
            return TimePeriod.night
        elif now <= sunrise + timedelta(minutes=30):
            return TimePeriod.dawn
        elif now <= midday:
            return TimePeriod.morning
        elif now <= sunset - timedelta(minutes=30):
            return TimePeriod.day
        elif now <= sunset + timedelta(minutes=30):
            return TimePeriod.dusk
        elif now <= sunset + timedelta(hours=2):
            return TimePeriod.evening
        return TimePeriod.night

    @property
    def local_sunrise(self) -> datetime:
        return datetime.fromtimestamp(self.sunrise, timezone.utc).astimezone()

    @property
    def local_sunset(self) -> datetime:
        return datetime.fromtimestamp(self.sunset, timezone.utc).astimezone()

    @property
    def local_midday(self) -> datetime:
        return self.local_sunrise + (self.local_sunset - self.local_sunrise) / 2

    @property
    def weather(self) -> WeatherType:
        return get_weather(self._weather)

    @property
    def summary(self) -> str:
        return f"{self.time.value} {self.weather.name}"


class WeatherHandler:
    def __init__(
        self,
        api_key: str,
        latitude: float = 51.5073219,
        longitude: float = -0.1276474,
        expiry_seconds: int = 30,
    ):
        self.state = State(owner=Owner.weather)
        self.state.set_status(status=Status.startup)

        self.api_key = api_key
        self.lat = latitude
        self.lon = longitude

        self._expiry: timedelta = timedelta(seconds=expiry_seconds)
        self._fetched: Optional[datetime] = None
        self._expires = datetime.now() - timedelta(seconds=1) - self._expiry

        self._weather: Weather = Weather(
            _weather="Clear sky",
            description="Default weather",
            temperature=21.0,
            sunrise=datetime.combine(datetime.now().date(), time(6, 0, 0)).timestamp(),
            sunset=datetime.combine(datetime.now().date(), time(18, 0, 0)).timestamp(),
        )

        LOG.debug(
            f"weatherHandler initialised: lat={self.lat:.5f} "
            f"lon={self.lon:.5f} expiry={int(self._expiry.total_seconds())}"
        )
        self.state.set_status(status=Status.running)

    def fetch_api_data(self, url: str) -> dict[str | int, Any] | None:
        try:
            response = requests.get(url)
            response.raise_for_status()
            LOG.debug(f"fetched data from {url}")
            return response.json()
        except requests.RequestException as e:
            LOG.warning(f"Error fetching data from {url}: {e}")
            return None

    def latlon_from_city(
        self, city: str, country: str = ""
    ) -> tuple[float, float] | None:
        place = city
        if country:
            place += f",{country}"

        if loc := self.fetch_api_data(
            "http://api.openweathermap.org/geo/1.0/direct?"
            f"q={place}&limit=1&appid={self.api_key}"
        ):
            lat, lon = loc[0]["lat"], loc[0]["lon"]
            LOG.debug(f"Resolved city '{city}' to lat={lat:.5f} lon={lon:.5f}")
            return lat, lon

        LOG.warning(f"could not resolve city '{city}'")

        return None

    def update_latlon(self, city: str, country: str = "") -> None:
        place = city
        if country:
            place += f",{country}"

        if loc := self.latlon_from_city(city, country):
            self.lat, self.lon = loc
            LOG.debug(f"updated location to lat={self.lat:.5f} lon={self.lon:.5f}")
            return None

        raise ValueError(f"Could not find a valid location '{place}'")

    def push_weather_to_state(self, weather: Weather) -> None:
        self.state.set_weather(
            StateWeather(
                condition=weather._weather,
                description=weather.description,
                temperature=weather.temperature,
                sunrise=weather.local_sunrise,
                sunset=weather.local_sunset,
                midday=weather.local_midday,
                timeperiod=weather.time.name,
                fetched_at=self._fetched or datetime.now(),
                valid_until=self._expires,
            )
        )

    @property
    def weather(self) -> Weather:
        if weather := self._weather:
            if datetime.now() <= self._expires:
                self.push_weather_to_state(weather)
                LOG.debug(
                    f"Using cached weather, expires at {self._expires.isoformat()}"
                )
                return weather

        if data := self.fetch_api_data(
            "https://api.openweathermap.org/data/2.5/weather?"
            f"lat={self.lat}&lon={self.lon}&appid={self.api_key}"
            "&units=metric"
        ):
            self._fetched = datetime.now()
            self._expires = self._fetched + self._expiry
            self._weather = Weather(
                _weather=data["weather"][0]["main"],
                description=data["weather"][0]["description"],
                temperature=data["main"]["temp"],
                sunrise=data["sys"]["sunrise"],
                sunset=data["sys"]["sunset"],
            )
            self.push_weather_to_state(self._weather)
            LOG.debug(
                f"Updated weather: {self._weather._weather} "
                f"{self._weather.temperature:.1f}°C "
                f"(expires at {self._expires.isoformat()})"
            )
        else:
            LOG.warning(
                f"Failed to update weather, using last known: {self._weather._weather}"
            )
        return self._weather

    def close(self) -> None:
        self.state.set_status(Status.shutdown)

    def error(self) -> None:
        self.state.set_status(Status.error)
