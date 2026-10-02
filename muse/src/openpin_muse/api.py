"""Local OpenPin HTTP backend for a Linux companion connected to Muse.

deviceId is a bearer credential because the existing Android client sends it
inside request bodies. Serve through HTTPS outside loopback, and disable access
logging on the server and reverse proxy: pairing URLs contain one-use secrets.
"""

import asyncio
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time
from typing import Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aiohttp import BodyPartReader, web

from .protocol import (
    HEADER_SIZE,
    MAX_VOICE_BYTES,
    PayloadTooLarge,
    ProtocolError,
    decode_json,
    encode_voice_response,
    parse_voice_header,
    parse_voice_request,
    validate_capture,
)


MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024
MAX_STATE_BYTES = 16 * 1024
STATE_FILENAME = "pairing.json"


class Bridge(Protocol):
    ready: bool

    async def voice(self, audio: bytes, image: bytes | None, translate: bool) -> bytes: ...

    async def capture(self, data: bytes, mime_type: str) -> None: ...


def validate_public_url(public_url: str) -> str:
    """Accept an origin, never trust a request Host or forwarded header."""
    if not isinstance(public_url, str) or any(char.isspace() or ord(char) < 32 for char in public_url):
        raise ValueError("public_url must be an HTTP(S) origin")
    try:
        parsed = urlsplit(public_url)
        host = parsed.hostname
        parsed.port  # Validate port syntax and range.
    except ValueError as exc:
        raise ValueError("Invalid public_url") from exc
    if (not host or parsed.scheme not in ("http", "https") or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or "\\" in public_url):
        raise ValueError("public_url must be an HTTP(S) origin without credentials or a path")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        if not loopback:
            raise ValueError("Non-loopback public_url requires HTTPS")
    return f"{parsed.scheme}://{parsed.netloc}"


class _StateStore:
    """A private, atomically replaced state file shared by the CLI and server.

    The separate lock file remains stable across replacements, making code
    issuance and one-use consumption safe even in different processes.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ValueError("State directory must be a real directory")
        self.directory.chmod(0o700)
        self.path = self.directory / STATE_FILENAME

    @contextmanager
    def locked(self):
        fd = os.open(self.directory / ".pairing.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def read(self) -> dict:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"version": 1, "device_id": None, "pairing": None}
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("State file must be a private regular file")
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError("Invalid state file")
        try:
            value = decode_json(raw)
            if value.get("version") != 1:
                raise ValueError("Unsupported state file version")
            device_id = value.get("device_id")
            if device_id is not None and (not isinstance(device_id, str) or not 32 <= len(device_id) <= 256):
                raise ValueError("Invalid device state")
            pairing = value.get("pairing")
            if pairing is not None:
                if (not isinstance(pairing, dict) or not isinstance(pairing.get("hash"), str)
                        or len(pairing["hash"]) != 64 or type(pairing.get("expires")) not in (int, float)
                        or not isinstance(pairing.get("public_url"), str)):
                    raise ValueError("Invalid pairing state")
            return value
        except ProtocolError as exc:
            raise ValueError("Invalid state file") from exc

    def write(self, value: dict) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".pairing-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    def authenticate(self, candidate: object) -> bool:
        if not isinstance(candidate, str) or not 1 <= len(candidate) <= 256:
            return False
        with self.locked():
            expected = self.read().get("device_id")
        return expected is not None and secrets.compare_digest(candidate.encode(), expected.encode())

    def consume(self, code: str, public_url: str) -> str | None:
        if not 1 <= len(code) <= 128:
            return None
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.locked():
            value = self.read()
            pairing = value.get("pairing")
            if (not pairing or not secrets.compare_digest(digest, pairing["hash"])
                    or pairing["expires"] <= time.time() or pairing["public_url"] != public_url):
                return None
            device_id = secrets.token_urlsafe(32)
            value.update(device_id=device_id, pairing=None)
            self.write(value)
            return device_id


def issue_pairing(state_dir: Path, public_url: str, ttl_seconds: int = 600) -> str:
    """Issue a secret QR URL; consuming it rotates the previous device bearer."""
    public_url = validate_public_url(public_url)
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
        raise ValueError("Pairing lifetime must be between 1 and 86400 seconds")
    store = _StateStore(state_dir)
    code = secrets.token_urlsafe(32)
    with store.locked():
        value = store.read()
        value["pairing"] = {"hash": hashlib.sha256(code.encode()).hexdigest(),
                            "expires": time.time() + ttl_seconds, "public_url": public_url}
        store.write(value)
    return f"{public_url}/api/dev/pair/{code}"


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status, headers={"Cache-Control": "no-store"})


async def _read_limited(stream, maximum: int) -> bytes:
    chunks = bytearray()
    while True:
        chunk = await stream.read(min(64 * 1024, maximum + 1 - len(chunks)))
        if not chunk:
            return bytes(chunks)
        chunks.extend(chunk)
        if len(chunks) > maximum:
            raise PayloadTooLarge("Request exceeds size limit")


async def _read_part(part: BodyPartReader, maximum: int) -> bytes:
    chunks = bytearray()
    while not part.at_eof():
        chunks.extend(await part.read_chunk())
        if len(chunks) > maximum:
            raise PayloadTooLarge("Multipart field exceeds size limit")
    return bytes(chunks)


def create_app(bridge: Bridge, state_dir: Path, *, public_url: str,
               time_zone: str = "UTC") -> web.Application:
    public_url = validate_public_url(public_url)
    zone = ZoneInfo(time_zone)
    store = _StateStore(state_dir)
    with store.locked():
        store.read()  # Refuse malformed/insecure state before serving traffic.

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except PayloadTooLarge:
            return _error(413, "Request exceeds size limit")
        except (ProtocolError, asyncio.IncompleteReadError, UnicodeDecodeError):
            return _error(400, "Malformed request")
        except web.HTTPException:
            raise
        except (ValueError, AssertionError):
            return _error(400, "Malformed request")

    app = web.Application(middlewares=[errors], client_max_size=MAX_CAPTURE_BYTES + MAX_JSON_BYTES)

    async def authorized(device_id):
        # CLI pairing shares this file lock. Never block the Noise event loop on it.
        return await asyncio.to_thread(store.authenticate, device_id)

    async def json_payload(request):
        if request.content_type != "application/json":
            raise ProtocolError("JSON content type required")
        if request.content_length is not None and request.content_length > MAX_JSON_BYTES:
            raise PayloadTooLarge("JSON request exceeds size limit")
        return decode_json(await _read_limited(request.content, MAX_JSON_BYTES))

    async def health(request):
        ready = bool(bridge.ready)
        return web.json_response({"ready": ready}, status=200 if ready else 503,
                                 headers={"Cache-Control": "no-store"})

    async def pair(request):
        device_id = await asyncio.to_thread(store.consume, request.match_info["code"], public_url)
        if device_id is None:
            return _error(404, "Pairing code is invalid or expired")
        return web.json_response({"baseUrl": public_url, "deviceId": device_id},
                                 headers={"Cache-Control": "no-store"})

    async def voice(request):
        if request.content_length is not None and request.content_length > MAX_VOICE_BYTES:
            raise PayloadTooLarge("Voice request exceeds size limit")
        header = await request.content.readexactly(HEADER_SIZE)
        metadata = parse_voice_header(header)
        if not await authorized(metadata.device_id):
            return _error(401, "Unauthorized device")
        expected = HEADER_SIZE + metadata.audio_size + metadata.image_size
        if request.content_length is not None and request.content_length != expected:
            raise ProtocolError("Body length does not match header")
        if not bridge.ready:
            return _error(503, "Muse is not ready")
        body = await request.content.readexactly(expected - HEADER_SIZE)
        if await request.content.read(1):
            raise ProtocolError("Unexpected data after media")
        parsed = parse_voice_request(header + body)
        try:
            mp3 = await bridge.voice(parsed.audio, parsed.image,
                                     translate=request.path.endswith("/translate"))
        except ValueError:
            return _error(400, "Media could not be decoded")
        except ConnectionError:
            return _error(503, "Muse is not available")
        except asyncio.TimeoutError:
            return _error(504, "Muse response timed out")
        except Exception:
            return _error(502, "Muse could not process this request")
        try:
            response = encode_voice_response(mp3)
        except ValueError:
            return _error(502, "Muse returned no audio")
        return web.Response(body=response, content_type="application/octet-stream",
                            headers={"Cache-Control": "no-store"})

    async def home(request):
        payload = await json_payload(request)
        if not await authorized(payload.get("deviceId")):
            return _error(401, "Unauthorized device")
        # Android deliberately formats this epoch as UTC. Shift the selected
        # zone's wall clock into UTC, matching OpenPin's existing home contract.
        wall_clock = datetime.fromtimestamp(time.time(), zone).replace(tzinfo=timezone.utc)
        return web.json_response({"time": int(wall_clock.timestamp() * 1000)})

    async def locate(request):
        payload = await json_payload(request)
        if not await authorized(payload.get("deviceId")):
            return _error(401, "Unauthorized device")
        return _error(503, "Wi-Fi geolocation is not supported by this companion")

    async def capture(request):
        if request.content_length is not None and request.content_length > MAX_CAPTURE_BYTES + MAX_JSON_BYTES:
            raise PayloadTooLarge("Capture exceeds size limit")
        if request.content_type != "multipart/form-data":
            raise ProtocolError("Multipart upload required")
        reader = await request.multipart()
        device_id = None
        data = None
        mime_type = None
        seen = set()
        while (part := await reader.next()) is not None:
            if not isinstance(part, BodyPartReader) or part.name not in {"deviceId", "file"} or part.name in seen:
                raise ProtocolError("Unexpected multipart field")
            seen.add(part.name)
            if part.headers.get("Content-Transfer-Encoding", "binary") != "binary":
                raise ProtocolError("Encoded multipart fields are not supported")
            if part.name == "deviceId":
                device_id = (await _read_part(part, 256)).decode("utf-8")
                if not await authorized(device_id):
                    return _error(401, "Unauthorized device")
            else:
                suffix = Path(part.filename or "").suffix.lower()
                mime_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".mp4": "video/mp4"}.get(suffix)
                if mime_type is None:
                    raise ProtocolError("Capture must be JPEG or MP4")
                content_type = part.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].strip()
                if content_type not in {mime_type, "application/octet-stream"}:
                    raise ProtocolError("Capture content type does not match filename")
                data = await _read_part(part, MAX_CAPTURE_BYTES)
        if not await authorized(device_id):
            return _error(401, "Unauthorized device")
        if data is None or mime_type is None:
            raise ProtocolError("Missing capture file")
        validate_capture(data, mime_type)
        if not bridge.ready:
            return _error(503, "Muse is not ready")
        try:
            await bridge.capture(data, mime_type)
        except ValueError:
            return _error(400, "Media could not be decoded")
        except ConnectionError:
            return _error(503, "Muse is not available")
        except asyncio.TimeoutError:
            return _error(504, "Muse capture timed out")
        except Exception:
            return _error(502, "Muse could not process this capture")
        return web.Response(status=200)

    app.router.add_get("/healthz", health)
    app.router.add_post("/api/dev/pair/{code}", pair)
    app.router.add_post("/api/dev/handle", voice)
    app.router.add_post("/api/dev/translate", voice)
    app.router.add_post("/api/dev/home-data", home)
    app.router.add_post("/api/dev/locate", locate)
    app.router.add_post("/api/dev/upload-capture", capture)
    return app
