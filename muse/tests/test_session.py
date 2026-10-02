"""Protocol tests: real SDK Noise handshake, encrypted frames, no live Muse.

The VM and its audio bytes are local fixtures; this verifies transport and
correlation, not the remote service's speech recognition or MP3 decoder.
"""

import asyncio
import base64
from contextlib import asynccontextmanager
import io
import json
import wave

import pytest

from musegadget.link_client import DeviceDescription, MessageDecoder, Outcome, encode_message
from musegadget.noise import (
    ApplicationResponse, BodyChunk, Header, NoiseFrameDecoder, NoiseXXResponder,
    Reset, ServiceFrame, encode_noise_frames,
)
from musegadget.noise.transport import decode_request_envelope, encode_response_envelope

from openpin_muse.session import MuseSession


class Pipe:
    def __init__(self, inbox, outbox):
        self.inbox, self.outbox = inbox, outbox

    async def send(self, data):
        await self.outbox.put(data)

    async def recv(self):
        data = await self.inbox.get()
        if data is None:
            raise ConnectionError("closed")
        return data

    async def close(self):
        await self.outbox.put(None)


class FakeVM:
    def __init__(self, ws):
        self.ws = ws
        self.decoder = NoiseFrameDecoder()
        self.messages = MessageDecoder()

    async def handshake(self):
        responder = NoiseXXResponder()
        responder.initialize()
        await self.ws.send(responder.read_message1_and_write_message2(await self.ws.recv()))
        responder.read_message3(await self.ws.recv())
        self.send_cipher, self.recv_cipher = responder.split()

    async def frame(self):
        async with asyncio.timeout(2):
            while True:
                plain = self.recv_cipher.decrypt_with_ad(b"", await self.ws.recv())
                assembled = self.decoder.decode(plain)
                if assembled is not None:
                    return decode_request_envelope(assembled)

    async def send(self, frame):
        for chunk in encode_noise_frames(encode_response_envelope(frame)):
            await self.ws.send(self.send_cipher.encrypt_with_ad(b"", chunk))

    async def response(self, stream, body=b"", *, status=200, end=True, headers=()):
        await self.send(ServiceFrame.response(stream, ApplicationResponse(
            status=status, body=body, end_body=end, headers=headers,
        )))

    async def chunk(self, stream, body, *, end=False):
        await self.send(ServiceFrame.body_chunk(stream, BodyChunk(data=body, end_body=end)))

    async def event(self, event, message_id, *, parent=None, **payload):
        payload["message_id"] = message_id
        if parent is not None:
            payload["reply_to_message_id"] = parent
        data = json.dumps({"type": "event", "event": event, "payload": payload}).encode() + b"\n"
        # Split inside JSON and an arbitrary UTF-8 sequence if one is present.
        await self.chunk(self.subscription, data[:13])
        await self.chunk(self.subscription, data[13:])

    async def subscribe_and_chat(self):
        request = await self.frame()
        assert (request.value.verb, request.value.path) == ("POST", "/chat/subscribe")
        assert json.loads(request.value.body) == {}
        self.subscription = request.stream_id
        await self.response(self.subscription, b'{"type":"ack"}\n', end=False)
        chat = await self.frame()
        assert (chat.value.verb, chat.value.path) == ("POST", "/chat/stream")
        return chat

    async def acknowledge(self, chat, **ids):
        await self.response(chat.stream_id, json.dumps({"result": ids}).encode())


@asynccontextmanager
async def connected(*, register=True, session_class=MuseSession, **kwargs):
    to_device, to_vm = asyncio.Queue(), asyncio.Queue()
    device_ws, vm_ws = Pipe(to_device, to_vm), Pipe(to_vm, to_device)
    async def connect(url, headers):
        assert url == "wss://vm.example/v1/noise?vm_id=vm-test"
        assert headers == {"Authorization": "Bearer local-test-token"}
        return device_ws
    session = session_class(
        noise_host="vm.example", vm_id="vm-test", vm_auth_token="local-test-token",
        device=DeviceDescription(node_id="openpin-test", display_name="OpenPin", version="0.1", commands={}),
        run_command=lambda *args: {"ok": False, "error": "unsupported"}, connect=connect,
        **kwargs,
    )
    vm = FakeVM(vm_ws)
    stop = asyncio.Event()
    running = asyncio.create_task(session.run(stop))
    try:
        await vm.handshake()
        control = await vm.frame()
        assert control.value.path == "/link-control"
        vm.control = control.stream_id
        await vm.response(vm.control, end=False)
        register_frame = await vm.frame()
        vm.registration = vm.messages.feed(register_frame.value.data)[0]
        if register:
            await vm.chunk(vm.control, encode_message({"type": "res", "id": vm.registration["id"], "ok": True}))
        yield session, vm, running
    finally:
        stop.set()
        await asyncio.wait_for(running, 2)


def wav(rate=16000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0\0" * 160)
    return output.getvalue()


@pytest.mark.asyncio
async def test_voice_note_correlates_early_events_and_returns_fragmented_mp3():
    async with connected() as (session, vm, _):
        audio = wav()
        answer = asyncio.create_task(session.converse(audio, b"jpeg-fixture", "Describe this"))
        chat = await vm.subscribe_and_chat()
        body = json.loads(chat.value.body)
        assert body["device_id"] == "openpin-test"
        assert body["message"] == "Describe this"
        assert body["output_modality"] == "voice"
        assert body["items"][0]["mime_type"] == "audio/wav"
        assert base64.b64decode(body["items"][0]["data_base64"]) == audio
        assert body["items"][1]["mime_type"] == "image/jpeg"
        assert base64.b64decode(body["items"][1]["data_base64"]) == b"jpeg-fixture"
        assert session.registered_at is not None
        await vm.event("message.assistant", "someone-elses-answer", parent="other-user", content="private")
        await vm.event("message.assistant", "unattributed-answer", content="private")
        await vm.event("delta.message_start", "our/answer?", parent="our-user")
        await vm.event("delta.text_append", "our/answer?", text="Hello")
        await vm.event("delta.message_done", "our/answer?")
        await vm.acknowledge(chat, message_id="our-user")
        tts = await vm.frame()
        assert tts.value.path == "/api/voice/tts-stream?message_id=our%2Fanswer%3F"
        assert dict((h.key.lower(), h.value) for h in tts.value.headers)["accept"] == "audio/mpeg"
        await vm.response(tts.stream_id, b"ID3", end=False, headers=[Header("Content-Type", "audio/mpeg")])
        await vm.chunk(tts.stream_id, b"local-mp3-transport-fixture", end=True)
        assert await asyncio.wait_for(answer, 2) == b"ID3local-mp3-transport-fixture"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 403, 500])
async def test_tts_fallback_only_on_404(status):
    async with connected() as (session, vm, _):
        answer = asyncio.create_task(session.converse(wav()))
        chat = await vm.subscribe_and_chat()
        await vm.acknowledge(chat, reply_to_message_id="our-user")
        await vm.event("message.assistant", "our-answer", parent="our-user", display_text="Hello")
        tts = await vm.frame()
        await vm.response(tts.stream_id, status=status)
        if status == 404:
            fallback = await vm.frame()
            assert fallback.value.path == "/voice/tts-stream?message_id=our-answer"
            await vm.response(fallback.stream_id, b"ID3fixture", headers=[Header("content-type", "audio/mpeg")])
            assert await answer == b"ID3fixture"
        else:
            with pytest.raises(RuntimeError, match=str(status)):
                await asyncio.wait_for(answer, 2)
            assert vm.ws.inbox.empty()


@pytest.mark.asyncio
async def test_registration_is_required_before_any_chat_request():
    async with connected(register=False, turn_timeout=0.03) as (session, vm, _):
        with pytest.raises(TimeoutError):
            await session.converse(wav())
        assert vm.ws.inbox.empty()


@pytest.mark.asyncio
async def test_rejected_registration_fails_pending_capture():
    async with connected(register=False) as (session, vm, _):
        capture = asyncio.create_task(session.capture(b"image-fixture", "image/jpeg"))
        await vm.chunk(vm.control, encode_message({"type": "res", "id": vm.registration["id"], "error": {"code": "denied"}}))
        with pytest.raises(ConnectionError, match="registration"):
            await asyncio.wait_for(capture, 2)
        assert vm.ws.inbox.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["reset", "disconnect"])
async def test_subscription_reset_or_disconnect_fails_waiting_answer(failure):
    async with connected() as (session, vm, running):
        answer = asyncio.create_task(session.converse(wav()))
        chat = await vm.subscribe_and_chat()
        await vm.acknowledge(chat, message_id="our-user")
        if failure == "reset":
            await vm.send(ServiceFrame.reset(vm.subscription, Reset(reason="fixture reset")))
        else:
            await vm.ws.close()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(answer, 2)
        if failure == "disconnect":
            assert await running is Outcome.CLOSED
            with pytest.raises(ConnectionError):
                await session.capture(b"image-fixture", "image/jpeg")


@pytest.mark.asyncio
async def test_uncorrelated_messages_never_trigger_tts_and_turn_times_out():
    async with connected(turn_timeout=0.1) as (session, vm, _):
        answer = asyncio.create_task(session.converse(wav()))
        chat = await vm.subscribe_and_chat()
        await vm.acknowledge(chat, message_id="our-user")
        await vm.event("message.assistant", "foreign", parent="other-user", content="private")
        await vm.event("message.assistant", "unattributed", content="private")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(answer, 2)
        assert vm.ws.inbox.empty()


@pytest.mark.asyncio
async def test_capture_posts_supported_file_on_same_device_session():
    async with connected() as (session, vm, _):
        capture = asyncio.create_task(session.capture(b"jpeg-fixture", "image/jpeg"))
        chat = await vm.frame()
        assert chat.value.path == "/chat/stream"
        body = json.loads(chat.value.body)
        assert body["device_id"] == "openpin-test"
        assert body["items"][0]["mime_type"] == "image/jpeg"
        assert base64.b64decode(body["items"][0]["data_base64"]) == b"jpeg-fixture"
        await vm.acknowledge(chat, message_id="capture-user")
        assert await capture is None


@pytest.mark.asyncio
async def test_capture_wait_has_request_timeout_and_resets_abandoned_stream():
    async with connected(request_timeout=0.03) as (session, vm, _):
        capture = asyncio.create_task(session.capture(b"jpeg-fixture", "image/jpeg"))
        chat = await vm.frame()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(capture, 2)
        reset = await vm.frame()
        assert (reset.kind, reset.stream_id) == ("reset", chat.stream_id)


@pytest.mark.asyncio
async def test_tts_response_is_bounded():
    async with connected(max_audio_bytes=4) as (session, vm, _):
        answer = asyncio.create_task(session.converse(wav()))
        chat = await vm.subscribe_and_chat()
        await vm.acknowledge(chat, message_id="our-user")
        await vm.event("message.assistant", "our-answer", parent="our-user", content="Hello")
        tts = await vm.frame()
        await vm.response(tts.stream_id, b"ID3too-large", headers=[Header("Content-Type", "audio/mpeg")])
        with pytest.raises(ValueError, match="large"):
            await asyncio.wait_for(answer, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("audio", [b"not-wav", wav(rate=44100), wav(channels=2)])
async def test_invalid_voice_input_is_rejected_before_sending(audio):
    async with connected() as (session, vm, _):
        with pytest.raises(ValueError, match="WAV"):
            await session.converse(audio)
        assert vm.ws.inbox.empty()


@pytest.mark.asyncio
async def test_control_result_queued_behind_upload_preserves_noise_nonce_order():
    invoke_sending = asyncio.Event()

    class ObservedSession(MuseSession):
        async def send(self, message):
            if message.get("method") == "link.result":
                invoke_sending.set()
            await super().send(message)

    async with connected(session_class=ObservedSession) as (session, vm, _):
        await session._wait_registered()
        # Force an HTTP upload to queue before an asynchronous control result.
        # Actual encrypted frames must still decrypt in the order sent.
        await session._send_lock.acquire()
        capture = asyncio.create_task(session.capture(b"x" * 100_000, "image/jpeg"))
        await asyncio.sleep(0)
        await vm.chunk(vm.control, encode_message({
            "method": "link.invoke", "id": "test-invoke", "command": "unsupported", "params": {},
        }))
        await asyncio.wait_for(invoke_sending.wait(), 2)
        session._send_lock.release()
        chat = await vm.frame()
        assert chat.value.path == "/chat/stream"
        control = await vm.frame()
        assert control.stream_id == vm.control
        assert vm.messages.feed(control.value.data)[0] == {
            "method": "link.result", "id": "test-invoke", "ok": False, "error": "unsupported",
        }
        await vm.acknowledge(chat, message_id="our-capture")
        await capture


@pytest.mark.asyncio
async def test_next_turn_reuses_subscription_but_rejects_previous_turn_replies():
    async with connected() as (session, vm, _):
        first = asyncio.create_task(session.converse(wav()))
        chat = await vm.subscribe_and_chat()
        await vm.acknowledge(chat, message_id="first-user")
        await vm.event("message.assistant", "first-answer", parent="first-user", content="One")
        tts = await vm.frame()
        await vm.response(tts.stream_id, b"ID3first")
        assert await first == b"ID3first"

        second = asyncio.create_task(session.converse(wav()))
        chat = await vm.frame()
        assert chat.value.path == "/chat/stream"
        await vm.event("message.assistant", "late-first-answer", parent="first-user", content="Old")
        await vm.acknowledge(chat, message_id="second-user")
        await vm.event("message.assistant", "second-answer", parent="second-user", content="Two", display_text_ready=False)
        await vm.event("delta.message_done", "second-answer")
        tts = await vm.frame()
        assert tts.value.path.endswith("message_id=second-answer")
        await vm.response(tts.stream_id, b"ID3second")
        assert await second == b"ID3second"


@pytest.mark.asyncio
async def test_only_first_completed_answer_is_downloaded():
    async with connected() as (session, vm, _):
        answer = asyncio.create_task(session.converse(wav()))
        chat = await vm.subscribe_and_chat()
        await vm.acknowledge(chat, message_id="our-user")
        await vm.event("message.assistant", "first-answer", parent="our-user", content="First")
        await vm.event("message.assistant", "second-answer", parent="our-user", content="Second")
        tts = await vm.frame()
        assert tts.value.path.endswith("message_id=first-answer")
        await vm.response(tts.stream_id, b"ID3first")
        assert await answer == b"ID3first"
        assert vm.ws.inbox.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", [b'{}', b'{"result":{"accepted":true}}', b'not-json'])
async def test_missing_or_malformed_ack_fails_without_playing_uncorrelated_events(ack):
    async with connected() as (session, vm, _):
        answer = asyncio.create_task(session.converse(wav()))
        chat = await vm.subscribe_and_chat()
        await vm.event("message.assistant", "foreign", parent="some-user", content="Private")
        await vm.response(chat.stream_id, ack)
        with pytest.raises(ValueError, match="acknowledgement"):
            await asyncio.wait_for(answer, 2)
        assert vm.ws.inbox.empty()


@pytest.mark.asyncio
async def test_subscription_failure_can_be_recovered_on_next_turn():
    async with connected() as (session, vm, _):
        first = asyncio.create_task(session.converse(wav()))
        chat = await vm.subscribe_and_chat()
        await vm.acknowledge(chat, message_id="first-user")
        await vm.send(ServiceFrame.reset(vm.subscription, Reset(reason="local failure")))
        with pytest.raises(ConnectionError):
            await first
        old_subscription = vm.subscription
        second = asyncio.create_task(session.converse(wav()))
        chat = await vm.subscribe_and_chat()
        assert vm.subscription != old_subscription
        await vm.acknowledge(chat, message_id="second-user")
        await vm.event("message.assistant", "second-answer", parent="second-user", content="Recovered")
        tts = await vm.frame()
        await vm.response(tts.stream_id, b"ID3second")
        assert await second == b"ID3second"


@pytest.mark.asyncio
async def test_closed_session_retains_last_registration_for_reconnect_backoff():
    async with connected() as (session, vm, running):
        await session._wait_registered()
        registered_at = session.registered_at
        await vm.ws.close()
        assert await running is Outcome.CLOSED
        assert session.registered_at is None
        assert session.last_registered_at == registered_at


@pytest.mark.asyncio
async def test_broken_subscription_json_fails_pending_request_immediately():
    async with connected() as (session, vm, _):
        answer = asyncio.create_task(session.converse(wav()))
        chat = await vm.subscribe_and_chat()
        await vm.acknowledge(chat, message_id="our-user")
        await vm.chunk(vm.subscription, b"not-json\n")
        with pytest.raises(ValueError):
            await asyncio.wait_for(answer, 2)


@pytest.mark.asyncio
async def test_partial_upload_cancellation_closes_old_session_and_new_session_works():
    async with connected() as (session, vm, _):
        original_send = session._ws.send
        first_frame = asyncio.Event()
        suspended = asyncio.Event()

        async def pause_after_first_frame(data):
            await original_send(data)
            first_frame.set()
            await suspended.wait()

        session._ws.send = pause_after_first_frame
        uploading = asyncio.create_task(session.capture(b"x" * 100000, "image/jpeg"))
        await asyncio.wait_for(first_frame.wait(), 1)
        uploading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await uploading
        assert session._requests == {}
        assert isinstance(await vm.ws.recv(), bytes)
        with pytest.raises(ConnectionError):
            await vm.ws.recv()

    async with connected() as (session, vm, _):
        uploading = asyncio.create_task(session.capture(b"fresh-fixture", "image/jpeg"))
        request = await vm.frame()
        assert request.value.path == "/chat/stream"
        await vm.response(request.stream_id, b'{"result":{"message_id":"fresh-turn"}}')
        await uploading
