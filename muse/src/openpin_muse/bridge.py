"""Reuse Muse's credential rotation/reconnect loop without its shell executor."""

import asyncio
import logging
import time

from musegadget.link_client import DeviceDescription, Outcome
from musegadget.service import DEFAULT_NOISE_HOST, Service

from openpin_muse.media import decode_ogg
from openpin_muse.session import MuseSession

log = logging.getLogger(__name__)


class NoCommands:
    def run(self, command, params, timeout_ms):
        return {"ok": False, "error": "This OpenPin gateway does not execute remote commands"}


class MuseBridge(Service):
    def __init__(self, identity, sdk_token, languages=("English", "Spanish")):
        super().__init__(
            identity=identity, executor=NoCommands(), sdk_token=sdk_token, display_name="OpenPin",
        )
        self.languages = languages
        self._turn = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._current is not None and self._current.registered_at is not None

    async def _session(self, vm: dict, pairing: dict) -> tuple[Outcome, float]:
        # SDK homehub registration is intentional: family 'link' receives ESP32 OTA.
        session = MuseSession(
            noise_host=pairing.get("noise_host") or DEFAULT_NOISE_HOST,
            vm_id=vm["vm_id"] or vm["vm_name"], vm_auth_token=vm["vm_auth_token"],
            device=DeviceDescription(self.identity.node_id, "OpenPin", "0.1.0", {}),
            run_command=self.executor.run,
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
        if self._turn.locked():
            raise ConnectionError("The Pin is already handling a request")
        async with self._turn:
            await self._current.capture(data, mime_type)
