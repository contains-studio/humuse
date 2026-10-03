"""Private companion dashboard; administrator sessions are separate from Pin pairing."""

import asyncio
from contextlib import contextmanager, suppress
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time

from aiohttp import web

from .gallery import Gallery
from .controls import COMMANDS, validate_command
from .protocol import ProtocolError, decode_json


COOKIE = "humuse_admin"
SESSION_SECONDS = 3600
MAX_AUTH_BYTES = 32 * 1024


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class _AuthStore:
    def __init__(self, state_dir):
        self.directory = Path(state_dir) / "dashboard"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ValueError("Dashboard state must be a real directory")
        self.directory.chmod(0o700)
        self.path = self.directory / "sessions.json"

    @contextmanager
    def locked(self):
        fd = os.open(self.directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"link": None, "sessions": {}}
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("Dashboard state must be a private regular file")
            raw = stream.read(MAX_AUTH_BYTES + 1)
        if len(raw) > MAX_AUTH_BYTES:
            raise ValueError("Dashboard state is too large")
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("sessions"), dict):
            raise ValueError("Invalid dashboard state")
        return value

    def write(self, value):
        fd, temporary = tempfile.mkstemp(prefix=".sessions-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    def redeem(self, code, origin):
        if not isinstance(code, str) or not 32 <= len(code) <= 128:
            return None
        with self.locked():
            value = self.read()
            link = value.get("link")
            if (not link or link["expires"] <= time.time() or link["origin"] != origin
                    or not secrets.compare_digest(link["hash"], _digest(code))):
                return None
            token = secrets.token_urlsafe(32)
            sessions = {key: entry for key, entry in value["sessions"].items()
                        if entry["expires"] > time.time()}
            # Bound persistent state even when links are issued repeatedly.
            while len(sessions) >= 16:
                del sessions[min(sessions, key=lambda key: sessions[key]["expires"])]
            expires = time.time() + SESSION_SECONDS
            sessions[_digest(token)] = {"expires": expires, "origin": origin}
            value.update(link=None, sessions=sessions)
            self.write(value)
            return token, expires

    def session(self, token, origin):
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            return None
        with self.locked():
            session = self.read()["sessions"].get(_digest(token))
        if not session or session["expires"] <= time.time() or session["origin"] != origin:
            return None
        return {"csrf": _digest("csrf:" + token), "expires_at": session["expires"]}

    def revoke(self, token):
        with self.locked():
            value = self.read()
            value["sessions"].pop(_digest(token), None)
            self.write(value)


def issue_dashboard_link(state_dir: Path, public_url: str, ttl_seconds: int = 600) -> str:
    """Create a one-use administrator link with its secret in the URL fragment."""
    from .api import validate_public_url

    origin = validate_public_url(public_url)
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
        raise ValueError("Dashboard link lifetime must be between 1 and 3600 seconds")
    store = _AuthStore(state_dir)
    code = secrets.token_urlsafe(32)
    with store.locked():
        value = store.read()
        value["link"] = {"hash": _digest(code), "expires": time.time() + ttl_seconds, "origin": origin}
        store.write(value)
    return f"{origin}/dashboard#{code}"


def install_dashboard(app: web.Application, gallery: Gallery, state_dir: Path, *, public_url: str,
                      controls=None, environment=None, forward_capture=None) -> None:
    from .api import validate_public_url

    origin = validate_public_url(public_url)
    store = _AuthStore(state_dir)
    with store.locked():
        store.read()

    async def security_headers(request, response):
        if request.path == "/dashboard" or request.path.startswith("/api/dashboard/"):
            response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                     "Referrer-Policy": "no-referrer", "Cross-Origin-Resource-Policy": "same-origin",
                                     "X-Frame-Options": "DENY"})

    app.on_response_prepare.append(security_headers)

    async def authenticate(request, *, mutation=False):
        token = request.cookies.get(COOKIE, "")
        session = await asyncio.to_thread(store.session, token, origin)
        if session is None:
            raise web.HTTPUnauthorized(text="Dashboard session expired. Open a new dashboard link.")
        if mutation and (request.headers.get("Origin") != origin or not secrets.compare_digest(
                request.headers.get("X-CSRF-Token", "").encode(), session["csrf"].encode())):
            raise web.HTTPForbidden(text="Invalid request origin or CSRF token")
        return session

    async def payload(request):
        if request.content_type != "application/json":
            raise web.HTTPBadRequest(text="JSON content type required")
        raw = bytearray()
        while chunk := await request.content.read(4097 - len(raw)):
            raw.extend(chunk)
            if len(raw) > 4096:
                raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=len(raw))
        try:
            value = decode_json(bytes(raw))
        except (ProtocolError, ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="Invalid JSON") from None
        if not isinstance(value, dict):
            raise web.HTTPBadRequest(text="JSON object required")
        return value

    async def page(request):
        nonce = secrets.token_urlsafe(24)
        response = web.Response(text=HTML.replace("__NONCE__", nonce), content_type="text/html")
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self'; media-src 'self'; connect-src 'self'; "
            f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        return response

    async def redeem(request):
        if request.headers.get("Origin") != origin:
            raise web.HTTPForbidden(text="Invalid request origin")
        result = await asyncio.to_thread(store.redeem, (await payload(request)).get("code"), origin)
        if result is None:
            raise web.HTTPUnauthorized(text="This dashboard link has expired or was already used.")
        token, expires = result
        response = web.json_response({"csrf": _digest("csrf:" + token), "expires_at": expires})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=origin.startswith("https:"),
                            samesite="Strict", path="/api/dashboard")
        return response

    async def session(request):
        return web.json_response(await authenticate(request))

    async def logout(request):
        await authenticate(request, mutation=True)
        await asyncio.to_thread(store.revoke, request.cookies[COOKIE])
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE, path="/api/dashboard")
        return response

    async def state(request):
        await authenticate(request)
        async def context(fetch):
            if fetch is None:
                return {"available": False}
            try:
                return await fetch()
            except Exception:
                return {"available": False, "error": "Temporarily unavailable"}
        control_state, environmental = await asyncio.gather(context(controls.snapshot if controls is not None else None),
                                                            context(environment.context if environment is not None else None))
        return web.json_response({"controls": control_state, "environment": environmental})

    async def media(request):
        await authenticate(request)
        items = await gallery.list()
        return web.json_response({"items": items, "used_bytes": sum(item["size"] for item in items),
                                  "max_bytes": gallery.max_bytes})

    async def original(request):
        await authenticate(request)
        item = await gallery.get(request.match_info["id"])
        if item is None:
            raise web.HTTPNotFound(text="Capture not found")
        metadata, path = item
        headers = {"Content-Type": metadata["mime_type"]}
        if request.path.endswith("/download"):
            headers["Content-Disposition"] = f'attachment; filename="{metadata["filename"]}"'
        return web.FileResponse(path, headers=headers)

    async def delete(request):
        await authenticate(request, mutation=True)
        if not await gallery.delete(request.match_info["id"]):
            raise web.HTTPNotFound(text="Capture not found")
        return web.json_response({"ok": True})

    async def command(request):
        await authenticate(request, mutation=True)
        value = await payload(request)
        name, params = value.get("command"), value.get("params", {})
        if not isinstance(name, str) or name not in COMMANDS or not isinstance(params, dict):
            raise web.HTTPBadRequest(text="Unsupported Pin command")
        try:
            validate_command(name, params)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid command parameters") from None
        if controls is None:
            raise web.HTTPServiceUnavailable(text="Pin controls are unavailable")
        try:
            return web.json_response(await controls.submit(name, params, timeout_ms=45000 if name in ("capture_photo", "record_video") else 15000))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid command parameters") from None
        except ConnectionError:
            raise web.HTTPServiceUnavailable(text="Pin is offline") from None
        except asyncio.TimeoutError:
            raise web.HTTPGatewayTimeout(text="Pin did not complete the command in time") from None

    async def retry(request):
        await authenticate(request, mutation=True)
        capture_id = request.match_info["id"]
        if await gallery.get(capture_id) is None:
            raise web.HTTPNotFound(text="Capture not found")
        if forward_capture is None:
            raise web.HTTPServiceUnavailable(text="Muse forwarding is unavailable")
        try:
            result = await forward_capture(capture_id)
        except ConnectionError:
            raise web.HTTPServiceUnavailable(text="Muse is offline. Your original is still saved.") from None
        return web.json_response(result if isinstance(result, dict) else {"ok": True}, status=202)

    app.router.add_get("/dashboard", page)
    app.router.add_post("/api/dashboard/redeem", redeem)
    app.router.add_get("/api/dashboard/session", session)
    app.router.add_post("/api/dashboard/logout", logout)
    app.router.add_get("/api/dashboard/state", state)
    app.router.add_get("/api/dashboard/media", media)
    app.router.add_get("/api/dashboard/media/{id}", original)
    app.router.add_get("/api/dashboard/media/{id}/download", original)
    app.router.add_delete("/api/dashboard/media/{id}", delete)
    app.router.add_post("/api/dashboard/media/{id}/retry", retry)
    app.router.add_post("/api/dashboard/commands", command)


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Humuse · Your Pin companion</title><style nonce="__NONCE__">
:root{color-scheme:light;--ink:#1a3028;--muted:#627268;--paper:#f4f4ec;--line:#dce1d6;--green:#275c47;--lime:#dfef96}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
button,input,a{font:inherit}button,a{touch-action:manipulation}button{border:1px solid var(--line);border-radius:9px;padding:10px 15px;background:white;color:var(--ink);cursor:pointer}button:hover{border-color:var(--green)}button:disabled{opacity:.5;cursor:wait}button:focus-visible,a:focus-visible,input:focus-visible{outline:3px solid #8aad51;outline-offset:3px}a{color:var(--green)}[hidden]{display:none!important}
.shell{max-width:1180px;margin:auto;padding:28px 40px 48px}.top{display:flex;align-items:center;justify-content:space-between;gap:16px;padding-bottom:26px;border-bottom:1px solid var(--line)}.brand{display:flex;gap:10px;align-items:center;font-size:24px;letter-spacing:-1px;font-weight:650}.mark{display:grid;place-items:center;background:var(--green);color:var(--lime);height:33px;width:33px;border-radius:10px;font-size:21px}.top small{display:block;color:var(--muted);font-size:12px;letter-spacing:.02em;font-weight:400}.top-actions{display:flex;align-items:center;gap:14px}.tag{font-size:12px;padding:4px 9px;background:#e9eddf;border-radius:30px;white-space:nowrap}.dot{display:inline-block;width:7px;height:7px;background:#869180;border-radius:50%;margin-right:6px}.online .dot{background:#4f922f}
.intro{display:flex;justify-content:space-between;align-items:end;gap:24px;padding:39px 0 25px}.eyebrow{font-size:11px;letter-spacing:.13em;font-weight:650;text-transform:uppercase;color:var(--muted);margin:0 0 8px}h1{font-size:clamp(28px,4vw,40px);letter-spacing:-1.2px;line-height:1.15;font-weight:500;margin:0 0 10px}p{margin:0}h2{font-size:19px;line-height:1.3;letter-spacing:-.4px;font-weight:550;margin:0}.muted{color:var(--muted)}.summary{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}.card{border:1px solid var(--line);background:#fafbf5;border-radius:15px;padding:22px;min-width:0}.card .value{font-size:24px;line-height:1.25;letter-spacing:-.6px;margin:14px 0 5px}.card .detail{font-size:13px;color:var(--muted);overflow-wrap:anywhere}.control-section{display:grid;grid-template-columns:1fr 1.6fr;gap:22px;margin:20px 0 34px}.controls{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-top:16px}.primary{background:var(--green);color:white;border-color:var(--green)}.volume{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:17px}.volume input{max-width:145px;accent-color:var(--green)}.volume label{font-size:13px}.command-result{font-size:13px;margin-top:14px;white-space:pre-wrap;overflow-wrap:anywhere}.notice{padding:14px 18px;border-radius:10px;background:#f7edd9;border:1px solid #e5d6b7;margin:20px 0;color:#654b21}.section-head{display:flex;justify-content:space-between;align-items:center;gap:14px;margin-bottom:17px}.section-head p{font-size:13px;color:var(--muted);margin-top:4px}.filters{display:flex;gap:6px}.filters button{padding:6px 12px;background:transparent;border-color:transparent;font-size:13px}.filters button[aria-pressed=true]{background:var(--green);color:white}.gallery{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}.capture{overflow:hidden;background:#fafbf5;border:1px solid var(--line);border-radius:12px}.capture img,.capture video{width:100%;aspect-ratio:4/3;object-fit:cover;background:#e0e5d8;display:block}.capture-body{padding:14px}.capture-title{display:flex;gap:8px;justify-content:space-between;align-items:center;font-size:14px}.capture-meta{color:var(--muted);font-size:12px;margin:4px 0 12px}.capture-actions{display:flex;justify-content:space-between;align-items:center}.capture-actions a{font-size:13px;text-decoration:none}.danger{background:transparent;border:0;font-size:13px;padding:5px;color:#8d4d3c}.status{font-size:10px;letter-spacing:.03em;text-transform:uppercase;background:#e8efdf;color:#527443;border-radius:5px;padding:3px 6px;white-space:nowrap}.status.failed{background:#f5e5da;color:#875131}.status.pending{background:#eeeade;color:#716241}.empty{border:1px dashed #c4cebd;border-radius:14px;padding:48px 25px;text-align:center;grid-column:1/-1}.empty-icon{height:52px;width:52px;border:1px solid #c4cebd;border-radius:15px;margin:0 auto 16px;display:grid;place-items:center;font-size:25px}.empty p{margin:8px auto 0;color:var(--muted);max-width:430px}.empty h2{font-size:18px}.auth{max-width:550px;margin:70px auto;text-align:center}.auth .card{padding:36px}.auth h1{font-size:31px}.auth p{margin:15px 0}.auth code{background:#e9eddf;padding:3px 7px;border-radius:4px;font-size:13px}.footer{font-size:12px;color:var(--muted);border-top:1px solid var(--line);padding-top:20px;margin-top:35px;display:flex;justify-content:space-between;gap:18px}.subtle{font-size:12px;color:var(--muted)}.loading{padding:60px;text-align:center;color:var(--muted)}
@media(max-width:760px){.shell{padding:20px}.top{padding-bottom:20px}.top-actions{gap:8px}.top-actions button{font-size:12px;padding:7px 10px}.brand{font-size:22px}.intro{padding-top:28px}.summary{grid-template-columns:1fr}.card{padding:18px}.summary .card{display:grid;grid-template-columns:1fr auto;gap:4px 15px}.summary .card .eyebrow{grid-column:1}.summary .card .value{grid-column:2;grid-row:1/3;margin:0;align-self:center;text-align:right;font-size:21px}.summary .card .detail{grid-column:1}.control-section{grid-template-columns:1fr;gap:13px;margin-top:15px}.gallery{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.section-head{align-items:start;flex-direction:column}.capture-body{padding:11px}.capture-title{flex-wrap:wrap;font-size:13px}.footer{flex-direction:column;gap:3px}.tag{font-size:10px}.intro>button{display:none}}
@media(max-width:420px){.gallery{grid-template-columns:1fr}.top small{font-size:10px}.tag{display:none}.intro h1{font-size:31px}}
</style></head><body><div class="shell">
<header class="top"><div class="brand"><span class="mark" aria-hidden="true">h</span><div>humuse<small>A little more presence.</small></div></div><div class="top-actions"><span class="tag" id="connection"><span class="dot"></span>Companion dashboard</span><button id="logout" hidden>Sign out</button></div></header>
<div id="loading" class="loading" role="status">Opening your companion…</div>
<section id="auth" class="auth" hidden><div class="card"><p class="eyebrow">Your private companion</p><h1>A small window<br>into your world.</h1><p id="auth-message" class="muted">Open a fresh dashboard link from your companion to view your captures and connect with your Pin.</p><p class="subtle">On your companion, run <code>openpin-muse dashboard</code><br>with the same public URL as your server.</p></div></section>
<main id="main" hidden><div class="intro"><div><p class="eyebrow">Your Pin, a little closer</p><h1>Life, captured.</h1><p class="muted">Your moments and a few useful things, all in one place.</p></div><button id="refresh">Refresh</button></div>
<div id="notice" class="notice" role="alert" hidden></div>
<section class="summary" aria-label="Companion overview"><article class="card"><p class="eyebrow">Pin connection</p><p class="value" id="pin-value">Checking…</p><p class="detail" id="pin-detail">Waiting for your Pin</p></article><article class="card"><p class="eyebrow">Location</p><p class="value" id="location-value">Not set</p><p class="detail" id="location-detail">A location will appear when available</p></article><article class="card"><p class="eyebrow">Weather</p><p class="value" id="weather-value">Not available</p><p class="detail" id="weather-detail">Waiting for a location</p></article></section>
<section class="control-section"><article class="card"><h2>A nudge to your Pin</h2><p class="subtle">Each action reports back when your Pin responds.</p><div class="controls"><button data-command="get_status">Check status</button><button data-command="ring">Ring Pin</button></div><div class="volume"><label for="volume">Volume</label><input id="volume" type="range" min="0" max="100" value="50"><output id="volume-value" for="volume">50%</output><button data-command="set_volume">Set</button></div></article><article class="card"><h2>Save a moment</h2><p class="subtle">Use your Pin’s camera. Captures stay private on this companion.</p><div class="controls"><button class="primary" data-command="capture_photo">Take a photo</button><button data-command="record_video">Record a video</button></div><p id="command-result" class="command-result" role="status">Choose an action above. Your Pin must be awake to respond.</p></article></section>
<section aria-labelledby="gallery-heading"><div class="section-head"><div><h2 id="gallery-heading">Your captures</h2><p id="storage">Loading your gallery…</p></div><div class="filters" aria-label="Filter captures"><button data-filter="all" aria-pressed="true">All</button><button data-filter="image/jpeg" aria-pressed="false">Photos</button><button data-filter="video/mp4" aria-pressed="false">Videos</button></div></div><div id="gallery" class="gallery" aria-live="polite"></div></section></main>
<footer class="footer"><span>Humuse · OpenPin + Muse</span><span>Stored on your companion. Shared with Muse when connected. Weather by <a href="https://open-meteo.com/" rel="noreferrer" target="_blank">Open-Meteo</a>.</span></footer></div>
<script nonce="__NONCE__">
'use strict';
const $=id=>document.getElementById(id);let csrf='',items=[],filter='all',busy=false,refreshing=false;
const code=location.hash.slice(1);history.replaceState(null,'',location.pathname);
const text=(id,value)=>{$(id).textContent=value};
function locked(message){items=[];$('gallery').replaceChildren();$('main').hidden=true;$('auth').hidden=false;$('loading').hidden=true;$('logout').hidden=true;if(message)text('auth-message',message);csrf=''}
async function api(path,options={}){const headers={...(options.body?{'Content-Type':'application/json'}:{}),...options.headers};if(options.method&&options.method!=='GET')headers['X-CSRF-Token']=csrf;const response=await fetch('/api/dashboard'+path,{...options,headers,credentials:'same-origin'});if(!response.ok){let message=await response.text();try{message=JSON.parse(message).error||message}catch{}if(response.status===401)locked('Your session has expired. Open a fresh dashboard link from your companion.');throw new Error(message||'The request could not be completed.')}return response.json()}
function notice(message){text('notice',message);$('notice').hidden=!message}
function size(bytes){if(bytes<1024)return bytes+' B';const unit=bytes>=1024**3?'GB':bytes>=1024**2?'MB':'KB';const divisor={GB:1024**3,MB:1024**2,KB:1024}[unit];return (bytes/divisor).toFixed(1)+' '+unit}
function el(tag,className,value){const node=document.createElement(tag);if(className)node.className=className;if(value!==undefined)node.textContent=value;return node}
function renderGallery(){const grid=$('gallery');grid.replaceChildren();const visible=items.filter(item=>filter==='all'||item.mime_type===filter);if(!visible.length){const empty=el('div','empty');empty.append(el('div','empty-icon','◎'),el('h2','',items.length?'Nothing in this view yet':'Make room for a little wonder.'),el('p','',items.length?'Try another filter to see your captures.':'Your photos and videos will appear here after you capture something with your Pin.'));grid.append(empty);return}for(const item of visible){const card=el('article','capture');const url='/api/dashboard/media/'+encodeURIComponent(item.id);const video=item.mime_type==='video/mp4';const media=el(video?'video':'img');media.src=url;if(video){media.controls=true;media.preload='metadata';media.playsInline=true;media.setAttribute('aria-label','Pin video captured '+item.created_at)}else{media.alt='Pin photo captured '+new Date(item.created_at).toLocaleString();media.loading='lazy'}const body=el('div','capture-body'),title=el('div','capture-title');title.append(el('span','',video?'Video':'Photo'),el('span','status '+item.muse_status,{sent:'Sent to Muse',failed:'Muse unavailable',pending:'Pending Muse'}[item.muse_status]||'Saved'));const meta=el('p','capture-meta',new Date(item.created_at).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})+' · '+size(item.size));const actions=el('div','capture-actions');const download=el('a','','Download original');download.href=url+'/download';const remove=el('button','danger','Delete');remove.onclick=async()=>{if(!confirm('Delete this capture from your companion? This cannot be undone. Copies already sent to Muse are not deleted.'))return;remove.disabled=true;try{await api('/media/'+encodeURIComponent(item.id),{method:'DELETE'});await refresh()}catch(error){notice(error.message);remove.disabled=false}};actions.append(download);if(item.muse_status!=='sent'){const retry=el('button','danger','Retry Muse');retry.onclick=async()=>{retry.disabled=true;try{await api('/media/'+encodeURIComponent(item.id)+'/retry',{method:'POST'});text('command-result','Capture queued for Muse.');await refresh()}catch(error){notice(error.message);retry.disabled=false}};actions.append(retry)}actions.append(remove);body.append(title,meta,actions);card.append(media,body);grid.append(card)}}
function renderState(state){const pin=state.controls||{},env=state.environment||{},location=env.location||{},weather=env.weather||{};const online=pin.online===true||pin.connected===true;text('pin-value',online?'Connected':'Not connected');const battery=pin.status?.battery;text('pin-detail',pin.error||(online?(typeof battery==='number'?Math.round(battery*100)+'% battery'+(pin.status?.isCharging?' · Charging':''):'Your Pin is ready'):'Waiting for your Pin to check in'));$('connection').classList.toggle('online',online);$('connection').replaceChildren(el('span','dot'),document.createTextNode(online?'Pin connected':'Pin offline'));text('location-value',location.display_name||location.label||location.name||location.city||(typeof location.latitude==='number'?location.latitude.toFixed(3)+'°, '+Number(location.longitude).toFixed(3)+'°':'Not set'));text('location-detail',location.error||(location.stale?'Last known location':location.source==='configured'?'Configured companion location':location.source==='pin'?'Reported by your Pin':location.source)||'Set a location on your companion to get local weather.');const temperature=weather.temperature_c??weather.temperature;const unit=weather.unit==='fahrenheit'?'°F':'°C';text('weather-value',typeof temperature==='number'?Math.round(temperature)+unit:'Not available');text('weather-detail',weather.description||weather.conditions||weather.condition||weather.error||'Waiting for a location');if(pin.available===false&&pin.error)notice(pin.error)}
async function refresh(){if(refreshing||!csrf)return;refreshing=true;$('refresh').disabled=true;try{const [media,state]=await Promise.all([api('/media'),api('/state')]);items=media.items;text('storage',items.length+' '+(items.length===1?'capture':'captures')+' · '+size(media.used_bytes)+' of '+size(media.max_bytes));renderGallery();renderState(state);notice('')}catch(error){notice(error.message)}finally{refreshing=false;$('refresh').disabled=false}}
function commandMessage(command,result){if(result.ok===false||result.error)return result.error||'Your Pin could not complete the action.';if(result.ok!==true)return 'Your Pin returned an unexpected response. Please check its connection.';if(command==='get_status'){const battery=typeof result.battery==='number'?Math.round(result.battery*100)+'% battery':'Battery unavailable';return battery+(result.isCharging?' · Charging':'')+(result.activity?' · '+result.activity:'')}if(command==='set_volume')return typeof result.volume==='number'?'Volume set to '+Math.round(result.volume*100)+'%.':'Your Pin confirmed the volume change.';if(command==='ring')return 'Your Pin played the ring sound.';return command==='capture_photo'?'Photo saved to your gallery.':'Video saved to your gallery.'}
document.querySelectorAll('[data-filter]').forEach(button=>button.onclick=()=>{filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(other=>other.setAttribute('aria-pressed',String(other===button)));renderGallery()});
document.querySelectorAll('[data-command]').forEach(button=>button.onclick=async()=>{if(busy)return;const command=button.dataset.command;if((command==='capture_photo'||command==='record_video')&&!confirm(command==='record_video'?'Record a video using your Pin camera now?':'Take a photo using your Pin camera now?'))return;busy=true;document.querySelectorAll('[data-command]').forEach(b=>b.disabled=true);text('command-result','Waiting for your Pin…');try{const params=command==='set_volume'?{volume:Number($('volume').value)/100}:{};const result=await api('/commands',{method:'POST',body:JSON.stringify({command,params})});text('command-result',commandMessage(command,result));await refresh()}catch(error){text('command-result',error.message)}finally{busy=false;document.querySelectorAll('[data-command]').forEach(b=>b.disabled=false)}});
$('volume').oninput=()=>text('volume-value',$('volume').value+'%');$('refresh').onclick=refresh;$('logout').onclick=async()=>{try{await api('/logout',{method:'POST'});locked('You’re signed out. Open a fresh dashboard link to return.')}catch(error){notice(error.message)}};
(async()=>{try{const session=code?await api('/redeem',{method:'POST',body:JSON.stringify({code})}):await api('/session');csrf=session.csrf;$('main').hidden=false;$('auth').hidden=true;$('loading').hidden=true;$('logout').hidden=false;await refresh()}catch(error){locked(error.message)}})();
</script></body></html>'''
