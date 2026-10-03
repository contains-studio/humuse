"""Short-lived, acknowledged Pin commands. Never execute on the companion host."""

import asyncio
import concurrent.futures
import json
import math
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass


COMMANDS = {
    "get_status": {
        "description": "Read the awake OpenPin's current battery, charging and activity status.",
        "required": {}, "optional": {}, "timeout_ms": 30000,
    },
    "ring": {
        "description": "Play a short audible chime on the awake OpenPin at its current volume.",
        "required": {}, "optional": {}, "timeout_ms": 30000,
    },
    "set_volume": {
        "description": "Set OpenPin speaker volume from 0 (silent) to 1 (full).",
        "required": {"volume": {"type": "number", "description": "Volume between 0 and 1."}},
        "optional": {}, "timeout_ms": 30000,
    },
    "capture_photo": {
        "description": "Take one photo with OpenPin, with an audible cue, and save it to its gallery.",
        "required": {}, "optional": {}, "timeout_ms": 45000,
    },
    "record_video": {
        "description": "Record a short OpenPin video with audible cues and save it to its gallery.",
        "required": {},
        "optional": {"duration_seconds": {
            "type": "integer", "description": "Duration from 1 to 15 seconds; default 5.",
        }},
        "timeout_ms": 45000,
    },
}


def _json_object(value, label, limit=16384):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    try:
        encoded = json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError(f"{label} must contain JSON values") from exc
    if len(encoded.encode()) > limit:
        raise ValueError(f"{label} is too large")
    return json.loads(encoded)


def validate_command(command, params, timeout_ms=None):
    if not isinstance(command, str) or command not in COMMANDS:
        raise ValueError("Unsupported Pin command")
    params = _json_object(params, "Command parameters", 1024)
    spec = COMMANDS[command]
    if set(params) - (set(spec["required"]) | set(spec["optional"])):
        raise ValueError("Unsupported command parameter")
    if set(spec["required"]) - set(params):
        raise ValueError("Missing command parameter")
    if command == "set_volume":
        volume = params["volume"]
        if type(volume) not in (int, float) or not math.isfinite(volume) or not 0 <= volume <= 1:
            raise ValueError("Volume must be a number between 0 and 1")
    if command == "record_video":
        duration = params.setdefault("duration_seconds", 5)
        if type(duration) is not int or not 1 <= duration <= 15:
            raise ValueError("Video duration must be an integer from 1 to 15 seconds")
    if timeout_ms is None:
        timeout_ms = spec["timeout_ms"]
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 45000:
        raise ValueError("Command timeout must be between 1 and 45000 milliseconds")
    return params, timeout_ms


@dataclass
class _Command:
    id: str
    name: str
    params: dict
    deadline: float
    expires_at: int
    future: asyncio.Future
    delivered: bool = False


class PinControls:
    """A single Pin's in-memory queue, owned by the HTTP server's event loop.

    Poll retries return the same command until acknowledged. The Pin journals IDs
    before execution so retries cannot repeat a camera capture or other action.
    No commands survive a companion restart.
    """

    def __init__(self, *, max_pending=8):
        self._loop = None
        self._pending = OrderedDict()
        self._completed = OrderedDict()
        self._max_pending = max_pending
        self._last_seen = None
        self._last_seen_mono = None
        self._status = {}
        self._closed = False

    def bind_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("PinControls belongs to another event loop")
        self._loop = loop

    async def submit(self, command, params, timeout_ms=None):
        self.bind_loop()
        params, timeout_ms = validate_command(command, params, timeout_ms)
        if self._closed:
            return {"ok": False, "error": "Pin control service is stopped"}
        if len(self._pending) >= self._max_pending:
            return {"ok": False, "error": "Pin command queue is full"}
        item = _Command(
            secrets.token_urlsafe(24), command, params,
            time.monotonic() + timeout_ms / 1000,
            int(time.time() * 1000) + timeout_ms, self._loop.create_future(),
        )
        self._pending[item.id] = item
        try:
            return await asyncio.wait_for(item.future, timeout_ms / 1000)
        except TimeoutError:
            return {
                "ok": False, "delivered": item.delivered,
                "error": "Pin command timed out; it may have run without an acknowledgement"
                if item.delivered else "Pin did not pick up the command before it expired",
            }
        finally:
            self._pending.pop(item.id, None)

    async def poll(self, status=None):
        self.bind_loop()
        if status is not None:
            status = _json_object(status, "Pin status", 2048)
            if set(status) - {"battery", "isCharging", "activity"}:
                raise ValueError("Unsupported Pin status field")
            if "battery" in status and (
                type(status["battery"]) not in (int, float) or not 0 <= status["battery"] <= 1
            ):
                raise ValueError("Invalid battery level")
            if "isCharging" in status and type(status["isCharging"]) is not bool:
                raise ValueError("Invalid charging state")
            if "activity" in status and (
                not isinstance(status["activity"], str) or len(status["activity"]) > 64
            ):
                raise ValueError("Invalid activity")
            self._status = status
        self._last_seen, self._last_seen_mono = time.time(), time.monotonic()
        if self._closed:
            return None
        for item in self._pending.values():
            remaining = int((item.deadline - time.monotonic()) * 1000)
            if remaining <= 0 or item.future.done():
                continue
            item.delivered = True
            return {
                "id": item.id, "name": item.name, "params": dict(item.params),
                "expiresAt": item.expires_at, "timeoutMs": remaining,
            }
        return None

    async def complete(self, command_id, result):
        self.bind_loop()
        if not isinstance(command_id, str):
            raise ValueError("Command ID must be a string")
        result = _json_object(result, "Command result")
        # The SDK merges this object into a link.result envelope. Accept only
        # hardware response fields, so a device cannot replace its RPC ID/method.
        if set(result) - {"ok", "error", "battery", "isCharging", "activity", "volume", "captureId"}:
            raise ValueError("Unsupported Pin command result field")
        if type(result.get("ok")) is not bool:
            raise ValueError("Command result must include a boolean ok")
        if command_id in self._completed:
            if self._completed[command_id] != result:
                raise ValueError("Command result conflicts with prior acknowledgement")
            return
        item = self._pending.get(command_id)
        if item is None or item.deadline <= time.monotonic() or item.future.done():
            raise KeyError("Unknown or expired Pin command")
        if not item.delivered:
            raise ValueError("Pin command has not been delivered")
        self._completed[command_id] = result
        while len(self._completed) > 128:
            self._completed.popitem(last=False)
        item.future.set_result(result)

    async def snapshot(self):
        return {
            "online": not self._closed and self._last_seen_mono is not None
            and time.monotonic() - self._last_seen_mono < 15,
            "lastSeen": self._last_seen,
            "status": dict(self._status),
            "pending": sum(not item.future.done() for item in self._pending.values()),
        }

    async def close(self):
        self._closed = True
        await self.reset()

    async def reset(self):
        """Invalidate commands and telemetry when a Pin is relinked."""
        for item in self._pending.values():
            if not item.future.done():
                item.future.set_result({"ok": False, "error": "Pin controls stopped or device was relinked"})
        self._pending.clear()
        self._completed.clear()
        self._last_seen = self._last_seen_mono = None
        self._status = {}

    def run(self, command, params, timeout_ms=None):
        """Muse SDK invokes executors in a worker thread; never block the loop."""
        if self._loop is None or not self._loop.is_running() or self._closed:
            return {"ok": False, "error": "Pin control service is unavailable"}
        try:
            if asyncio.get_running_loop() is self._loop:
                return {"ok": False, "error": "Pin executor must run in a worker thread"}
        except RuntimeError:
            pass
        future = asyncio.run_coroutine_threadsafe(self.submit(command, params, timeout_ms), self._loop)
        try:
            return future.result(timeout=50)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return {"ok": False, "error": "Pin control service timed out"}
        except (ValueError, RuntimeError, concurrent.futures.CancelledError) as exc:
            return {"ok": False, "error": str(exc)}
