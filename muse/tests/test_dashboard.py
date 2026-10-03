from urllib.parse import urlsplit
import asyncio
import stat
from types import SimpleNamespace

from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from openpin_muse.api import create_app, issue_pairing
from openpin_muse.controls import PinControls
from openpin_muse.dashboard import install_dashboard, issue_dashboard_link
from openpin_muse.gallery import Gallery


ORIGIN = "http://127.0.0.1:8787"
JPEG = b"\xff\xd8\xffdashboard image fixture\xff\xd9"


class Controls:
    async def snapshot(self):
        return {"online": False}

    async def submit(self, command, params, timeout_ms):
        return {"ok": True, "volume": params["volume"]}


async def client_for(tmp_path, gallery, **options):
    app = web.Application()
    install_dashboard(app, gallery, tmp_path, public_url=ORIGIN, **options)
    client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    return client


async def redeem(client, link):
    response = await client.post("/api/dashboard/redeem", json={"code": urlsplit(link).fragment},
                                 headers={"Origin": ORIGIN})
    assert response.status == 200
    return await response.json()


async def test_one_use_link_cookie_auth_and_private_media(tmp_path):
    gallery = Gallery(tmp_path)
    image = await gallery.save(JPEG, "image/jpeg")
    link = issue_dashboard_link(tmp_path, ORIGIN)
    assert urlsplit(link).query == ""
    async with await client_for(tmp_path, gallery) as client:
        assert (await client.get("/api/dashboard/media")).status == 401
        assert (await client.get(f"/api/dashboard/media/{image['id']}")).status == 401
        page = await client.get("/dashboard")
        assert page.status == 200
        assert "default-src 'none'" in page.headers["Content-Security-Policy"]
        session = await redeem(client, link)
        assert len(session["csrf"]) >= 32
        response = await client.get("/api/dashboard/media")
        assert (await response.json())["items"][0]["id"] == image["id"]
        response = await client.get(f"/api/dashboard/media/{image['id']}")
        assert await response.read() == JPEG
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Content-Type"] == "image/jpeg"
        assert (await client.post("/api/dashboard/redeem", json={"code": urlsplit(link).fragment},
                                 headers={"Origin": ORIGIN})).status == 401


async def test_mutations_require_same_origin_and_csrf_and_logout_revokes_session(tmp_path):
    gallery = Gallery(tmp_path)
    item = await gallery.save(JPEG, "image/jpeg")
    link = issue_dashboard_link(tmp_path, ORIGIN)
    async with await client_for(tmp_path, gallery, controls=Controls()) as client:
        session = await redeem(client, link)
        path = f"/api/dashboard/media/{item['id']}"
        assert (await client.delete(path)).status == 403
        assert (await client.delete(path, headers={"Origin": "https://evil.example",
                                                   "X-CSRF-Token": session["csrf"]})).status == 403
        headers = {"Origin": ORIGIN, "X-CSRF-Token": session["csrf"]}
        response = await client.post("/api/dashboard/commands",
                                     json={"command": "set_volume", "params": {"volume": 0.5}}, headers=headers)
        assert response.status == 200
        assert await response.json() == {"ok": True, "volume": 0.5}
        assert (await client.post("/api/dashboard/commands", json={"command": "shell"},
                                  headers=headers)).status == 400
        assert (await client.delete(path, headers=headers)).status == 200
        assert await gallery.get(item["id"]) is None
        assert (await client.post("/api/dashboard/logout", headers=headers)).status == 200
        assert (await client.get("/api/dashboard/media")).status == 401


@pytest.mark.parametrize("pin_result", [
    {"ok": True, "volume": 0.4},
    {"ok": False, "error": "Pin audio service is unavailable"},
])
async def test_dashboard_command_waits_for_pin_ack_and_returns_its_actual_result(tmp_path, pin_result):
    controls = PinControls()
    pair_link = issue_pairing(tmp_path, ORIGIN)
    # Only the Pin and Muse transport are simulated; both HTTP contracts and the
    # command queue are the same ones used by the dashboard and Android client.
    app = create_app(SimpleNamespace(ready=False), tmp_path, public_url=ORIGIN,
                     gallery=Gallery(tmp_path), controls=controls)
    async with TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True)) as client:
        pairing = await client.post(urlsplit(pair_link).path)
        assert pairing.status == 200
        device_id = (await pairing.json())["deviceId"]
        session = await redeem(client, issue_dashboard_link(tmp_path, ORIGIN))

        async def send_dashboard_command():
            return await client.post("/api/dashboard/commands",
                                     json={"command": "set_volume", "params": {"volume": 0.4}},
                                     headers={"Origin": ORIGIN, "X-CSRF-Token": session["csrf"]})

        async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(send_dashboard_command())
            command = None
            while command is None:
                poll = await client.post("/api/dev/commands/poll", json={"deviceId": device_id})
                assert poll.status == 200
                command = (await poll.json())["command"]
                if command is None:
                    await asyncio.sleep(0.01)
            assert command["name"] == "set_volume"
            assert command["params"] == {"volume": 0.4}
            assert not pending.done(), "Delivery alone must not report success to the dashboard"

            acknowledgement = await client.post("/api/dev/commands/result", json={
                "deviceId": device_id, "id": command["id"], "result": pin_result,
            })
            assert acknowledgement.status == 200
            response = await pending
            assert response.status == 200
            assert await response.json() == pin_result

        poll = await client.post("/api/dev/commands/poll", json={"deviceId": device_id})
        assert (await poll.json())["command"] is None


async def test_expired_links_and_cross_origin_redemption_are_denied(tmp_path, monkeypatch):
    import openpin_muse.dashboard as dashboard
    monkeypatch.setattr(dashboard.time, "time", lambda: 1000)
    link = issue_dashboard_link(tmp_path, ORIGIN, ttl_seconds=1)
    async with await client_for(tmp_path, Gallery(tmp_path)) as client:
        assert (await client.post("/api/dashboard/redeem", json={"code": urlsplit(link).fragment},
                                 headers={"Origin": "https://evil.example"})).status == 403
        monkeypatch.setattr(dashboard.time, "time", lambda: 1002)
        assert (await client.post("/api/dashboard/redeem", json={"code": urlsplit(link).fragment},
                                 headers={"Origin": ORIGIN})).status == 401


@pytest.mark.parametrize("path", ["/api/dashboard/state", "/api/dashboard/session"])
async def test_private_endpoints_require_session(tmp_path, path):
    async with await client_for(tmp_path, Gallery(tmp_path)) as client:
        assert (await client.get(path)).status == 401


async def test_video_ranges_download_and_retry_use_saved_original(tmp_path):
    gallery = Gallery(tmp_path)
    original = b"\x00\x00\x00\x18ftypmp42video-fixture"
    item = await gallery.save(original, "video/mp4")
    await gallery.mark_forwarded(item["id"], "failed")
    forwarded = []

    async def forward(capture_id):
        forwarded.append(capture_id)
        await gallery.mark_forwarded(capture_id, "pending")
        return {"queued": True}

    link = issue_dashboard_link(tmp_path, ORIGIN)
    async with await client_for(tmp_path, gallery, forward_capture=forward) as client:
        session = await redeem(client, link)
        path = f"/api/dashboard/media/{item['id']}"
        response = await client.get(path, headers={"Range": "bytes=4-7"})
        assert response.status == 206
        assert await response.read() == b"ftyp"
        response = await client.get(path + "/download")
        assert await response.read() == original
        assert response.headers["Content-Disposition"].startswith("attachment;")
        headers = {"Origin": ORIGIN, "X-CSRF-Token": session["csrf"]}
        assert (await client.post(path + "/retry", headers=headers)).status == 202
        assert forwarded == [item["id"]]
        assert (await gallery.get(item["id"]))[0]["muse_status"] == "pending"


async def test_https_cookie_is_private_and_session_survives_restart_then_expires(tmp_path, monkeypatch):
    import openpin_muse.dashboard as dashboard
    origin = "https://pin.example"
    monkeypatch.setattr(dashboard.time, "time", lambda: 1000)
    link = issue_dashboard_link(tmp_path, origin)
    app = web.Application()
    install_dashboard(app, Gallery(tmp_path), tmp_path, public_url=origin)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/dashboard/redeem", json={"code": urlsplit(link).fragment},
                                     headers={"Origin": origin})
        cookie = response.cookies[dashboard.COOKIE]
        assert cookie["httponly"] and cookie["secure"]
        assert cookie["samesite"] == "Strict"
        assert cookie["path"] == "/api/dashboard"
        token = cookie.value
    store = dashboard._AuthStore(tmp_path)
    assert store.session(token, origin)["expires_at"] == 4600
    for path in store.directory.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        contents = path.read_text()
        assert token not in contents
        assert urlsplit(link).fragment not in contents
    monkeypatch.setattr(dashboard.time, "time", lambda: 4601)
    assert store.session(token, origin) is None


@pytest.mark.parametrize("value", [-0.1, 1.1, True, "0.5", None])
async def test_volume_validation_prevents_invalid_control_calls(tmp_path, value):
    gallery = Gallery(tmp_path)
    async with await client_for(tmp_path, gallery, controls=Controls()) as client:
        session = await redeem(client, issue_dashboard_link(tmp_path, ORIGIN))
        response = await client.post("/api/dashboard/commands",
                                     json={"command": "set_volume", "params": {"volume": value}},
                                     headers={"Origin": ORIGIN, "X-CSRF-Token": session["csrf"]})
        assert response.status == 400


async def test_chunked_json_redemption_handles_split_packets(tmp_path):
    import asyncio
    import json
    link = issue_dashboard_link(tmp_path, ORIGIN)
    payload = json.dumps({"code": urlsplit(link).fragment}).encode()

    async def chunks():
        yield payload[:5]
        await asyncio.sleep(0.01)
        yield payload[5:]

    async with await client_for(tmp_path, Gallery(tmp_path)) as client:
        response = await client.post("/api/dashboard/redeem", data=chunks(),
                                     headers={"Origin": ORIGIN, "Content-Type": "application/json"})
        assert response.status == 200


async def test_unknown_media_ids_cannot_escape_gallery(tmp_path):
    async with await client_for(tmp_path, Gallery(tmp_path)) as client:
        session = await redeem(client, issue_dashboard_link(tmp_path, ORIGIN))
        assert (await client.get("/api/dashboard/media/secret")).status == 404
        assert (await client.delete("/api/dashboard/media/secret", headers={"Origin": ORIGIN,
                               "X-CSRF-Token": session["csrf"]})).status == 404
