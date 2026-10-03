"""Reuse Muse's credential rotation/reconnect loop without its shell executor."""

import asyncio
import logging
import time

from musegadget.link_client import DeviceDescription, Outcome
from musegadget.executor import ok, error
from musegadget.service import DEFAULT_NOISE_HOST, Service

from openpin_muse.media import decode_ogg
from openpin_muse.session import MuseSession

log = logging.getLogger(__name__)


class NoCommands:
    def run(self, command, params, timeout_ms):
        return {"ok": False, "error": "This OpenPin gateway does not execute remote commands"}


class MuseBridge(Service):
    def __init__(self, identity, sdk_token, languages=("English", "Spanish"), *, controls=None, environment=None):
        super().__init__(
            identity=identity, executor=NoCommands(), sdk_token=sdk_token, display_name="OpenPin",
        )
        self.languages = languages
        self.controls = controls
        self.environment = environment
        self._turn = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._current is not None and self._current.registered_at is not None

    async def _session(self, vm: dict, pairing: dict) -> tuple[Outcome, float]:
        # SDK homehub registration is intentional: family 'link' receives ESP32 OTA.
        commands = {}
        executor = self.executor.run
        if self.controls is not None:
            from .controls import COMMANDS
            self.controls.bind_loop()
            commands = dict(COMMANDS)
            executor = self.controls.run
        if self.environment is not None:
            commands["get_environment"] = {
                "description": "Read the Pin's last known location, its source and age, and current weather. Configured coordinates are not a live Pin fix.",
                "required": {}, "optional": {}, "timeout_ms": 20000,
            }
            loop = asyncio.get_running_loop()
            delegate = executor

            def executor(command, params, timeout_ms):
                if command != "get_environment":
                    return delegate(command, params, timeout_ms)
                if params:
                    return {"ok": False, "error": "get_environment takes no parameters"}
                future = asyncio.run_coroutine_threadsafe(self.environment.context(), loop)
                try:
                    return {"ok": True, **future.result(timeout=20)}
                except Exception:
                    future.cancel()
                    return {"ok": False, "error": "Location or weather is unavailable"}

        # The Pin HTTP API uses flat acknowledgements. Muse's link protocol
        # requires successful values inside payload, as in the SDK executor.
        def muse_executor(command, params, timeout_ms):
            result = executor(command, params, timeout_ms)
            if result.get("ok") is True:
                return ok({key: value for key, value in result.items() if key != "ok"})
            return error(result.get("error", "Pin command failed"))

        session = MuseSession(
            noise_host=pairing.get("noise_host") or DEFAULT_NOISE_HOST,
            vm_id=vm["vm_id"] or vm["vm_name"], vm_auth_token=vm["vm_auth_token"],
            device=DeviceDescription(self.identity.node_id, "OpenPin", "0.2.0", commands),
            run_command=muse_executor,
        )
        self._current = session
        try:
            outcome = await session.run(self._stop)
        except Exception as exc:
            # Do not log credential-bearing URL/response strings from network errors.
            log.warning("Muse session failed (%s); reconnecting", type(exc).__name__)
            outcome = Outcome.CLOSED
        finally:
            self._current = None
        return outcome, time.monotonic() - (session.last_registered_at or time.monotonic())

    async def voice(self, audio: bytes, image: bytes | None, translate: bool) -> bytes:
        if not self.ready:
            raise ConnectionError("Muse is not connected")
        if self._turn.locked():
            raise ConnectionError("The Pin is already handling a request")
        async with self._turn:
            session = self._current
            wav = await decode_ogg(audio)
            prompt = ""
            if translate:
                first, second = self.languages
                prompt = (
                    f"Translate the attached speech between {first} and {second}. "
                    "Detect which of these languages was spoken and reply only with its translation "
                    "into the other language."
                )
            return await session.converse(wav, image, prompt)

    async def capture(self, data: bytes, mime_type: str) -> None:
        if not self.ready:
            raise ConnectionError("Muse is not connected")
        # Capture forwarding waits for voice turns. The HTTP upload already
        # saved the original, so a camera command can acknowledge immediately.
        async with self._turn:
            if not self.ready:
                raise ConnectionError("Muse is not connected")
            await self._current.capture(data, mime_type)
