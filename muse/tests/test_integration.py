"""Simulated Pin HTTP → real ffmpeg → real encrypted Muse protocol → valid MP3.

The Muse peer supplies a generated tone, not speech from a live Muse account.
"""

import asyncio
import base64
import io
import json
import shutil
import wave
from urllib.parse import urlsplit

from aiohttp.test_utils import TestClient, TestServer
from musegadget.identity import Identity
from musegadget import config, muse_api
from musegadget.link_client import encode_message
from musegadget.noise import Header
import pytest
import websockets.asyncio.client

from openpin_muse.api import create_app, issue_pairing
from openpin_muse.bridge import MuseBridge
from openpin_muse.controls import PinControls
from test_session import FakeVM, Pipe, connected


async def tone(codec, format):
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-ar", "16000", "-ac", "1", "-c:a", codec, "-f", format, "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    audio, error = await process.communicate()
    assert process.returncode == 0, error
    return audio


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")
@pytest.mark.parametrize("translate", [False, True])
async def test_qr_link_to_spoken_reply(tmp_path, translate):
    ogg, mp3 = await asyncio.gather(tone("libopus", "ogg"), tone("libmp3lame", "mp3"))
    async with connected() as (session, vm, _):
        bridge = MuseBridge(Identity("02:00:00:00:00:01"), None)
        bridge._current = session
        await asyncio.wait_for(session._registration_ready.wait(), 1)
        assert vm.registration["params"]["commands_v2"] == {}
        assert vm.registration["params"]["device_family"] == "homehub"
        app = create_app(bridge, tmp_path, public_url="http://127.0.0.1:8787")
        async with TestClient(TestServer(app)) as client:
            qr = issue_pairing(tmp_path, "http://127.0.0.1:8787")
            pairing = await client.post(urlsplit(qr).path)
            assert pairing.status == 200
            device = await pairing.json()
            jpeg = b"\xff\xd8\xff\xe0test-jpeg\xff\xd9"  # Local payload fixture, not a camera capture.
            header = json.dumps({"deviceId": device["deviceId"], "audioFormat": "ogg",
                                 "audioSize": len(ogg), "imageSize": len(jpeg)}).encode()
            body = header.ljust(512, b"\0") + jpeg + ogg
            request = asyncio.create_task(client.post(
                "/api/dev/translate" if translate else "/api/dev/handle", data=body,
            ))
            chat = await vm.subscribe_and_chat()
            posted = json.loads(chat.value.body)
            assert bool(posted["message"]) == translate
            if translate:
                assert "English" in posted["message"] and "Spanish" in posted["message"]
            audio = base64.b64decode(posted["items"][0]["data_base64"])
            with wave.open(io.BytesIO(audio)) as decoded:
                assert (decoded.getframerate(), decoded.getnchannels(), decoded.getsampwidth()) == (16000, 1, 2)
                assert decoded.getnframes() == 16000
            assert base64.b64decode(posted["items"][1]["data_base64"]) == jpeg
            await vm.acknowledge(chat, message_id="pin-turn")
            await vm.event("message.assistant", "other-answer", parent="other-phone", content="Ignore me")
            await vm.event("message.assistant", "pin-answer", parent="pin-turn", content="A simulated answer")
            tts = await vm.frame()
            assert tts.value.path == "/api/voice/tts-stream?message_id=pin-answer"
            await vm.response(tts.stream_id, mp3, headers=[Header("Content-Type", "audio/mpeg")])
            response = await request
            assert response.status == 200
            wire = await response.read()
            assert isinstance(json.loads(wire[:512].rstrip(b"\0")), dict)
            assert wire[512:] == mp3
            assert (await client.post(urlsplit(qr).path)).status == 404


async def test_production_bridge_refreshes_registers_and_reconnects(tmp_path, monkeypatch):
    """Stub account API credentials only; run the production service and Noise session."""
    monkeypatch.setenv(config.STATE_DIR_ENV, str(tmp_path))
    config.save_json(config.PAIRING_FILE, {
        "access_token": "expired-fixture", "refresh_token": "refresh-fixture",
        "access_token_saved_at": 0, "noise_host": "vm.example",
    })
    refreshed = []

    def refresh(token, node_id, api, sdk_token):
        refreshed.append((token, node_id))
        return {"access_token": "fresh-fixture", "refresh_token": "rotated-fixture"}, 200

    def fetch(token, api):
        assert token == "fresh-fixture"
        return [{"vm_id": "vm-test", "vm_name": "Test Muse", "vm_auth_token": "vm-fixture",
                 "is_default": True}], 200

    peers = asyncio.Queue()

    async def connect(url, **kwargs):
        assert kwargs["additional_headers"] == {"Authorization": "Bearer vm-fixture"}
        to_device, to_vm = asyncio.Queue(), asyncio.Queue()
        peers.put_nowait(FakeVM(Pipe(to_vm, to_device)))
        return Pipe(to_device, to_vm)

    monkeypatch.setattr(muse_api, "refresh_device_token", refresh)
    monkeypatch.setattr(muse_api, "fetch_vms_with_status", fetch)
    monkeypatch.setattr(websockets.asyncio.client, "connect", connect)
    controls = PinControls()

    class Environment:
        async def context(self):
            return {"location": {"name": "Simulated location", "source": "configured"}}

    bridge = MuseBridge(Identity("02:00:00:00:00:01"), None, controls=controls, environment=Environment())
    running = asyncio.create_task(bridge.run())

    async def register_peer():
        vm = await asyncio.wait_for(peers.get(), 5)
        await vm.handshake()
        control = await vm.frame()
        assert control.value.path == "/link-control"
        vm.control = control.stream_id
        await vm.response(control.stream_id, end=False)
        frame = await vm.frame()
        registration = vm.messages.feed(frame.value.data)[0]
        assert registration["params"]["node_id"] == "homelink-000001"
        assert set(registration["params"]["commands_v2"]) == {"get_status", "ring", "set_volume", "capture_photo", "record_video", "get_environment"}
        await vm.chunk(control.stream_id, encode_message({"id": registration["id"], "ok": True}))
        async with asyncio.timeout(1):
            while not bridge.ready:
                await asyncio.sleep(0.005)
        return vm

    try:
        first = await register_peer()
        assert refreshed == [("refresh-fixture", "homelink-000001")]
        assert config.load_json(config.PAIRING_FILE)["refresh_token"] == "rotated-fixture"
        await first.ws.close()
        async with asyncio.timeout(1):
            while bridge.ready:
                await asyncio.sleep(0.005)
        second = await register_peer()
        await second.chunk(second.control, encode_message({"method": "link.invoke", "id": "remote-command",
                           "command": "set_volume", "params": {"volume": 0.4}}))
        async with asyncio.timeout(2):
            command = None
            while command is None:
                command = await controls.poll({"battery": 0.75})
                await asyncio.sleep(0.005)
        assert command["name"] == "set_volume" and command["params"] == {"volume": 0.4}
        await controls.complete(command["id"], {"ok": True, "volume": 0.4})
        result = await second.frame()
        returned = second.messages.feed(result.value.data)[0]
        assert returned == {"method": "link.result", "id": "remote-command", "ok": True, "payload": {"volume": 0.4}}
        await second.chunk(second.control, encode_message({"method": "link.invoke", "id": "environment-command",
                           "command": "get_environment", "params": {}}))
        result = await second.frame()
        assert second.messages.feed(result.value.data)[0] == {
            "method": "link.result", "id": "environment-command", "ok": True,
            "payload": {"location": {"name": "Simulated location", "source": "configured"}},
        }
        await second.chunk(second.control, encode_message({"method": "link.invoke", "id": "bad-command",
                           "command": "system.run", "params": {}}))
        result = await second.frame()
        assert second.messages.feed(result.value.data)[0] == {
            "method": "link.result", "id": "bad-command", "ok": False,
            "error": "Unsupported Pin command",
        }
        sending = asyncio.create_task(bridge.capture(b"jpeg-fixture", "image/jpeg"))
        request = await second.frame()
        assert request.value.path == "/chat/stream"
        assert json.loads(request.value.body)["device_id"] == "homelink-000001"
        await second.response(request.stream_id, b'{"result":{"message_id":"capture"}}')
        await sending
    finally:
        bridge.stop()
        await asyncio.wait_for(running, 2)
    assert not bridge.ready
