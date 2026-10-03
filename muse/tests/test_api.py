"""HTTP contract checks; StubBridge replaces hardware and media conversion."""

import asyncio
import json
import stat
from urllib.parse import urlsplit

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import openpin_muse.api as api
from openpin_muse.api import create_app, issue_pairing, validate_public_url
from openpin_muse.protocol import HEADER_SIZE, MAX_AUDIO_BYTES


class StubBridge:
    ready = True

    def __init__(self):
        self.voices = []
        self.captures = []

    async def voice(self, audio, image, translate):
        self.voices.append((audio, image, translate))
        return b"ID3synthetic response fixture"

    async def capture(self, data, mime_type):
        self.captures.append((data, mime_type))


def voice_packet(device_id, **changes):
    audio = b"OggS\x00synthetic audio fixture"
    image = b"\xff\xd8\xff\xe0synthetic jpeg fixture\xff\xd9"
    metadata = {"deviceId": device_id, "audioSize": len(audio),
                "imageSize": len(image), "audioFormat": "ogg",
                "audioBitrate": "64k", "battery": 0.5}
    metadata.update(changes)
    return (json.dumps(metadata).encode() + b"\0").ljust(HEADER_SIZE, b"\0") + image + audio


async def paired_client(tmp_path, bridge=None):
    bridge = bridge or StubBridge()
    url = issue_pairing(tmp_path, "https://pin.example")
    client = TestClient(TestServer(create_app(bridge, tmp_path, public_url="https://pin.example")))
    await client.start_server()
    response = await client.post(urlsplit(url).path)
    assert response.status == 200
    details = await response.json()
    assert details["baseUrl"] == "https://pin.example"
    return client, bridge, details["deviceId"]


@pytest.mark.asyncio
async def test_pairing_is_post_only_one_use_private_and_survives_restart(tmp_path):
    url = issue_pairing(tmp_path, "https://pin.example/")
    path = urlsplit(url).path
    bridge = StubBridge()
    async with TestClient(TestServer(create_app(bridge, tmp_path, public_url="https://pin.example"))) as client:
        assert (await client.get(path)).status == 405
        responses = await asyncio.gather(client.post(path), client.post(path))
        assert sorted(r.status for r in responses) == [200, 404]
        details = await next(r for r in responses if r.status == 200).json()
        assert set(details) == {"baseUrl", "deviceId"}
        assert len(details["deviceId"]) >= 32
    async with TestClient(TestServer(create_app(bridge, tmp_path, public_url="https://pin.example"))) as client:
        assert (await client.post(path)).status == 404
        assert (await client.post("/api/dev/home-data", json={"deviceId": details["deviceId"]})).status == 200
    for file in tmp_path.iterdir():
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
        assert path.rsplit("/", 1)[-1] not in file.read_text()
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_expired_unknown_and_replaced_pairing_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: 1000)
    expired = issue_pairing(tmp_path, "https://pin.example", ttl_seconds=1)
    monkeypatch.setattr(api.time, "time", lambda: 1002)
    async with TestClient(TestServer(create_app(StubBridge(), tmp_path, public_url="https://pin.example"))) as client:
        assert (await client.post(urlsplit(expired).path)).status == 404
        assert (await client.post("/api/dev/pair/unknown")).status == 404
        old = issue_pairing(tmp_path, "https://pin.example")
        new = issue_pairing(tmp_path, "https://pin.example")
        assert (await client.post(urlsplit(old).path)).status == 404
        assert (await client.post(urlsplit(new).path)).status == 200


@pytest.mark.parametrize("url", ["http://pin.example", "http://192.168.1.10:8080", "ftp://localhost",
                                "https://u:p@pin.example", "https://pin.example/path", "https://pin.example?x=1",
                                "https://pin.example#fragment", "https://", "https://pin.example:bad"])
def test_public_url_rejects_insecure_or_ambiguous_addresses(url):
    with pytest.raises(ValueError):
        validate_public_url(url)


@pytest.mark.parametrize("url", ["https://pin.example", "http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"])
def test_supported_public_urls(url):
    assert validate_public_url(url + "/") == url


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,translate", [("handle", False), ("translate", True)])
async def test_android_voice_contract(tmp_path, endpoint, translate):
    client, bridge, device_id = await paired_client(tmp_path)
    async with client:
        response = await client.post(f"/api/dev/{endpoint}", data=voice_packet(device_id))
        assert response.status == 200
        body = await response.read()
        assert json.loads(body[:512].rstrip(b"\0"))["disabled"] is False
        assert body[512:] == b"ID3synthetic response fixture"
        assert bridge.voices == [(b"OggS\x00synthetic audio fixture",
                                 b"\xff\xd8\xff\xe0synthetic jpeg fixture\xff\xd9", translate)]


@pytest.mark.asyncio
async def test_rejects_unauthorized_malformed_and_oversize_before_bridge(tmp_path):
    client, bridge, device_id = await paired_client(tmp_path)
    async with client:
        assert (await client.post("/api/dev/handle", data=voice_packet("incorrect"))).status == 401
        assert (await client.post("/api/dev/translate", data=voice_packet(device_id)[:-1])).status == 400
        assert (await client.post("/api/dev/handle", data=voice_packet(device_id) + b"extra")).status == 400
        assert (await client.post("/api/dev/handle", data=b"short")).status == 400
        oversized = voice_packet(device_id, audioSize=MAX_AUDIO_BYTES + 1)[:HEADER_SIZE]
        assert (await client.post("/api/dev/handle", data=oversized)).status == 413
        assert (await client.post("/api/dev/home-data", json={"deviceId": "incorrect"})).status == 401
        assert (await client.post("/api/dev/locate", json={"deviceId": "incorrect"})).status == 401
    assert bridge.voices == []
    assert bridge.captures == []


@pytest.mark.asyncio
async def test_chunked_voice_is_bounded_and_checks_exact_length(tmp_path):
    client, bridge, device_id = await paired_client(tmp_path)
    async def chunks(body):
        for start in range(0, len(body), 19):
            yield body[start:start + 19]
    async with client:
        assert (await client.post("/api/dev/handle", data=chunks(voice_packet(device_id)))).status == 200
        assert (await client.post("/api/dev/handle", data=chunks(voice_packet(device_id) + b"extra"))).status == 400
    assert len(bridge.voices) == 1


@pytest.mark.asyncio
async def test_home_locate_and_safe_health(tmp_path, monkeypatch):
    client, bridge, device_id = await paired_client(tmp_path)
    monkeypatch.setattr(api.time, "time", lambda: 1234.5)
    async with client:
        response = await client.post("/api/dev/home-data", json={"deviceId": device_id})
        assert await response.json() == {"time": 1234500}
        response = await client.post("/api/dev/locate", json={"deviceId": device_id, "wifiAccessPoints": []})
        assert response.status == 503
        response = await client.get("/healthz")
        assert response.status == 200
        assert await response.json() == {"ready": True}
        bridge.ready = False
        assert (await client.get("/healthz")).status == 503
        assert (await client.post("/api/dev/handle", data=voice_packet(device_id))).status == 503
    assert bridge.voices == []


@pytest.mark.asyncio
async def test_home_clock_uses_selected_zone_wall_time(tmp_path, monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: 1790956800)  # 2026-10-02 16:00 UTC.
    # Pair at the same fixed instant so the code remains unexpired.
    url = issue_pairing(tmp_path, "https://pin.example")
    app = create_app(StubBridge(), tmp_path, public_url="https://pin.example", time_zone="America/Los_Angeles")
    async with TestClient(TestServer(app)) as client:
        details = await (await client.post(urlsplit(url).path)).json()
        response = await client.post("/api/dev/home-data", json={"deviceId": details["deviceId"]})
        assert await response.json() == {"time": (1790956800 - 7 * 3600) * 1000}


@pytest.mark.asyncio
async def test_new_pairing_rotates_existing_device_credential(tmp_path):
    client, _, old_device = await paired_client(tmp_path)
    async with client:
        url = issue_pairing(tmp_path, "https://pin.example")
        # Issuance alone leaves the currently paired device usable.
        assert (await client.post("/api/dev/home-data", json={"deviceId": old_device})).status == 200
        details = await (await client.post(urlsplit(url).path)).json()
        assert details["deviceId"] != old_device
        assert (await client.post("/api/dev/home-data", json={"deviceId": old_device})).status == 401
        assert (await client.post("/api/dev/home-data", json={"deviceId": details["deviceId"]})).status == 200


def capture_form(device_id, data, filename, content_type="application/octet-stream"):
    form = FormData()
    form.add_field("deviceId", device_id)
    form.add_field("file", data, filename=filename, content_type=content_type)
    return form


@pytest.mark.asyncio
@pytest.mark.parametrize("data,filename,mime", [(b"\xff\xd8\xff\xe0fixture\xff\xd9", "capture.JPG", "image/jpeg"),
                                              (b"\x00\x00\x00\x18ftypisomfixture", "capture.mp4", "video/mp4")])
async def test_capture_multipart_compatible_with_android(tmp_path, data, filename, mime):
    client, bridge, device_id = await paired_client(tmp_path)
    async with client:
        response = await client.post("/api/dev/upload-capture", data=capture_form(device_id, data, filename))
        assert response.status == 200
    assert bridge.captures == [(data, mime)]


@pytest.mark.asyncio
async def test_capture_auth_types_limits_and_duplicate_fields(tmp_path, monkeypatch):
    client, bridge, device_id = await paired_client(tmp_path)
    monkeypatch.setattr(api, "MAX_CAPTURE_BYTES", 100)
    async with client:
        assert (await client.post("/api/dev/upload-capture", data=capture_form("bad", b"\xff\xd8\xfffile", "a.jpg"))).status == 401
        assert (await client.post("/api/dev/upload-capture", data=capture_form(device_id, b"not jpeg", "a.jpg"))).status == 400
        assert (await client.post("/api/dev/upload-capture", data=capture_form(device_id, b"PNG", "a.png"))).status == 400
        assert (await client.post("/api/dev/upload-capture", data=capture_form(device_id, b"x" * 101, "a.jpg"))).status == 413
        form = capture_form(device_id, b"\xff\xd8\xfffile", "a.jpg")
        form.add_field("deviceId", device_id)
        assert (await client.post("/api/dev/upload-capture", data=form)).status == 400
    assert bridge.captures == []


@pytest.mark.asyncio
async def test_bridge_failures_do_not_expose_secrets(tmp_path):
    class FailingBridge(StubBridge):
        async def voice(self, *args, **kwargs):
            raise RuntimeError("SECRET UPSTREAM TOKEN")
    client, _, device_id = await paired_client(tmp_path, FailingBridge())
    async with client:
        response = await client.post("/api/dev/handle", data=voice_packet(device_id))
        assert response.status == 502
        assert "SECRET" not in await response.text()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,status", [(ValueError("bad codec"), 400),
                                           (ConnectionError("offline"), 503),
                                           (TimeoutError("timeout"), 504)])
async def test_bridge_failure_statuses(tmp_path, failure, status):
    class FailingBridge(StubBridge):
        async def voice(self, *args, **kwargs):
            raise failure
    client, _, device_id = await paired_client(tmp_path, FailingBridge())
    async with client:
        response = await client.post("/api/dev/handle", data=voice_packet(device_id))
        assert response.status == status


async def test_environment_and_remote_commands_are_authenticated(tmp_path):
    from openpin_muse.controls import PinControls
    class Environment:
        async def home_data(self):
            return {"location": "Fixture city", "temp": "20 C", "conditions": "sunny"}
        async def locate(self, entries):
            assert entries == [{"fixture": True}]
            return {"location": {"lat": 10, "lng": 20}, "accuracy": 50}
    controls = PinControls()
    app = create_app(StubBridge(), tmp_path, public_url="https://pin.example",
                     environment=Environment(), controls=controls)
    async with TestClient(TestServer(app)) as client:
        link = issue_pairing(tmp_path, "https://pin.example")
        device = (await (await client.post(urlsplit(link).path)).json())["deviceId"]
        home = await (await client.post("/api/dev/home-data", json={"deviceId": device})).json()
        assert home["location"] == "Fixture city" and home["conditions"] == "sunny"
        located = await client.post("/api/dev/locate", json={"deviceId": device, "wifiAccessPoints": [{"fixture": True}]})
        assert (await located.json())["location"] == {"lat": 10, "lng": 20}
        pending = asyncio.create_task(controls.submit("ring", {}))
        await asyncio.sleep(0)
        assert (await client.post("/api/dev/commands/poll", json={"deviceId": "wrong"})).status == 401
        command = (await (await client.post("/api/dev/commands/poll", json={"deviceId": device})).json())["command"]
        assert command["name"] == "ring"
        response = await client.post("/api/dev/commands/result", json={"deviceId": device,
                                      "id": command["id"], "result": {"ok": True}})
        assert response.status == 200
        assert (await pending)["ok"] is True


async def test_gallery_saves_captures_while_muse_is_offline(tmp_path):
    from openpin_muse.gallery import Gallery
    gallery = Gallery(tmp_path)
    bridge = StubBridge()
    bridge.ready = False
    app = create_app(bridge, tmp_path, public_url="https://pin.example", gallery=gallery)
    async with TestClient(TestServer(app)) as client:
        link = issue_pairing(tmp_path, "https://pin.example")
        device = (await (await client.post(urlsplit(link).path)).json())["deviceId"]
        response = await client.post("/api/dev/upload-capture", data=capture_form(
            device, b"\xff\xd8\xfffixture", "camera.jpg"))
        assert response.status == 200
        capture_id = (await response.json())["captureId"]
        entry, path = await gallery.get(capture_id)
        assert path.read_bytes() == b"\xff\xd8\xfffixture"
        async with asyncio.timeout(2):
            while (await gallery.get(capture_id))[0]["muse_status"] == "pending":
                await asyncio.sleep(0.01)
        assert (await gallery.get(capture_id))[0]["muse_status"] == "failed"
