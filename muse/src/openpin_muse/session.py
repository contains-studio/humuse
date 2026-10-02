"""OpenPin voice and attachments over the pinned Muse gadget Noise session.

The Linux SDK provides registration, Noise, framing, and request dispatch. This
small adapter supplies the voice protocol documented by the SDK's ESP32 client.
It intentionally uses LinkSession's private request registry, so the SDK revision
must remain pinned and these protocol tests must pass before it is upgraded.

One conversation returns the first completed, nonempty assistant message whose
parent is explicitly correlated with this request's acknowledgement. Additional
assistant messages in the same turn are not played by this initial adapter.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import mimetypes
import re
import uuid
import wave
from urllib.parse import quote

from musegadget.link_client import APP_ID, LinkSession, encode_message
from musegadget.noise import Header

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_EVENT_BYTES = 512 * 1024
MAX_PENDING_EVENTS = 256
MAX_MESSAGES = 64


class MuseHTTPError(RuntimeError):
    def __init__(self, path: str, status: int):
        self.status = status
        super().__init__(f"Muse {path} returned HTTP {status}")


def _future():
    future = asyncio.get_running_loop().create_future()
    # A stream can fail just as its caller times out or stops. Retrieving the
    # exception here prevents an unobserved-future warning; await still raises.
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return future


def _file_item(data: bytes, mime_type: str, filename: str | None = None) -> dict:
    if not data or len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("attachment must be nonempty and no larger than 8 MiB")
    if not re.fullmatch(r"[\w.+-]+/[\w.+-]+", mime_type, flags=re.ASCII):
        raise ValueError("invalid attachment MIME type")
    return {
        "type": "file", "mime_type": mime_type,
        "filename": filename or "capture" + (mimetypes.guess_extension(mime_type) or ".bin"),
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def _validate_wav(data: bytes) -> None:
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("WAV input is too large")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError("voice input must be a 16 kHz mono PCM16 WAV")
            if not audio.getnframes() or len(audio.readframes(audio.getnframes())) != audio.getnframes() * 2:
                raise ValueError("voice input must be a nonempty, complete WAV")
    except (wave.Error, EOFError) as exc:
        raise ValueError("voice input must be a 16 kHz mono PCM16 WAV") from exc


class _Response:
    def __init__(self, limit: int):
        self.done = _future()
        self.limit = limit
        self.status = 0
        self.headers = {}
        self.body = bytearray()
        self.ended = False

    def on_frame(self, frame):
        if self.done.done():
            return
        if frame.kind == "reset":
            self.ended = True
            self.done.set_exception(ConnectionError("Muse request stream reset"))
            return
        if frame.kind == "response":
            self.status = frame.value.status
            self.headers = {header.key.lower(): header.value for header in frame.value.headers}
            data, self.ended = frame.value.body, frame.value.end_body
        else:
            data, self.ended = frame.value.data, frame.value.end_body
        if len(self.body) + len(data) > self.limit:
            self.done.set_exception(ValueError("Muse response too large"))
        else:
            self.body.extend(data)
            if self.ended:
                self.done.set_result((self.status, bytes(self.body), self.headers))


class _Turn:
    def __init__(self):
        self.done = _future()
        self.user_ids: set[str] | None = None
        self.pending: list[dict] = []
        self.pending_bytes = 0
        self.messages: dict[str, bool] = {}

    def fail(self, error: Exception):
        if not self.done.done():
            self.done.set_exception(error)

    def acknowledge(self, body: bytes):
        try:
            ack = json.loads(body)
            if isinstance(ack, dict) and isinstance(ack.get("result"), dict):
                ack = ack["result"]
            self.user_ids = {
                value for key in ("message_id", "reply_to_message_id")
                if isinstance(ack, dict) and isinstance(value := ack.get(key), str) and value
            }
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Muse chat acknowledgement is invalid JSON") from exc
        if not self.user_ids:
            raise ValueError("Muse chat acknowledgement has no message correlation ID")
        pending, self.pending = self.pending, []
        for event in pending:
            self.on_event(event)

    def on_event(self, event: dict):
        if self.done.done() or event.get("type") != "event":
            return
        kind = event.get("event")
        if kind not in {"delta.message_start", "delta.text_append", "delta.message_done", "message.assistant"}:
            return
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("role", "assistant") != "assistant":
            return
        if self.user_ids is None:
            self.pending_bytes += len(json.dumps(event).encode())
            if len(self.pending) >= MAX_PENDING_EVENTS or self.pending_bytes > MAX_JSON_BYTES:
                self.fail(ValueError("too many Muse events before chat acknowledgement"))
            else:
                self.pending.append(event)
            return
        message_id = payload.get("message_id") or event.get("message_id") or payload.get("id")
        if not isinstance(message_id, str) or not message_id:
            return
        parent = payload.get("reply_to_message_id") or payload.get("parent_message_id")
        if parent is not None and (not isinstance(parent, str) or parent not in self.user_ids | self.messages.keys()):
            return
        if message_id not in self.messages:
            # A start/done without a parent cannot acquire ownership of a turn.
            # Only later deltas of an already correlated message may omit it.
            if not parent:
                return
            if len(self.messages) >= MAX_MESSAGES:
                self.fail(ValueError("too many Muse messages in one turn"))
                return
            self.messages[message_id] = False
        text = payload.get("text") if kind == "delta.text_append" else payload.get("display_text", payload.get("content"))
        if isinstance(text, str) and text.strip():
            self.messages[message_id] = True
        completed = kind == "delta.message_done" or (kind == "message.assistant" and payload.get("display_text_ready") is not False)
        if completed and self.messages[message_id]:
            self.done.set_result(message_id)


class _Subscription:
    def __init__(self, on_event):
        self.done = _future()
        self.ready = asyncio.Event()
        self.on_event = on_event
        self.buffer = bytearray()
        self.ended = False

    def on_frame(self, frame):
        if self.done.done():
            return
        try:
            if frame.kind == "reset":
                self.ended = True
                raise ConnectionError("Muse subscription stream reset")
            if frame.kind == "response":
                data, self.ended = frame.value.body, frame.value.end_body
                if not 200 <= frame.value.status < 300:
                    raise MuseHTTPError("/chat/subscribe", frame.value.status)
                self.ready.set()
            else:
                data, self.ended = frame.value.data, frame.value.end_body
            self.buffer.extend(data)
            consumed = 0
            while (newline := self.buffer.find(b"\n", consumed)) != -1:
                self._line(self.buffer[consumed:newline])
                consumed = newline + 1
            del self.buffer[:consumed]
            if len(self.buffer) > MAX_EVENT_BYTES:
                raise ValueError("Muse subscription event too large")
            if self.ended:
                if self.buffer:
                    self._line(self.buffer)
                raise ConnectionError("Muse subscription ended")
        except (ValueError, UnicodeDecodeError, ConnectionError, MuseHTTPError) as exc:
            self.done.set_exception(exc)
            self.ready.set()

    def _line(self, line):
        if len(line) > MAX_EVENT_BYTES:
            raise ValueError("Muse subscription event too large")
        if line.strip():
            event = json.loads(line)
            if isinstance(event, dict):
                self.on_event(event)


class MuseSession(LinkSession):
    """Registered gadget session with one-at-a-time voice and capture requests.

    ``converse`` returns MP3 bytes for the first complete correlated reply. Both
    the registration wait and complete turn are bounded by ``turn_timeout``.
    Reconnect by constructing a new MuseSession, as with the upstream SDK.
    """

    def __init__(self, *, turn_timeout=90.0, request_timeout=20.0,
                 max_audio_bytes=16 * 1024 * 1024, **kwargs):
        super().__init__(**kwargs)
        self.turn_timeout = turn_timeout
        self.request_timeout = request_timeout
        self.max_audio_bytes = max_audio_bytes
        self.last_registered_at: float | None = None
        self._registration_ready = asyncio.Event()
        self._session_error: Exception | None = None
        self._turn_lock = asyncio.Lock()
        self._turn: _Turn | None = None
        self._subscription: _Subscription | None = None
        self._subscription_id: int | None = None

    async def run(self, stop):
        try:
            return await super().run(stop)
        finally:
            self._session_error = ConnectionError("Muse session ended")
            self.registered_at = None
            self._registration_ready.set()
            if self._subscription is not None:
                self._subscription.ready.set()
            if self._turn is not None:
                self._turn.fail(self._session_error)

    def _handle(self, message):
        outcome = super()._handle(message)
        if message.get("id") == self._register_id and message.get("method") is None:
            if message.get("error"):
                self._session_error = ConnectionError("Muse device registration rejected")
            else:
                self.last_registered_at = self.registered_at
            self._registration_ready.set()
        return outcome

    async def send(self, message):
        # The SDK's default send encrypts before acquiring its write lock. An
        # invoke result must not advance the nonce ahead of a queued HTTP request.
        async with self._send_lock:
            frames = self._transport.encrypt_body_chunk(self._stream_id, encode_message(message))
            try:
                for frame in frames:
                    await self._ws.send(frame)
            except BaseException:
                await self._ws.close()
                raise

    async def send_chat(self, message: str, session_id: str | None = None) -> dict:
        """Keep the SDK's text-chat API on the same serialized request path."""
        body = {"message": message, "device_id": self._device.node_id}
        if session_id:
            body["session_id"] = session_id
        async with asyncio.timeout(self.turn_timeout):
            await self._wait_registered()
            status, response, _ = await self._request("POST", "/chat/stream", json.dumps(body).encode())
        try:
            decoded = json.loads(response) if response else None
        except (ValueError, UnicodeDecodeError):
            decoded = response.decode("utf-8", errors="replace")[:2000]
        return {"ok": 200 <= status < 300, "status": status, "response": decoded}

    async def _wait_registered(self):
        await self._registration_ready.wait()
        if self._session_error is not None:
            raise self._session_error
        if self.registered_at is None:
            raise ConnectionError("Muse device has not registered")

    async def _start_request(self, method, path, body, receiver, *, accept=None):
        headers = [Header("x-app-id", APP_ID), Header("x-request-id", str(uuid.uuid4()))]
        if body:
            headers.append(Header("Content-Type", "application/json"))
        if accept:
            headers.append(Header("Accept", accept))
        # Encryption advances Noise's send nonce. Acquire the same lock the
        # SDK uses before encrypting, so cancellation while queued is harmless.
        async with self._send_lock:
            encrypted = self._transport.encrypt_http_request(method, path, body, headers=headers)
            self._requests[encrypted.stream_id] = receiver
            try:
                for frame in encrypted.frames:
                    await self._ws.send(frame)
            except BaseException:
                self._requests.pop(encrypted.stream_id, None)
                # Partially sent encrypted frames cannot safely be reused.
                await self._ws.close()
                raise
        return encrypted.stream_id

    async def _reset_request(self, stream_id):
        if self._session_error is not None:
            return
        try:
            async with asyncio.timeout(min(1.0, self.request_timeout)):
                async with self._send_lock:
                    frames = self._transport.encrypt_reset(stream_id, reason="request cancelled")
                    for frame in frames:
                        await self._ws.send(frame)
        except asyncio.CancelledError:
            await self._ws.close()
            raise
        except Exception:
            await self._ws.close()

    async def _request(self, method, path, body=b"", *, limit=MAX_JSON_BYTES, accept=None, guard=None):
        receiver = _Response(limit)
        stream_id = None
        try:
            async with asyncio.timeout(self.request_timeout):
                stream_id = await self._start_request(method, path, body, receiver, accept=accept)
                if guard is not None:
                    await asyncio.wait({receiver.done, guard}, return_when=asyncio.FIRST_COMPLETED)
                    if guard.done():
                        await guard
                return await receiver.done
        finally:
            if stream_id is not None:
                self._requests.pop(stream_id, None)
                if not receiver.ended:
                    await self._reset_request(stream_id)
            if not receiver.done.done():
                receiver.done.cancel()

    async def _ensure_subscription(self):
        if self._subscription is not None and not self._subscription.done.done():
            return self._subscription
        if self._subscription_id is not None:
            self._requests.pop(self._subscription_id, None)
            if not self._subscription.ended:
                await self._reset_request(self._subscription_id)
        subscription = self._subscription = _Subscription(self._on_event)
        subscription.done.add_done_callback(self._subscription_ended)
        try:
            async with asyncio.timeout(self.request_timeout):
                self._subscription_id = await self._start_request(
                    "POST", "/chat/subscribe", b"{}", subscription, accept="application/x-ndjson",
                )
                await subscription.ready.wait()
                if self._session_error is not None:
                    raise self._session_error
                if subscription.done.done():
                    await subscription.done
        except BaseException:
            if self._subscription_id is not None:
                self._requests.pop(self._subscription_id, None)
                if not subscription.ended:
                    await self._reset_request(self._subscription_id)
            subscription.done.cancel()
            self._subscription_id = None
            self._subscription = None
            raise
        return subscription

    def _subscription_ended(self, future):
        if not future.cancelled() and self._turn is not None:
            self._turn.fail(future.exception() or ConnectionError("Muse subscription ended"))

    def _on_event(self, event):
        if self._turn is not None:
            self._turn.on_event(event)

    async def converse(self, audio_wav: bytes, image: bytes | None = None, prompt: str = "") -> bytes:
        _validate_wav(audio_wav)
        items = [_file_item(audio_wav, "audio/wav", "voice_note.wav")]
        if image is not None:
            items.append(_file_item(image, "image/jpeg", "capture.jpg"))
        body = json.dumps({"message": prompt, "output_modality": "voice", "items": items,
                           "device_id": self._device.node_id}).encode()
        async with asyncio.timeout(self.turn_timeout):
            async with self._turn_lock:
                await self._wait_registered()
                subscription = await self._ensure_subscription()
                turn = self._turn = _Turn()
                try:
                    status, ack, _ = await self._request("POST", "/chat/stream", body, guard=subscription.done)
                    if not 200 <= status < 300:
                        raise MuseHTTPError("/chat/stream", status)
                    turn.acknowledge(ack)
                    message_id = await turn.done
                    query = "?message_id=" + quote(message_id, safe="")
                    for path in ("/api/voice/tts-stream", "/voice/tts-stream"):
                        status, audio, headers = await self._request(
                            "GET", path + query, limit=self.max_audio_bytes, accept="audio/mpeg",
                            guard=subscription.done,
                        )
                        if status != 404:
                            break
                    if not 200 <= status < 300:
                        raise MuseHTTPError(path, status)
                    content_type = headers.get("content-type", "").split(";")[0].lower().strip()
                    if content_type and content_type not in {"audio/mpeg", "audio/mp3", "application/octet-stream"}:
                        raise ValueError("Muse TTS response is not MP3 audio")
                    if not audio:
                        raise ValueError("Muse TTS returned empty audio")
                    return audio
                finally:
                    self._turn = None
                    if not turn.done.done():
                        turn.done.cancel()

    async def capture(self, data: bytes, mime_type: str) -> None:
        body = json.dumps({"message": "", "items": [_file_item(data, mime_type)],
                           "device_id": self._device.node_id}).encode()
        async with asyncio.timeout(self.turn_timeout):
            async with self._turn_lock:
                await self._wait_registered()
                status, _, _ = await self._request("POST", "/chat/stream", body)
                if not 200 <= status < 300:
                    raise MuseHTTPError("/chat/stream", status)
