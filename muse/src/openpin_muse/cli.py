"""Local setup and serving commands. Secrets never go in command arguments."""

import argparse
import asyncio
import contextlib
import getpass
import importlib.util
import json
import logging
import os
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfoNotFoundError

from aiohttp import web
from musegadget import config, identity


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Connect an OpenPin-enabled Humane Ai Pin to Muse")
    result.add_argument("--state-dir", type=Path, default=Path.home() / ".local/share/openpin-muse")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("token", help="Store a Muse SDK token from a hidden prompt")
    pairing = commands.add_parser("pair-muse", help="Pair this Linux companion in the Muse phone app")
    pairing.add_argument("--force", action="store_true", help="Replace an existing Muse pairing")
    pairing.add_argument("--timeout", type=int, default=600)
    link = commands.add_parser("link", help="Create a one-use QR link for OpenPin's Link .Center screen")
    link.add_argument("--public-url", required=True)
    link.add_argument("--qr", type=Path, default=Path("pairing.png"))
    dashboard = commands.add_parser("dashboard", help="Create a private one-use dashboard sign-in link")
    dashboard.add_argument("--public-url", required=True)
    serve = commands.add_parser("serve", help="Run the Muse backend (normally behind an HTTPS proxy)")
    serve.add_argument("--public-url", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--timezone", default="UTC", help="IANA time zone for the Pin's clock")
    serve.add_argument("--languages", nargs=2, default=["English", "Spanish"], metavar=("FIRST", "SECOND"))
    serve.add_argument("--latitude", type=float, help="Configured weather latitude when Pin location is unavailable")
    serve.add_argument("--longitude", type=float, help="Configured weather longitude")
    serve.add_argument("--location-name", help="Label for the configured location")
    serve.add_argument("--temperature-unit", choices=["fahrenheit", "celsius"], default="fahrenheit")
    serve.add_argument("--gallery-max-gib", type=float, default=5, help="Local media quota; no automatic deletion")
    commands.add_parser("doctor", help="Check local prerequisites without contacting Muse")
    return result


def doctor(state_dir: Path) -> int:
    from openpin_muse.api import STATE_FILENAME

    try:
        token_ready = bool(config.sdk_token())
    except ValueError:
        token_ready = False
    checks = {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "linux_bluetooth_host": sys.platform.startswith("linux"),
        "dbus_python": importlib.util.find_spec("dbus") is not None,
        "pygobject": importlib.util.find_spec("gi") is not None,
        "sdk_token": token_ready,
        "muse_pairing": bool(config.load_json(config.PAIRING_FILE)),
        "openpin_link": (state_dir / STATE_FILENAME).exists(),
    }
    print(json.dumps(checks, indent=2))
    print("Bluetooth radio availability and live Muse access still require a device test.")
    return 0 if all(value for name, value in checks.items() if name != "openpin_link") else 1


def serve(args) -> None:
    from openpin_muse.api import create_app
    from openpin_muse.bridge import MuseBridge
    from openpin_muse.controls import PinControls
    from openpin_muse.environment import EnvironmentService
    from openpin_muse.gallery import Gallery

    if not shutil.which("ffmpeg"):
        raise ValueError("Install ffmpeg before serving Pin recordings")
    token = config.sdk_token()
    if not token or not config.load_json(config.PAIRING_FILE):
        raise ValueError("Run token and pair-muse before starting the backend")
    if not shutil.which("ffprobe"):
        raise ValueError("Install ffprobe (included with ffmpeg) for camera capture forwarding")
    if not 0.1 <= args.gallery_max_gib <= 1024:
        raise ValueError("Gallery quota must be between 0.1 and 1024 GiB")
    controls = PinControls()
    environment = EnvironmentService(
        args.state_dir, latitude=args.latitude, longitude=args.longitude, location_name=args.location_name,
        temperature_unit=args.temperature_unit, google_api_key=os.environ.get("HUMUSE_GOOGLE_MAPS_API_KEY"),
    )
    gallery = Gallery(args.state_dir, max_bytes=int(args.gallery_max_gib * 1024 ** 3))
    bridge = MuseBridge(identity.load_or_create(), token, tuple(args.languages),
                        controls=controls, environment=environment)
    app = create_app(bridge, args.state_dir, public_url=args.public_url, time_zone=args.timezone,
                     environment=environment, gallery=gallery, controls=controls)

    async def connection_lifetime(app):
        task = asyncio.create_task(bridge.run(), name="muse-connection")
        try:
            yield
        finally:
            bridge.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.cleanup_ctx.append(connection_lifetime)
    # Pair URLs contain a one-use secret. Do not write them to request logs.
    web.run_app(app, host=args.host, port=args.port, access_log=None)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    args.state_dir = args.state_dir.expanduser().resolve()
    muse_state = args.state_dir / "muse"
    os.environ[config.STATE_DIR_ENV] = str(muse_state)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        if args.command == "doctor":
            return doctor(args.state_dir)
        args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        muse_state.mkdir(exist_ok=True, mode=0o700)
        if args.command == "token":
            value = getpass.getpass("Muse SDK token: ").strip()
            # Use the SDK's exact validation before replacing an existing token.
            if not config._SDK_TOKEN.fullmatch(value):
                raise ValueError("Invalid SDK token; copy it from gadgets.muse.ai")
            path = muse_state / config.SDK_TOKEN_FILE
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write(value + "\n")
            print("SDK token stored privately.")
        elif args.command == "pair-muse":
            if not sys.platform.startswith("linux"):
                raise ValueError("Muse Bluetooth pairing requires a Linux companion with BlueZ")
            if not config.sdk_token():
                raise ValueError("Run token first")
            if not 1 <= args.timeout <= 600:
                raise ValueError("Pairing timeout must be between 1 and 600 seconds")
            from musegadget.cli import cmd_pair
            return cmd_pair(args)
        elif args.command == "link":
            import qrcode
            from openpin_muse.api import issue_pairing
            url = issue_pairing(args.state_dir, args.public_url)
            image = qrcode.make(url)
            fd = os.open(args.qr, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as file:
                image.save(file, format="PNG")
            print(f"One-use OpenPin QR saved to {args.qr}. It expires in 10 minutes.")
            print("On the Pin, open Settings → Link .Center and scan it. Keep this QR private.")
        elif args.command == "dashboard":
            from openpin_muse.dashboard import issue_dashboard_link
            print(issue_dashboard_link(args.state_dir, args.public_url))
            print("Private one-use sign-in link; expires in 10 minutes. Do not share it.")
        elif args.command == "serve":
            serve(args)
        return 0
    except (ValueError, OSError, ImportError, ZoneInfoNotFoundError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
