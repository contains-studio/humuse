# OpenPin × Muse: integration findings

Research date: 2026-10-02. OpenPin revision: `4dd83a56175ad88774bd211071785b2ba431f540`.
Muse SDK revision: `7e7123e2815d3e7e3c0f2ca330f576290ae6a6a9`.

## Why a companion backend

OpenPin is an Android Kotlin/Compose client, an ARM64 shell daemon, and a separate
TypeScript backend. It already has microphone recording, camera capture, gesture
interpretation, laser UI and MP3 playback. The client sends requests through
command files which its shell daemon executes with curl. Its QR linking API lets
another backend supply its own URL without rebuilding the APK.

The [OpenPin team's device research](https://github.com/PenumbraOS/docs#difficulties)
documents custom SELinux restrictions that prevent ordinary apps from using
direct networking, DNS and sockets. The shell user can access the network; file
communication crosses the app/shell boundary. This explains OpenPin's existing
daemon design. Adding Android INTERNET permission or embedding the Python SDK
would not by itself solve that restriction. BLE service access on stock Pin
firmware was not verified.

Muse's [Linux SDK](https://github.com/facebookincubator/muse-gadget-sdk/tree/7e7123e2815d3e7e3c0f2ca330f576290ae6a6a9/linux)
uses BlueZ/D-Bus for BLE pairing, Python for encrypted sessions, and Linux service
facilities. It is not an Android SDK. A native port would need shell-side networking,
a compatible runtime, a BLE strategy and hardware testing. The implemented backend
uses the existing OpenPin network path and places the Muse SDK on a supported Linux
host. This is an integration into OpenPin's backend architecture, not a standalone
Muse firmware replacement.

## Verified wire contracts

OpenPin's
[BackendManager](https://github.com/MaxMaeder/OpenPin/blob/4dd83a56175ad88774bd211071785b2ba431f540/client/apps/primaryapp/src/main/java/org/openpin/primaryapp/backend/BackendManager.kt)
posts a scanned QR URL and saves `{baseUrl, deviceId}`. Voice requests contain a
512-byte NUL-padded JSON header, followed by optional JPEG bytes and Ogg/Opus audio.
Replies use a 512-byte JSON header followed by MP3. The Pin records 16 kHz audio
and limits a voice hold to 20 seconds. Preserving these contracts avoids firmware
and APK changes.

Muse's
[Linux session](https://github.com/facebookincubator/muse-gadget-sdk/blob/7e7123e2815d3e7e3c0f2ca330f576290ae6a6a9/linux/src/musegadget/link_client.py)
fetches VM credentials, connects through an authenticated WebSocket, negotiates
Noise XX, and multiplexes virtual HTTP requests inside encrypted envelopes. Device
registration stays in the `homehub` family: upstream explicitly warns that `link`
devices receive ESP32 OTA pushes. This implementation advertises a fixed set of Pin status, speaker and camera
commands plus location/weather context. It advertises no shell, arbitrary file,
or OTA commands.

The Linux SDK's `send_chat` returns a submission acknowledgment, not a spoken reply.
The actual voice protocol is in the
[ESP32 voice implementation](https://github.com/facebookincubator/muse-gadget-sdk/blob/7e7123e2815d3e7e3c0f2ca330f576290ae6a6a9/esp32/components/muse/muse_chat_session.cpp)
and [voice payload definitions](https://github.com/facebookincubator/muse-gadget-sdk/blob/7e7123e2815d3e7e3c0f2ca330f576290ae6a6a9/esp32/components/muse/muse_chat_priv.h):

1. Subscribe through `POST /chat/subscribe` before submitting a turn.
2. Post a base64 PCM16 WAV file item to `/chat/stream` with `output_modality: voice`.
3. Correlate assistant events to the acknowledgment's message IDs.
4. Fetch MP3 from `/api/voice/tts-stream?message_id=...`; try `/voice/tts-stream`
   only if the first path returns 404.

The gateway combines that voice payload with Linux's device attribution field.
Photo/video file items and voice-associated JPEGs use the same attachment schema.
These combinations are implementation inferences from the published protocol;
their acceptance by a live Muse VM remains to be verified. Translation is a Muse
instruction using the configured language pair, rather than OpenPin.Center's
separate speech/translation services.

The Muse transport caps each attachment at 8 MiB; the gallery accepts originals
up to 128 MiB and converts oversized media for forwarding. OpenPin uses CameraX's highest
available video quality for its 15-second clips; a physical capture is needed to
measure typical sizes. Originals are retained privately and only the Muse copy is transcoded. Failed
conversions remain available in the gallery with a retry action.

## Verification boundary

The user's Pin is still boxed. No hardware was flashed, activated, paired, or
queried. No real microphone recordings or account tokens were used. Automated
verification uses generated audio, HTTP clients, and a simulated Muse server that
performs the real Noise handshake and encrypted request/response framing.
Live BLE, account authorization, network/TLS reachability and Pin gestures remain
the final acceptance tests.

## Completed software extensions

- Multi-message responses follow the SDK voice client's three-second settle
  period and wait for known unfinished messages. MP3 segments are decoded and
  encoded into one recording so per-file duration headers do not stop playback.
- Location follows Google's [Wi-Fi geolocation contract](https://developers.google.com/maps/documentation/geolocation/requests-geolocation),
  with IP fallback disabled. Weather uses [Open-Meteo's current-weather contract](https://open-meteo.com/en/docs).
  The source of configured versus Pin location is preserved.
- Local gallery persistence and private browser sessions are companion features;
  they do not use OpenPin.Center's Firebase account system.
- Remote controls use the Muse SDK's existing `link.invoke`/`link.result` protocol
  and a new authenticated Pin poll/ack API. The Android app journals command IDs,
  serializes camera actions with gestures, and polls only while awake.

These extensions are locally testable. Physical sensor behavior, power transitions,
BLE pairing, live Muse service compatibility and TLS remain hardware/account
acceptance checks.
