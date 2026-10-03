"""Location and weather for OpenPin; no companion-IP location guesses.

Only Google Wi-Fi geolocation produces a /locate fix with an accuracy radius.
Configured coordinates provide explicitly labelled home weather. Provider calls
are bounded and cached; coordinates, never Wi-Fi scans or API keys, are saved.
"""

import asyncio
from contextlib import suppress
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time

import aiohttp


GEOLOCATION_URL = "https://www.googleapis.com/geolocation/v1/geolocate"
GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
MAX_PROVIDER_BYTES = 64 * 1024
WEATHER_CACHE_SECONDS = 600
LOCATION_CACHE_SECONDS = 60
LOCATION_STALE_SECONDS = 1800
PROVIDER_RETRY_SECONDS = 60


def _number(value, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("Invalid numeric location or weather value")
    return float(value)


def _name(value) -> str | None:
    if value is None:
        return None
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 100
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("Location name must contain 1–100 printable characters")
    return value.strip()


def weather_condition(code: int) -> str | None:
    """Map Open-Meteo WMO codes to the five OpenPin weather icons."""
    if type(code) is not int:
        return None
    groups = {"sunny": (0, 1), "cloudy": (2, 3, 45, 48),
              "rainy": (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82),
              "snow": (71, 73, 75, 77, 85, 86), "thunderstorm": (95, 96, 97, 99)}
    return next((name for name, codes in groups.items() if code in codes), None)


def _wifi_points(points: list) -> list[dict]:
    if not isinstance(points, list) or not 1 <= len(points) <= 100:
        raise ValueError("Wi-Fi scan must contain 1–100 access points")
    usable = {}
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("Invalid Wi-Fi access point")
        mac = point.get("macAddress")
        if not isinstance(mac, str) or re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac) is None:
            raise ValueError("Invalid Wi-Fi MAC address")
        mac = mac.lower()
        value = {"macAddress": mac}
        if "signalStrength" in point:
            value["signalStrength"] = _number(point["signalStrength"], -128, -10)
        if "channel" in point:
            channel = point["channel"]
            if type(channel) is not int or not 1 <= channel <= 233:
                raise ValueError("Invalid Wi-Fi channel")
            value["channel"] = channel
        # Google cannot locate locally administered, multicast, or reserved MACs.
        if int(mac[:2], 16) & 3 or mac.startswith("00:00:5e:") or mac == "00:00:00:00:00:00":
            continue
        usable[mac] = value
    if len(usable) < 2:
        raise ConnectionError("At least two usable Wi-Fi access points are needed for location")
    return list(usable.values())


class EnvironmentService:
    def __init__(self, state_dir: Path, *, latitude=None, longitude=None,
                 location_name=None, google_api_key=None, temperature_unit="fahrenheit"):
        if temperature_unit not in ("fahrenheit", "celsius"):
            raise ValueError("temperature_unit must be fahrenheit or celsius")
        name = _name(location_name)
        if (latitude is None) != (longitude is None) or (name is not None and latitude is None):
            raise ValueError("Configured location requires latitude and longitude")
        configured = None
        if latitude is not None:
            configured = {"latitude": _number(latitude, -90, 90),
                          "longitude": _number(longitude, -180, 180),
                          "accuracy": None, "source": "configured", "name": name,
                          "updated_at": time.time()}
        if google_api_key is not None and (not isinstance(google_api_key, str)
                or not 1 <= len(google_api_key) <= 256 or any(char.isspace() for char in google_api_key)):
            raise ValueError("Invalid Google API key")
        self._directory = Path(state_dir)
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._directory.is_symlink() or not self._directory.is_dir():
            raise ValueError("State directory must be a real directory")
        self._directory.chmod(0o700)
        self._path = self._directory / "location.json"
        self._location = self._read()
        if configured is not None:
            self._location = configured
            self._write(configured)
        self._key = google_api_key
        self.temperature_unit = temperature_unit
        self._session = None
        self._lock = asyncio.Lock()
        self._weather = None
        self._weather_after = 0.0
        self._geocode_after = 0.0
        self._locate_after = 0.0
        self._scan = None
        self._fix = None

    def _read(self) -> dict | None:
        try:
            descriptor = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("Location state must be a private regular file")
            data = stream.read(4097)
        try:
            if len(data) > 4096:
                raise ValueError("Location state exceeds size limit")
            value = json.loads(data)
            if value["source"] not in ("configured", "wifi", "pin"):
                raise ValueError("Invalid location source")
            return {"latitude": _number(value["latitude"], -90, 90),
                    "longitude": _number(value["longitude"], -180, 180),
                    "accuracy": None if value["accuracy"] is None else _number(value["accuracy"], 0, 5000000),
                    "source": value["source"],
                    "name": _name(value["name"]),
                    "updated_at": _number(value["updated_at"], 0, time.time() + 60)}
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid location state") from exc

    def _write(self, location: dict) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".location-", dir=self._directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(location, stream, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    async def _json(self, method: str, url: str, **kwargs) -> dict:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        try:
            async with self._session.request(method, url, allow_redirects=False, **kwargs) as response:
                if response.status != 200:
                    raise ConnectionError("Location or weather provider is unavailable")
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > MAX_PROVIDER_BYTES:
                        raise ConnectionError("Location or weather provider response is too large")
                value = json.loads(body)
                if not isinstance(value, dict):
                    raise ValueError("Expected provider JSON object")
                return value
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise ConnectionError("Location or weather provider is unavailable") from exc

    async def _set_location(self, latitude, longitude, source, accuracy=None) -> None:
        previous = self._location
        same = previous is not None and (previous["latitude"], previous["longitude"]) == (latitude, longitude)
        name = previous["name"] if same and previous["source"] != "configured" else None
        self._location = {"latitude": latitude, "longitude": longitude, "accuracy": accuracy,
                          "source": source, "name": name, "updated_at": time.time()}
        await asyncio.to_thread(self._write, self._location)
        if not same:
            self._weather = None
            self._weather_after = self._geocode_after = 0.0

    async def update_coordinates(self, latitude, longitude) -> None:
        latitude = _number(latitude, -90, 90)
        longitude = _number(longitude, -180, 180)
        async with self._lock:
            await self._set_location(latitude, longitude, "pin")

    async def locate(self, wifi_access_points: list) -> dict:
        points = _wifi_points(wifi_access_points)
        if not self._key:
            raise ConnectionError("Wi-Fi location requires a Google Geolocation API key")
        scan = frozenset(point["macAddress"] for point in points)
        async with self._lock:
            if time.monotonic() < self._locate_after:
                if scan == self._scan and self._fix is not None:
                    return {"location": dict(self._fix["location"]), "accuracy": self._fix["accuracy"]}
                raise ConnectionError("Location lookup is cooling down; retry in one minute")
            self._locate_after = time.monotonic() + LOCATION_CACHE_SECONDS
            self._scan, self._fix = scan, None
            value = await self._json("POST", GEOLOCATION_URL, params={"key": self._key},
                                     json={"considerIp": False, "wifiAccessPoints": points})
            try:
                latitude = _number(value["location"]["lat"], -90, 90)
                longitude = _number(value["location"]["lng"], -180, 180)
                accuracy = _number(value["accuracy"], 0, 5000000)
            except (KeyError, TypeError, ValueError) as exc:
                raise ConnectionError("Location provider returned an invalid fix") from exc
            await self._set_location(latitude, longitude, "wifi", accuracy)
            self._fix = {"location": {"lat": latitude, "lng": longitude}, "accuracy": accuracy}
            return {"location": dict(self._fix["location"]), "accuracy": accuracy}

    async def _weather_data(self, location: dict) -> dict | None:
        try:
            value = await self._json("GET", WEATHER_URL, params={"latitude": location["latitude"],
                "longitude": location["longitude"], "current": "temperature_2m,weather_code",
                "temperature_unit": self.temperature_unit, "forecast_days": 1})
            current = value["current"]
            temperature = _number(current["temperature_2m"], -240, 320)
            condition = weather_condition(current["weather_code"])
            return {"temperature": temperature, "unit": self.temperature_unit, "conditions": condition,
                    "provider": "Open-Meteo", "fetched_at": time.time()}
        except (ConnectionError, KeyError, TypeError, ValueError):
            return None

    async def _location_name(self, location: dict) -> str | None:
        try:
            value = await self._json("GET", GEOCODING_URL, params={"key": self._key,
                "latlng": f"{location['latitude']},{location['longitude']}", "result_type": "locality"})
            if value.get("status") != "OK":
                return None
            for result in value["results"]:
                for component in result["address_components"]:
                    if "locality" in component["types"]:
                        return _name(component["long_name"])
        except (ConnectionError, KeyError, TypeError, ValueError):
            pass
        return None

    async def home_data(self) -> dict:
        async with self._lock:
            location = self._location
            if location is None:
                return {}
            now = time.monotonic()
            weather_due = now >= self._weather_after
            name_due = self._key and not location["name"] and now >= self._geocode_after
            jobs = []
            if weather_due:
                jobs.append(self._weather_data(location))
            if name_due:
                jobs.append(self._location_name(location))
            results = iter(await asyncio.gather(*jobs))
            if weather_due:
                self._weather = next(results)
                self._weather_after = time.monotonic() + (WEATHER_CACHE_SECONDS if self._weather else PROVIDER_RETRY_SECONDS)
            if name_due:
                location["name"] = next(results)
                self._geocode_after = time.monotonic() + 3600
                if location["name"]:
                    await asyncio.to_thread(self._write, location)
            label = location["name"] or f"{location['latitude']:.3f}, {location['longitude']:.3f}"
            if location["source"] == "configured":
                label += " (configured)"
            elif time.time() - location["updated_at"] > LOCATION_STALE_SECONDS:
                label += " (last known)"
            result = {"location": label}
            if self._weather:
                unit = "F" if self.temperature_unit == "fahrenheit" else "C"
                result["temp"] = f"{round(self._weather['temperature'])}°{unit}"
                if self._weather["conditions"]:
                    result["conditions"] = self._weather["conditions"]
            return result

    async def context(self) -> dict:
        home = await self.home_data()
        location = dict(self._location) if self._location else None
        if location:
            location["display_name"] = home["location"]
            location["stale"] = (location["source"] != "configured"
                                  and time.time() - location["updated_at"] > LOCATION_STALE_SECONDS)
        return {"location": location, "weather": dict(self._weather) if self._weather else None,
                "wifi_location_enabled": bool(self._key)}

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
