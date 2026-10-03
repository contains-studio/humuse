"""Transient disk failures must preserve captures and leave forwarding usable."""

import asyncio
from urllib.parse import urlsplit

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
import pytest

from openpin_muse.api import create_app, issue_pairing
from openpin_muse.dashboard import issue_dashboard_link
from openpin_muse.gallery import Gallery
from test_api import StubBridge, capture_form


@pytest.mark.parametrize("failed_status", ["pending", "failed"])
async def test_status_write_failure_allows_next_capture_and_retry(tmp_path, monkeypatch, failed_status):
    origin = "http://127.0.0.1:8787"
    gallery, bridge = Gallery(tmp_path), StubBridge()
    bridge.ready = failed_status == "pending"
    original_mark = gallery.mark_forwarded
    failed_write = asyncio.Event()

    async def fail_once(capture_id, status):
        if status == failed_status and not failed_write.is_set():
            failed_write.set()
            raise OSError("Simulated transient storage failure")
        await original_mark(capture_id, status)

    monkeypatch.setattr(gallery, "mark_forwarded", fail_once)
    app = create_app(bridge, tmp_path, public_url=origin, gallery=gallery)
    async with TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True)) as client:
        pairing = issue_pairing(tmp_path, origin)
        device = (await (await client.post(urlsplit(pairing).path)).json())["deviceId"]
        first = await client.post("/api/dev/upload-capture", data=capture_form(
            device, b"\xff\xd8\xfffirst fixture", "first.jpg"))
        assert first.status == (500 if failed_status == "pending" else 200)
        await asyncio.wait_for(failed_write.wait(), 2)
        first_id = (await gallery.list())[0]["id"]
        assert (await gallery.get(first_id))[1].read_bytes() == b"\xff\xd8\xfffirst fixture"

        # A subsequent capture proves the sole worker survived a failed status
        # update. These are local byte fixtures, not live Muse transmissions.
        bridge.ready = True
        second = await client.post("/api/dev/upload-capture", data=capture_form(
            device, b"\xff\xd8\xffsecond fixture", "second.jpg"))
        assert second.status == 200
        second_id = (await second.json())["captureId"]

        async def wait_sent(capture_id):
            async with asyncio.timeout(2):
                while (await gallery.get(capture_id))[0]["muse_status"] != "sent":
                    await asyncio.sleep(0.005)

        await wait_sent(second_id)
        link = issue_dashboard_link(tmp_path, origin)
        response = await client.post("/api/dashboard/redeem", json={"code": urlsplit(link).fragment},
                                     headers={"Origin": origin})
        assert response.status == 200
        session = await response.json()
        retry = await client.post(f"/api/dashboard/media/{first_id}/retry",
                                  headers={"Origin": origin, "X-CSRF-Token": session["csrf"]})
        assert retry.status == 202
        await wait_sent(first_id)
        assert len(bridge.captures) == 2
