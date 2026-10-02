# Muse for OpenPin

This backend connects an **OpenPin-enabled Humane Ai Pin** to Muse. Keep OpenPin's
existing Android client and shell daemon: point its QR linking screen at this
backend, then use the usual voice and camera gestures.

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
- Photo/video captures up to 8 MiB: submit attachments to Muse. This is not
  OpenPin.Center's media library; larger captures are rejected. OpenPin records
  video at its highest available quality, so some clips may exceed this limit.
- Existing QR linking and home clock, with an explicit IANA time zone.

The initial voice path plays the first completed, correlated assistant message.
Later messages from the same turn are not played. Muse replies must include a
parent message ID; uncorrelated messages are never played on the Pin. Weather,
location lookup, OpenPin.Center account features, remote hardware commands, and
ESP32 OTA are not implemented here. The original client remains available by
relinking it to its previous backend.

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
connection code, registers with no remote commands, and never grants Muse shell
or file access to the companion.

## Start the backend and link the Pin

```sh
.venv/bin/openpin-muse --state-dir .muse-state serve \
  --public-url https://pin.example.com \
  --timezone America/Los_Angeles \
  --languages English Spanish
```

Replace the example address with your own HTTPS endpoint. The server listens at
`127.0.0.1:8787` by default. Forward your TLS proxy to that address, allow request
bodies up to 17 MiB and response times of at least two minutes, and disable proxy
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
The backend does not retain uploaded media on disk.

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
check readiness, the TLS certificate, ffmpeg, and companion connectivity. The
Pin's existing location lookup gets an explicit unsupported response; its home
screen shows no weather/location data. Its clock uses `--timezone` because the
current OpenPin client renders a wall-clock timestamp as UTC.

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
repeat with vision, translation, reconnect and a camera capture.

The Muse SDK dependency is pinned to commit
`7e7123e2815d3e7e3c0f2ca330f576290ae6a6a9`; `uv.lock` pins the remaining dependencies.
The code uses SDK extension points in `LinkSession`, so SDK upgrades require rerunning
the encrypted-peer tests. OpenPin's repository license remains in [../LICENSE](../LICENSE);
the Muse SDK dependency retains its Apache 2.0 license.
