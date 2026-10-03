"""Environment provider contracts use local HTTP fixtures, never real locations."""

import asyncio
import json
import stat

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import openpin_muse.environment as environment
from openpin_muse.environment import EnvironmentService


WIFI = [{"macAddress": "3c:37:86:5d:75:d4", "signalStrength": -45, "channel": 6},
        {"macAddress": "30:86:2d:c4:29:d0", "signalStrength": -50, "channel": 11}]


async def test_configured_weather_contract_caches_and_labels_location(tmp_path, monkeypatch):
    calls = []

    async def weather(request):
        calls.append(dict(request.query))
        return web.json_response({"current": {"temperature_2m": 68.4, "weather_code": 2}})

    app = web.Application()
    app.router.add_get("/weather", weather)
    async with TestServer(app) as server:
        monkeypatch.setattr(environment, "WEATHER_URL", str(server.make_url("/weather")))
        service = EnvironmentService(tmp_path, latitude=40, longitude=-70, location_name="Fixture Town")
        try:
            assert await service.home_data() == {"location": "Fixture Town (configured)",
                                                 "temp": "68°F", "conditions": "cloudy"}
            assert await service.home_data() == await service.home_data()
            assert len(calls) == 1
            assert calls[0]["current"] == "temperature_2m,weather_code"
            assert calls[0]["temperature_unit"] == "fahrenheit"
            context = await service.context()
            assert context["location"]["source"] == "configured"
            assert context["weather"]["temperature"] == 68.4
            with pytest.raises(ConnectionError):
                await service.locate(WIFI)
        finally:
            await service.close()


async def test_wifi_contract_persists_private_fix_without_ip_fallback(tmp_path, monkeypatch):
    requests = []

    async def locate(request):
        requests.append((dict(request.query), await request.json()))
        return web.json_response({"location": {"lat": 40, "lng": -70}, "accuracy": 42.5})

    async def geocode(request):
        assert request.query["latlng"] == "40.0,-70.0"
        assert request.query["result_type"] == "locality"
        return web.json_response({"status": "OK", "results": [{"address_components": [
            {"long_name": "Fixture City", "types": ["locality", "political"]}]}]})

    async def weather(request):
        return web.json_response({"current": {"temperature_2m": 20, "weather_code": 0}})

    app = web.Application()
    app.router.add_post("/locate", locate)
    app.router.add_get("/geocode", geocode)
    app.router.add_get("/weather", weather)
    async with TestServer(app) as server:
        monkeypatch.setattr(environment, "GEOLOCATION_URL", str(server.make_url("/locate")))
        monkeypatch.setattr(environment, "GEOCODING_URL", str(server.make_url("/geocode")))
        monkeypatch.setattr(environment, "WEATHER_URL", str(server.make_url("/weather")))
        service = EnvironmentService(tmp_path, google_api_key="fixture-key", temperature_unit="celsius")
        try:
            expected = {"location": {"lat": 40, "lng": -70}, "accuracy": 42.5}
            assert await service.locate(WIFI) == expected
            assert await service.locate(WIFI) == expected
            assert len(requests) == 1
            assert requests[0] == ({"key": "fixture-key"}, {"considerIp": False, "wifiAccessPoints": WIFI})
            assert await service.home_data() == {"location": "Fixture City", "temp": "20°C", "conditions": "sunny"}
            assert "fixture-key" not in (tmp_path / "location.json").read_text()
            assert stat.S_IMODE((tmp_path / "location.json").stat().st_mode) == 0o600
        finally:
            await service.close()
    restarted = EnvironmentService(tmp_path)
    try:
        assert restarted._location["source"] == "wifi"
        assert restarted._location["latitude"] == 40
    finally:
        await restarted.close()


async def test_pin_update_drops_configured_name_and_persists(tmp_path):
    service = EnvironmentService(tmp_path, latitude=40, longitude=-70, location_name="Fixture Home")
    await service.update_coordinates(41, -71)
    assert service._location["source"] == "pin"
    assert service._location["name"] is None
    await service.close()
    restarted = EnvironmentService(tmp_path)
    assert restarted._location["latitude"] == 41
    await restarted.close()


@pytest.mark.parametrize("latitude,longitude", [(91, 0), (0, -181), (float("nan"), 0), (True, 0), (0, None)])
def test_invalid_configuration_is_rejected(tmp_path, latitude, longitude):
    with pytest.raises(ValueError):
        EnvironmentService(tmp_path, latitude=latitude, longitude=longitude)


@pytest.mark.parametrize("wifi", [None, {}, [{}], [{"macAddress": "bad"}], WIFI * 51])
async def test_invalid_wifi_is_rejected_without_provider_request(tmp_path, wifi):
    service = EnvironmentService(tmp_path, google_api_key="fixture-key")
    with pytest.raises(ValueError):
        await service.locate(wifi)
    await service.close()


async def test_weather_provider_failure_leaves_location_available_and_is_cached(tmp_path, monkeypatch):
    calls = []

    async def weather(request):
        calls.append(True)
        return web.Response(status=503, text="provider's internal error")

    app = web.Application()
    app.router.add_get("/weather", weather)
    async with TestServer(app) as server:
        monkeypatch.setattr(environment, "WEATHER_URL", str(server.make_url("/weather")))
        service = EnvironmentService(tmp_path, latitude=40, longitude=-70)
        try:
            assert set(await service.home_data()) == {"location"}
            assert set(await service.home_data()) == {"location"}
            assert len(calls) == 1
        finally:
            await service.close()


@pytest.mark.parametrize("code,condition", [(0, "sunny"), (1, "sunny"), (3, "cloudy"),
    (45, "cloudy"), (61, "rainy"), (67, "rainy"), (71, "snow"), (86, "snow"),
    (95, "thunderstorm"), (97, "thunderstorm"), (99, "thunderstorm"), (999, None)])
def test_weather_code_mapping(code, condition):
    assert environment.weather_condition(code) == condition


@pytest.mark.parametrize("body", [{"location": {"lat": 91, "lng": 0}, "accuracy": 10},
    {"location": {"lat": 0, "lng": 0}, "accuracy": -1}, [], {"error": "fixture failure"}])
async def test_bad_location_provider_data_is_not_persisted(tmp_path, monkeypatch, body):
    async def locate(request):
        return web.json_response(body)

    app = web.Application()
    app.router.add_post("/locate", locate)
    async with TestServer(app) as server:
        monkeypatch.setattr(environment, "GEOLOCATION_URL", str(server.make_url("/locate")))
        service = EnvironmentService(tmp_path, google_api_key="fixture-key")
        try:
            with pytest.raises(ConnectionError):
                await service.locate(WIFI)
            assert not (tmp_path / "location.json").exists()
        finally:
            await service.close()


async def test_wifi_filters_nonlocatable_nodes_and_coalesces_requests(tmp_path, monkeypatch):
    requests = []

    async def locate(request):
        requests.append(await request.json())
        return web.json_response({"location": {"lat": 40, "lng": -70}, "accuracy": 42.5})

    app = web.Application()
    app.router.add_post("/locate", locate)
    async with TestServer(app) as server:
        monkeypatch.setattr(environment, "GEOLOCATION_URL", str(server.make_url("/locate")))
        service = EnvironmentService(tmp_path, google_api_key="fixture-key")
        try:
            scan = WIFI + [{"macAddress": "ff:ff:ff:ff:ff:ff"}, {"macAddress": "02:00:00:00:00:00"},
                           {"macAddress": "00:00:5e:00:00:01"}, WIFI[0]]
            first, second = await asyncio.gather(service.locate(scan), service.locate(scan))
            assert first == second
            assert requests == [{"considerIp": False, "wifiAccessPoints": WIFI}]
            with pytest.raises(ConnectionError, match="cooling down"):
                await service.locate([WIFI[0], {"macAddress": "3c:37:86:5d:75:d5"}])
        finally:
            await service.close()


@pytest.mark.parametrize("body", [b"{}" * 40000, b"malformed json",
    b'{"current":{"temperature_2m":NaN,"weather_code":0}}'])
async def test_malformed_or_oversized_weather_keeps_home_available(tmp_path, monkeypatch, body):
    async def weather(request):
        return web.Response(body=body, content_type="application/json")

    app = web.Application()
    app.router.add_get("/weather", weather)
    async with TestServer(app) as server:
        monkeypatch.setattr(environment, "WEATHER_URL", str(server.make_url("/weather")))
        service = EnvironmentService(tmp_path, latitude=0, longitude=0)
        try:
            assert await service.home_data() == {"location": "0.000, 0.000 (configured)"}
        finally:
            await service.close()


async def test_unknown_location_never_calls_a_provider(tmp_path):
    service = EnvironmentService(tmp_path)
    assert await service.home_data() == {}
    assert await service.context() == {"location": None, "weather": None, "wifi_location_enabled": False}
    assert service._session is None
    await service.close()


def test_state_rejects_insecure_and_invalid_content(tmp_path):
    path = tmp_path / "location.json"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        EnvironmentService(tmp_path)
    path.chmod(0o600)
    path.write_text(json.dumps({"source": "invented"}))
    with pytest.raises(ValueError, match="Invalid location state"):
        EnvironmentService(tmp_path)
