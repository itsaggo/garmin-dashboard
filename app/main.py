"""Web server: login, JSON API, static dashboard, and the background sync loop."""
from __future__ import annotations

import html
import ipaddress
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import auth, config, sync
from .db import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("garmin.web")

settings = config.load()
db = Database(settings.db_path)
STATIC = Path(__file__).parent / "static"
limiter = auth.LoginLimiter()
_stop = threading.Event()
_last_manual_sync = 0.0
MANUAL_SYNC_COOLDOWN_S = 120

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Robots-Tag": "noindex, nofollow",
}

# Paths reachable without signing in.
PUBLIC_PATHS = {"/login", "/healthz", "/static/style.css", "/static/favicon.svg"}


# ------------------------------------------------------------------ scheduler
def _first_run_delay() -> float:
    """Seconds until the first sync: skip it if a recent sync already succeeded,
    so container restarts don't re-hit Garmin every time."""
    from datetime import datetime, timezone

    last = db.last_sync()
    if not last or last.get("status") not in ("ok", "partial") or not last.get("finished_at"):
        return 0
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last["finished_at"])).total_seconds()
    except ValueError:
        return 0
    return max(0.0, settings.sync_interval_hours * 3600 - age)


def _scheduler() -> None:
    try:
        db.mark_interrupted_syncs()
        next_run = time.monotonic() + _first_run_delay()
    except Exception:
        log.exception("Scheduler setup failed; syncing now")
        next_run = time.monotonic()
    while not _stop.is_set():
        try:
            requested = settings.sync_request_file.exists()
            if requested:
                settings.sync_request_file.unlink(missing_ok=True)
            if requested or time.monotonic() >= next_run:
                next_run = time.monotonic() + settings.sync_interval_hours * 3600
                if settings.api_key() or settings.connect_configured():
                    result = sync.run_sync(settings, db)
                    log.info("Sync finished: %s", result["status"])
                else:
                    log.info("Waiting for setup: docker exec -it garmin-dashboard python -m app.setup")
        except Exception:  # never let the scheduler thread die
            log.exception("Scheduled sync crashed; will retry at the next interval")
        _stop.wait(30)  # also notices sync requests from `app.setup` within 30 s


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_scheduler, name="sync-scheduler", daemon=True).start()
    yield
    _stop.set()


app = FastAPI(title="Garmin Dashboard", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ------------------------------------------------------------------ middleware
def _host_allowed(host_header: str) -> bool:
    """Accept requests addressed to an IP address, localhost, or a name listed in
    ALLOWED_HOSTS. Blocks DNS-rebinding attacks, where a malicious site points
    its own domain name at your server."""
    host = host_header.strip().lower()
    if host.startswith("["):  # [IPv6]:port
        host = host[1:host.find("]")] if "]" in host else host
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    host = host.rstrip(".")
    if not host:
        return False
    if host == "localhost" or host in settings.allowed_hosts:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _same_origin(request: Request) -> bool:
    """Reject cross-site form/fetch submissions (on top of SameSite=Strict cookies)."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin or origin == "null":
        return False
    parts = urlsplit(origin)
    host = request.headers.get("host", "")
    if host and parts.netloc.lower() == host.lower():
        return True
    # Behind a reverse proxy: trust only exact origins (scheme, name and port) listed in ALLOWED_ORIGINS.
    full = f"{parts.scheme}://{parts.netloc}".lower()
    return bool(parts.scheme and parts.netloc) and full in settings.allowed_origins


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    host = request.headers.get("host", "")
    if path != "/healthz" and not _host_allowed(host):
        name = host.rsplit(":", 1)[0][:100]
        return Response(
            f"This address ({name}) isn't allowed. Open the dashboard by IP address, or add "
            f"{name} to ALLOWED_HOSTS in config.env and run install.sh again.",
            status_code=400, media_type="text/plain", headers=SECURITY_HEADERS,
        )
    if request.method not in ("GET", "HEAD", "POST"):
        response = Response(status_code=405)
    elif request.method == "POST" and not _same_origin(request):
        response = Response("Cross-site request blocked", status_code=403)
    elif path in PUBLIC_PATHS:
        response = await call_next(request)
    elif not auth.password_configured(settings):
        response = _setup_required(request)
    elif not auth.session_ok(db, request.cookies.get(auth.SESSION_COOKIE)):
        if path.startswith("/api/"):
            response = JSONResponse({"detail": "Not signed in"}, status_code=401)
        else:
            response = RedirectResponse("/login", status_code=303)
    else:
        response = await call_next(request)

    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"  # revalidate, so updates show up right away
    else:
        response.headers["Cache-Control"] = "no-store"
    if settings.cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


def _setup_required(request: Request) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Setup required"}, status_code=503)
    return HTMLResponse(_page(
        "Finish setup",
        "<p>Set a dashboard password and add your Garmin key from the Unraid terminal:</p>"
        "<pre>docker exec -it garmin-dashboard python -m app.setup</pre>"
        "<p>Then reload this page.</p>",
    ), status_code=503)


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><link rel=stylesheet href=/static/style.css>"
        "<link rel=icon href=/static/favicon.svg></head>"
        f"<body><main class=narrow><h1>{html.escape(title)}</h1>{body}</main></body></html>"
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------------ login
_LOGIN_MESSAGES = {
    "bad": "That password didn't match.",
    "locked": "Too many attempts. Wait 15 minutes and try again.",
    "out": "You're signed out.",
    "busy": "The server is busy checking other sign-ins. Try again in a moment.",
}


@app.get("/login")
def login_page(m: str = ""):
    if not auth.password_configured(settings):
        return RedirectResponse("/", status_code=303)
    note = _LOGIN_MESSAGES.get(m)  # fixed messages only; nothing from the URL is echoed
    return HTMLResponse(_page(
        "Training Dashboard",
        (f"<p class=notice>{note}</p>" if note else "")
        + "<form method=post action=/login class=login>"
        "<label for=pw>Password</label>"
        "<input id=pw name=password type=password autocomplete=current-password required autofocus>"
        "<button class=btn type=submit>Sign in</button></form>",
    ))


@app.post("/login")
def login_submit(request: Request, password: str = Form(..., max_length=1024)):
    ip = _client_ip(request)
    if not auth.password_configured(settings):
        return RedirectResponse("/", status_code=303)
    stamp = limiter.try_begin(ip)  # counted before hashing, so bursts can't bypass the limit
    if stamp is None:
        return RedirectResponse("/login?m=locked", status_code=303)
    ok = auth.check_password_limited(settings, password)
    if ok is None:
        limiter.release(stamp)  # the password wasn't checked, so don't charge an attempt
        return RedirectResponse("/login?m=busy", status_code=303)
    if not ok:
        log.warning("Failed dashboard login from %s", ip)
        return RedirectResponse("/login?m=bad", status_code=303)
    limiter.success(ip, stamp)
    token = auth.new_session(db)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
        httponly=True, samesite="strict", secure=settings.cookie_secure, path="/",
    )
    return resp


@app.post("/logout")
def logout(request: Request):
    auth.end_session(db, request.cookies.get(auth.SESSION_COOKIE))
    resp = RedirectResponse("/login?m=out", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/", samesite="strict", httponly=True, secure=settings.cookie_secure)
    return resp


# ------------------------------------------------------------------ API
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/status")
def status():
    return {
        "api_key_configured": bool(settings.api_key()),
        "connect_configured": settings.connect_configured(),
        "units": settings.units,
        "sync_interval_hours": settings.sync_interval_hours,
        "syncing": sync.is_running(settings),
        "last_sync": db.last_sync(),
        "days_stored": db.count("daily"),
        "training_days_stored": db.count("training"),
        "activities_stored": db.count("activities"),
        "race": {"name": settings.race_name, "date": settings.race_date} if settings.race_date else None,
    }


def _since(days: int) -> str:
    return (date.today() - timedelta(days=days - 1)).isoformat()


@app.get("/api/daily")
def daily(days: int = Query(90, ge=7, le=3650)):
    return db.rows_since("daily", _since(days))


@app.get("/api/training")
def training(days: int = Query(90, ge=7, le=3650)):
    return db.rows_since("training", _since(days))


@app.get("/api/activities")
def activities(limit: int = Query(50, ge=1, le=500)):
    return db.activities(limit)


@app.get("/api/weekly")
def weekly(weeks: int = Query(16, ge=1, le=260), type: str = Query("run", pattern="^[a-z_]{1,30}$")):
    since = (date.today() - timedelta(weeks=weeks)).isoformat()
    return db.weekly(since, f"%{type}%")


@app.post("/api/sync")
def trigger_sync(request: Request):
    global _last_manual_sync
    if request.headers.get("x-requested-with") != "dashboard":
        raise HTTPException(403, "Missing request header")
    if not (settings.api_key() or settings.connect_configured()):
        raise HTTPException(400, "Nothing configured to sync yet")
    if sync.is_running(settings):
        return {"started": False, "reason": "A sync is already running"}
    wait = MANUAL_SYNC_COOLDOWN_S - (time.monotonic() - _last_manual_sync)
    if wait > 0:
        return {"started": False, "reason": f"Please wait {int(wait)} s between manual syncs"}
    _last_manual_sync = time.monotonic()
    threading.Thread(target=sync.run_sync, args=(settings, db), daemon=True).start()
    return {"started": True}


@app.get("/api/raw")
def raw_index():
    return db.raw_summary()


@app.get("/api/raw/{endpoint}")
def raw_latest(endpoint: str):
    data = db.raw_latest(endpoint)
    if data is None:
        raise HTTPException(404, "Nothing stored for that endpoint yet")
    return JSONResponse(data)


# ------------------------------------------------------------------ pages
@app.get("/raw")
def raw_page():
    return FileResponse(STATIC / "raw.html")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
