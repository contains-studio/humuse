# Muse for OpenPin

This backend connects an **OpenPin-enabled Humane Ai Pin** to Muse. Keep OpenPin's
shell daemon and point its QR linking screen at this backend. The usual voice
and camera gestures remain available; install this fork’s updated Android client
to enable remote controls.

**A Linux companion is required.** This does not install Muse directly on stock
Pin firmware or activate a boxed Pin. The companion pairs with the Muse phone app,
holds the Muse connection, and translates OpenPin's requests. No physical Pin or
live Muse account was used during development; the automated tests use a local
simulated Muse peer with real encryption.

```text
Pin microphone/camera → OpenPin shell daemon → HTTPS → this backend
                                                     ↓
Pin speaker          ← 512-byte header + MP3 ← Muse encrypted session
```

## What it supports

- One-finger hold: send the recording to Muse and play its spoken answer.
- Tap then hold: include the Pin's JPEG with the voice request.
- Two-finger hold: translate between two configured languages (English/Spanish by default).
- Photo/video uploads up to 128 MiB: originals are saved in a private gallery on
  the companion, even while Muse is offline. Large captures are converted to fit
  Muse's 8 MiB attachment limit before forwarding. Originals remain unchanged.
- A responsive browser dashboard for viewing, playing, downloading, deleting,
  and retrying failed media submissions. The default storage quota is 5 GiB;
  reaching it rejects new uploads without deleting older captures.
- Home-screen weather and location, using Open-Meteo and optional Google Wi-Fi
  geolocation. A configured place can supply weather before the Pin is connected.
- Remote status/battery, ring, speaker volume, photos and 1–15 second videos from
  Muse or the dashboard. Camera actions have audible cues. Controls operate when
  the updated Pin app is awake and paired, and report an acknowledgment or expiry.
- Multiple spoken messages per reply. The adapter keeps replies in order, waits
  for unfinished messages, and uses the SDK's three-second quiet period to finish.

Voice replies still require parent IDs linking them to the request. This prevents
another conversation's response from playing on the Pin. Push-to-talk remains the
input method. The adapter does not provide stock Humane cloud activation,
OpenPin.Center accounts, wake-word listening, shell access, or firmware/ESP32 OTA.

## Before unboxing / installing

Read [OpenPin's installation instructions](https://openpin.org). You need a working
OpenPin client and daemon, Pin Wi-Fi, and the hardware connection required by that
installer. This repository's backend does not perform firmware installation or
assume a factory-sealed Pin has working cloud activation. The [research notes](RESEARCH.md)
explain the software and hardware boundaries.

For the companion, prepare:

- Linux with Python 3.11+, Bluetooth LE, BlueZ, D-Bus and PyGObject.
- `ffmpeg` for the Pin's Ogg/Opus recordings.
- A Muse account, Muse phone app with **Settings → Devices → Developer mode**, and an
  [SDK token](https://gadgets.muse.ai/settings/sdk-tokens). Review the
  [Gadget SDK Terms](https://gadgets.muse.ai/sdk-terms).
- An HTTPS address reachable by the Pin. Its daemon's curl must trust the TLS
  certificate. Use a TLS reverse proxy in front of this process; do not disable
  certificate checking. HTTP is accepted only for loopback development.

## Install and pair the companion

From this `muse` directory on the Linux companion:

```sh
sudo apt-get install git python3-venv python3-pip python3-dbus python3-gi bluez ffmpeg
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .
.venv/bin/openpin-muse --state-dir .muse-state token
```

`token` uses a hidden prompt. It stores the token with owner-only permissions, not
in shell history or command arguments. `.muse-state` must remain private and is
gitignored. Do not publish it, its pairing QR, or Muse credentials.

As required by the Muse SDK for Android phone pairing, set this section in
`/etc/bluetooth/main.conf`, preserving its other settings:

```ini
[GATT]
ExchangeMTU = 256
```

Restarting Bluetooth will interrupt any current Bluetooth connections. When ready:

```sh
sudo systemctl restart bluetooth
sudo .venv/bin/openpin-muse --state-dir "$PWD/.muse-state" pair-muse
sudo chown -R "$(id -u):$(id -g)" .muse-state
```

During the 10-minute pairing window, add the printed `MuseGadgetXXXXXX` device in
the Muse phone app. Choose the companion's existing network when prompted. The
upstream pairing flow uses the companion's current connection; it does not
configure Wi-Fi on the Pin. Elevated permission is used for BlueZ pairing only;
run the backend as your ordinary account. The state ownership command restores
access to files created during pairing.

Do **not** run the upstream Linux install script for this integration: it starts
a separate general-purpose gadget service. This backend reuses its pairing and
connection code, registers only the documented Pin controls plus `get_environment`, and never
grants Muse shell or arbitrary file access to the companion.

## Start the backend and link the Pin

```sh
.venv/bin/openpin-muse --state-dir .muse-state serve \
  --public-url https://pin.example.com \
  --timezone America/Los_Angeles \
  --languages English Spanish
```

Replace the example address with your own HTTPS endpoint. The server listens at
`127.0.0.1:8787` by default. Forward your TLS proxy to that address, allow request
bodies up to 129 MiB and response times of at least two minutes, and disable proxy
access logs for the pairing route because its URL contains a one-use secret.
Run just one backend process for each state directory. Use `--host` and `--port`
only if your proxy arrangement requires a different listener.

In another terminal, using the same directory and HTTPS address:

```sh
.venv/bin/openpin-muse --state-dir .muse-state link \
  --public-url https://pin.example.com --qr pairing.png
```

Open `pairing.png` on your computer. On OpenPin, choose **Settings → Link .Center**
(or **Relink .Center**) and scan it with the Pin camera. The backend consumes the
link once and returns the exact `baseUrl`/`deviceId` format OpenPin expects. Links
expire after ten minutes. Creating a new link replaces the pending link; redeeming
it replaces the previously linked Pin's credential.

After Muse connects, hold one finger on the Pin and speak. OpenPin limits the hold
to 20 seconds; this backend rejects decoded recordings over 21 seconds. Photos,
video, voice and images are sent to your Muse account when those gestures are used.
Camera uploads are retained privately on the companion until you delete them.
Voice recordings and the image attached to a spoken question are not retained.

## Gallery, weather, and remote controls

Create a private dashboard sign-in link in another terminal:

```sh
.venv/bin/openpin-muse --state-dir .muse-state dashboard --public-url https://pin.example.com
```

Open the printed link. It expires in ten minutes and can be redeemed once; the
browser session lasts one hour. Dashboard access is separate from the Pin's QR
credential. Keep the link private. Media and location require a signed-in session.
The dashboard labels saved captures as pending, sent, or failed; **Retry Muse**
resubmits pending or failed originals after connection, storage or conversion failures.
Repeated clicks do not enqueue a capture that is already being forwarded. A crash after Muse
accepts a submission but before its acknowledgment is saved can cause a retry to
submit it twice. Original captures stay on this companion; back up its state
folder if you want another copy.

Weather can use a configured place with no geolocation key:

```sh
.venv/bin/openpin-muse --state-dir .muse-state serve \
  --public-url https://pin.example.com --timezone America/Los_Angeles \
  --latitude 37.7749 --longitude -122.4194 --location-name 'San Francisco' \
  --temperature-unit fahrenheit --gallery-max-gib 5
```

These are example coordinates. Configured locations are labeled as such. For
actual Pin positioning, supply `HUMUSE_GOOGLE_MAPS_API_KEY` through your private
service environment with Google's Geolocation API enabled; optionally enable
Geocoding for city names. Google receives nearby Wi-Fi access-point identifiers;
IP-based positioning is disabled. The companion does not retain Wi-Fi scans or
keys. Coordinates are saved privately, and stale location is labeled. No key
means Wi-Fi location returns unavailable; configured-place weather still works.
Weather requests send coordinates to [Open-Meteo](https://open-meteo.com/).
Its free endpoint is for noncommercial use; see its [terms](https://open-meteo.com/en/terms).
Provider failures leave the home clock available.

Remote controls need the Android app from this fork. With JDK 17 and Android SDK
35 installed, build it from the repository's `client` directory:

```sh
./gradlew :apps:primaryapp:assembleDebug
```

Use the resulting `apps/primaryapp/build/outputs/apk/debug/primaryapp-debug.apk`
when following OpenPin's installation procedure. Installing just this backend
keeps gestures working with the original client but does not add remote controls.
Commands expire within 45 seconds, do not survive companion restart, and are
journaled on the Pin to avoid repeating a camera action when an acknowledgment
is lost. A timeout after delivery means the action might have run. Remote videos
use SD quality; gesture captures keep their existing quality. The Pin must be
awake; this backend cannot remotely wake it from hardware sleep.

Large video conversion supports clips up to 30 seconds (Pin gestures record at
most 15 seconds). Media that cannot fit Muse's limit remains in the gallery with
a failed status. Combined spoken replies are bounded to three minutes of audio
and the existing 90-second request deadline; limits produce an error rather than
silently truncating the reply. Very late messages after the quiet period are
ignored and will not play during a later interaction.

## Check and troubleshoot

```sh
.venv/bin/openpin-muse --state-dir .muse-state doctor
curl --fail https://pin.example.com/healthz
```

The health endpoint reports readiness without credentials or chat contents.
`doctor` checks local prerequisites without contacting Muse. A Mac can run the
tests but cannot use the Linux BlueZ pairing command.

If pairing fails after Wi-Fi selection, check `ExchangeMTU` and toggle the phone's
Bluetooth, as described in the upstream SDK. If the Pin plays its failure sound,
check readiness, the TLS certificate, ffmpeg, and companion connectivity. Its clock uses `--timezone` because the current OpenPin client renders a
wall-clock timestamp as UTC.

## Develop and verify

With [uv](https://docs.astral.sh/uv/) and ffmpeg installed:

```sh
uv sync --locked
uv run pytest -q
uv run ruff check src tests
```

Tests cover real HTTP OpenPin framing/pairing, ffmpeg conversion and Muse's
encrypted Noise session against a local protocol peer. They do not prove BLE
pairing, Muse server compatibility, camera/audio routing, TLS trust, or gesture
behavior on a physical Pin. The live acceptance test is: pair the companion,
QR-link an OpenPin-enabled Pin, speak, receive the matching spoken reply, then
repeat with vision, translation, reconnect, weather, gallery playback and all
remote controls. Verify audible camera cues, duplicate-command handling, sleep
behavior and actual capture sizes on the physical Pin.

The Muse SDK dependency is pinned to commit
`7e7123e2815d3e7e3c0f2ca330f576290ae6a6a9`; `uv.lock` pins the remaining dependencies.
The code uses SDK extension points in `LinkSession`, so SDK upgrades require rerunning
the encrypted-peer tests. OpenPin's repository license remains in [../LICENSE](../LICENSE);
the Muse SDK dependency retains its Apache 2.0 license.
