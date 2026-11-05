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

import json

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ai, auth, config, fitness, garmin_login, notify, prefs, sync
from .db import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("garmin.web")

settings = config.load()
db = Database(settings.db_path)
prefs.apply_timezone(prefs.get_all(db)["timezone"])
garmin_login.cleanup_stale_staging(settings)
STATIC = Path(__file__).parent / "static"
limiter = auth.LoginLimiter()
garmin_limiter = auth.LoginLimiter(per_ip=5, total=8)  # Garmin sign-ins / key checks (Garmin locks out abusers)
ai_limiter = auth.LoginLimiter(per_ip=10, total=20)  # AI key checks
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
PUBLIC_PATHS = {"/login", "/healthz", "/static/style.css", "/static/favicon.svg", "/static/theme.js"}
# Before any account exists, only the create-account page (plus the above) is reachable.
FIRST_RUN_PATHS = {"/welcome"}


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
                    log.info("Waiting for setup: open the dashboard in a browser to finish setup")
        except Exception:  # never let the scheduler thread die
            log.exception("Scheduled sync crashed; will retry at the next interval")
        try:
            ai.maybe_daily(settings, db, lambda: sync.run_sync(settings, db))
        except ai.AIError as e:
            log.warning("Daily AI insight not written: %s", e)
        except Exception:
            log.exception("Daily AI insight crashed")
        try:
            notify.maybe_send(settings, db)
        except notify.NotifyError as e:
            log.warning("Discord notification not sent: %s", e)
        except Exception:
            log.exception("Discord notification crashed")
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
            f"{name} to the container's 'Allowed host names' setting.",
            status_code=400, media_type="text/plain", headers=SECURITY_HEADERS,
        )
    has_account = auth.account_exists(settings)
    if request.method not in ("GET", "HEAD", "POST"):
        response = Response(status_code=405)
    elif request.method == "POST" and not _same_origin(request):
        response = Response("Cross-site request blocked", status_code=403)
    elif request.method == "POST" and path.startswith("/api/") and request.headers.get("x-requested-with") != "dashboard":
        response = JSONResponse({"detail": "Missing request header"}, status_code=403)
    elif path in PUBLIC_PATHS:
        response = await call_next(request)
    elif not has_account:
        if path in FIRST_RUN_PATHS:
            response = await call_next(request)
        elif path.startswith("/api/"):
            response = JSONResponse({"detail": "Create your account first"}, status_code=503)
        else:
            response = RedirectResponse("/welcome", status_code=303)
    elif path in FIRST_RUN_PATHS:
        response = RedirectResponse("/login", status_code=303)
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


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><script src=/static/theme.js></script>"
        "<link rel=stylesheet href=/static/style.css>"
        "<link rel=icon href=/static/favicon.svg></head>"
        f"<body><main class=narrow><h1>{html.escape(title)}</h1>{body}</main></body></html>"
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _start_session(response: Response) -> None:
    token = auth.new_session(db)
    response.set_cookie(
        auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
        httponly=True, samesite="strict", secure=settings.cookie_secure, path="/",
    )


def _request_sync() -> None:
    try:
        settings.sync_request_file.write_text("1")
    except OSError:
        log.warning("Couldn't request a sync")


# ------------------------------------------------------------------ first run: create the account
_WELCOME_MESSAGES = {
    "name": auth.username_problem("") or "",
    "short": auth.password_problem("") or "",
    "long": f"Use at most {auth.MAX_PASSWORD_LENGTH} characters.",
    "match": "The two passwords didn't match.",
    "busy": "The server is busy. Try again in a moment.",
    "locked": "Too many attempts. Wait 15 minutes and try again.",
}


@app.get("/welcome")
def welcome_page(m: str = ""):
    note = _WELCOME_MESSAGES.get(m)  # fixed messages only; nothing from the URL is echoed
    return HTMLResponse(_page(
        "Welcome",
        "<p>Create the account you'll use to sign in to this dashboard. "
        "Only one account can exist, and you'll set up your Garmin connection next.</p>"
        + (f"<p class=notice>{html.escape(note)}</p>" if note else "")
        + "<form method=post action=/welcome class=login>"
        "<label for=un>Username</label>"
        "<input id=un name=username autocomplete=username required autofocus minlength=3 maxlength=32 "
        "pattern='[A-Za-z0-9._\\-]{3,32}' title='3-32 letters, numbers, dots, dashes or underscores'>"
        f"<label for=pw>Password <span class=hint>at least {auth.MIN_PASSWORD_LENGTH} characters</span></label>"
        f"<input id=pw name=password type=password autocomplete=new-password required minlength={auth.MIN_PASSWORD_LENGTH}>"
        "<label for=pw2>Repeat password</label>"
        "<input id=pw2 name=password2 type=password autocomplete=new-password required>"
        "<button class=btn type=submit>Create account</button></form>",
    ))


@app.post("/welcome")
def welcome_submit(request: Request, username: str = Form(..., max_length=64),
                   password: str = Form(..., max_length=1024), password2: str = Form(..., max_length=1024)):
    username = username.strip()
    if auth.username_problem(username):
        return RedirectResponse("/welcome?m=name", status_code=303)
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        return RedirectResponse("/welcome?m=short", status_code=303)
    if auth.password_problem(password):
        return RedirectResponse("/welcome?m=long", status_code=303)
    if password != password2:
        return RedirectResponse("/welcome?m=match", status_code=303)
    stamp = limiter.try_begin(_client_ip(request))
    if stamp is None:
        return RedirectResponse("/welcome?m=locked", status_code=303)
    created = auth.limited(auth.create_account, settings, username, password)
    limiter.release(stamp)
    if created is None:
        return RedirectResponse("/welcome?m=busy", status_code=303)
    if not created:  # someone else finished first
        return RedirectResponse("/login", status_code=303)
    log.info("Dashboard account created")
    resp = RedirectResponse("/setup", status_code=303)
    _start_session(resp)
    return resp


# ------------------------------------------------------------------ login
_LOGIN_MESSAGES = {
    "bad": "That username and password didn't match.",
    "locked": "Too many attempts. Wait 15 minutes and try again.",
    "out": "You're signed out.",
    "busy": "The server is busy checking other sign-ins. Try again in a moment.",
}


@app.get("/login")
def login_page(m: str = ""):
    note = _LOGIN_MESSAGES.get(m)  # fixed messages only; nothing from the URL is echoed
    return HTMLResponse(_page(
        "Training Dashboard",
        (f"<p class=notice>{html.escape(note)}</p>" if note else "")
        + "<form method=post action=/login class=login>"
        "<label for=un>Username</label>"
        "<input id=un name=username autocomplete=username required autofocus maxlength=32>"
        "<label for=pw>Password</label>"
        "<input id=pw name=password type=password autocomplete=current-password required>"
        "<button class=btn type=submit>Sign in</button></form>",
    ))


@app.post("/login")
def login_submit(request: Request, username: str = Form(..., max_length=64),
                 password: str = Form(..., max_length=1024)):
    ip = _client_ip(request)
    if not auth.account_exists(settings):
        return RedirectResponse("/welcome", status_code=303)
    stamp = limiter.try_begin(ip)  # counted before hashing, so bursts can't bypass the limit
    if stamp is None:
        return RedirectResponse("/login?m=locked", status_code=303)
    ok = auth.limited(auth.check_login, settings, username, password)
    if ok is None:
        limiter.release(stamp)  # the password wasn't checked, so don't charge an attempt
        return RedirectResponse("/login?m=busy", status_code=303)
    if not ok:
        log.warning("Failed dashboard login from %s", ip)
        return RedirectResponse("/login?m=bad", status_code=303)
    limiter.success(ip, stamp)
    resp = RedirectResponse("/", status_code=303)
    _start_session(resp)
    return resp


@app.post("/logout")
def logout(request: Request):
    auth.end_session(db, request.cookies.get(auth.SESSION_COOKIE))
    resp = RedirectResponse("/login?m=out", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/", samesite="strict", httponly=True, secure=settings.cookie_secure)
    return resp


# ------------------------------------------------------------------ dashboard data API
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/status")
def status():
    p = prefs.get_all(db)
    return {
        "api_key_configured": bool(settings.api_key()),
        "connect_configured": settings.connect_configured(),
        "units": p["units"],
        "theme": p["theme"],
        "include_cycling": p["include_cycling"],
        "race_enabled": bool(p["race_enabled"]),
        "ai_configured": ai.configured(settings, p),
        "ai_provider": p["ai_provider"],
        "layout": p["layout"],
        "sync_interval_hours": settings.sync_interval_hours,
        "syncing": sync.is_running(settings),
        "last_sync": db.last_sync(),
        "days_stored": db.count("daily"),
        "training_days_stored": db.count("training"),
        "activities_stored": db.count("activities"),
        "race": fitness.race_card(db),
        "username": auth.current_username(settings),
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
def weekly(weeks: int = Query(16, ge=1, le=260), type: str = Query("run", pattern="^(run|ride)$")):
    since = (date.today() - timedelta(weeks=weeks)).isoformat()
    return db.weekly(since, type)


@app.post("/api/sync")
def trigger_sync():
    global _last_manual_sync
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


# ------------------------------------------------------------------ settings & setup API
class PrefsIn(BaseModel):
    values: dict = Field(default_factory=dict)


class ApiKeyIn(BaseModel):
    key: str = Field(..., min_length=1, max_length=4096)


class ConnectIn(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=1, max_length=1024)


class MfaIn(BaseModel):
    pending: str = Field(..., min_length=10, max_length=100)
    code: str = Field(..., min_length=4, max_length=12, pattern=r"^\s*\d{4,10}\s*$")


class PasswordChangeIn(BaseModel):
    current: str = Field(..., min_length=1, max_length=1024)
    new: str = Field(..., min_length=1, max_length=1024)


class DeleteIn(BaseModel):
    password: str = Field(..., min_length=1, max_length=1024)
    scope: str = Field(..., pattern="^(garmin_data|everything)$")


class AskIn(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)


class AiKeyIn(BaseModel):
    provider: str = Field(..., pattern="^(claude|gemini|openai)$")
    key: str = Field(..., min_length=10, max_length=400)


class WebhookIn(BaseModel):
    url: str = Field(..., min_length=40, max_length=300)


class AiModelsIn(BaseModel):
    provider: str = Field(..., pattern="^(claude|gemini|openai|ollama)$")
    url: str | None = Field(None, max_length=200)


@app.get("/api/settings")
def get_settings():
    return {
        "prefs": prefs.get_all(db),
        "username": auth.current_username(settings),
        "api_key_configured": bool(settings.api_key()),
        "connect_configured": settings.connect_configured(),
        "race_distances": list(prefs.RACE_DISTANCES_KM) + ["custom"],
        "ai_providers": {k: {"label": v["label"], "keyed": v["keyed"], "key_url": v.get("key_url")}
                         for k, v in ai.PROVIDERS.items()},
        "ai_keys_saved": ai.keys_saved(settings),
        "discord_saved": notify.get_webhook(settings) is not None,
        "widgets": {k: {"title": t, "section": sec, "w": w, "h": h} for k, (t, sec, w, h) in prefs.WIDGETS.items()},
        "setup_done": db.get_meta("setup_done") is not None,
    }


@app.post("/api/settings")
def save_settings(body: PrefsIn):
    try:
        updated = prefs.update(db, body.values)
    except prefs.PrefError as e:
        return JSONResponse({"detail": str(e), "field": e.field}, status_code=422)
    return {"prefs": updated}


@app.post("/api/setup/done")
def setup_done():
    db.set_meta("setup_done", date.today().isoformat())
    return {"ok": True}


def _garmin_attempt(request: Request) -> float:
    stamp = garmin_limiter.try_begin(_client_ip(request))
    if stamp is None:
        raise HTTPException(429, "Too many Garmin sign-in attempts. Wait 15 minutes, so Garmin doesn't lock your account.")
    return stamp


@app.post("/api/garmin/api-key")
def save_api_key(request: Request, body: ApiKeyIn):
    from garmin_personal_api import HttpError, PersonalApiAuthenticator

    key = body.key.strip()
    if not key or any(c.isspace() for c in key):
        raise HTTPException(422, "That doesn't look like a key. Paste it exactly as Garmin showed it.")
    stamp = _garmin_attempt(request)
    try:
        with PersonalApiAuthenticator(bearer_token_cache_file=settings.token_cache) as a:
            a.obtain_token(key, force_refresh=True)
    except HttpError as e:
        raise HTTPException(400, f"Garmin rejected that key (HTTP {e.status_code}). Check it, or create a new one.") from e
    except Exception as e:
        raise HTTPException(502, f"Couldn't reach Garmin to check the key ({type(e).__name__}). Is the server online?") from e
    garmin_limiter.success(_client_ip(request), stamp)
    config.write_secret(settings.api_key_file, key + "\n")
    _request_sync()
    return {"ok": True}


@app.post("/api/garmin/api-key/remove")
def remove_api_key():
    settings.api_key_file.unlink(missing_ok=True)
    settings.token_cache.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/garmin/connect")
def connect_start(request: Request, body: ConnectIn):
    stamp = _garmin_attempt(request)
    try:
        pending = garmin_login.start(settings, db, body.email.strip(), body.password)
    except garmin_login.LoginError as e:
        raise HTTPException(400, str(e)) from e
    if pending:
        return {"status": "mfa", "pending": pending}
    garmin_limiter.success(_client_ip(request), stamp)
    _request_sync()
    return {"status": "connected"}


@app.post("/api/garmin/connect/mfa")
def connect_mfa(request: Request, body: MfaIn):
    try:
        garmin_login.finish(settings, db, body.pending, body.code)
    except garmin_login.LoginError as e:
        raise HTTPException(400, str(e)) from e
    _request_sync()
    return {"status": "connected"}


@app.post("/api/garmin/connect/remove")
def connect_remove():
    garmin_login.disconnect(settings)
    return {"ok": True}


@app.post("/api/account/password")
def change_password(request: Request, body: PasswordChangeIn):
    problem = auth.password_problem(body.new)
    if problem:
        raise HTTPException(422, problem)
    ip = _client_ip(request)
    stamp = limiter.try_begin(ip)
    if stamp is None:
        raise HTTPException(429, "Too many attempts. Wait 15 minutes and try again.")
    ok = auth.limited(auth.check_password, settings, body.current)
    if ok is None:
        limiter.release(stamp)
        raise HTTPException(503, "The server is busy. Try again in a moment.")
    if not ok:
        raise HTTPException(400, "Your current password didn't match.")
    limiter.success(ip, stamp)
    auth.set_password(settings, body.new)
    removed = auth.end_other_sessions(db, request.cookies.get(auth.SESSION_COOKIE))
    return {"ok": True, "signed_out": removed}


@app.post("/api/account/logout-others")
def logout_others(request: Request):
    return {"signed_out": auth.end_other_sessions(db, request.cookies.get(auth.SESSION_COOKIE))}


@app.get("/api/export")
def export_data():
    payload = {
        "exported_at": date.today().isoformat(),
        "app": "garmin-dashboard",
        "note": "Your stored Garmin data. Contains no passwords, keys or sign-in tokens.",
        "preferences": prefs.get_all(db),
        "daily": db.rows_since("daily", "0000-01-01"),
        "training": db.rows_since("training", "0000-01-01"),
        "activities": db.activities(1_000_000),
    }
    name = f"garmin-dashboard-export-{date.today().isoformat()}.json"
    return Response(json.dumps(payload, indent=1, default=str), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.post("/api/data/delete")
def delete_data(request: Request, body: DeleteIn):
    ip = _client_ip(request)
    stamp = limiter.try_begin(ip)
    if stamp is None:
        raise HTTPException(429, "Too many attempts. Wait 15 minutes and try again.")
    ok = auth.limited(auth.check_password, settings, body.password)
    if ok is None:
        limiter.release(stamp)
        raise HTTPException(503, "The server is busy. Try again in a moment.")
    if not ok:
        raise HTTPException(400, "Password didn't match. Nothing was deleted.")
    limiter.success(ip, stamp)
    with sync.data_lock(settings, blocking=True):
        db.wipe_garmin_data()
        if body.scope == "everything":
            import shutil
            shutil.rmtree(settings.secrets_dir, ignore_errors=True)
            config.ensure_private_dir(settings.secrets_dir)
            db.wipe_everything_else()
    log.warning("Data deleted from the dashboard (scope: %s)", body.scope)
    if body.scope == "everything":
        resp = JSONResponse({"ok": True, "next": "/welcome"})
        resp.delete_cookie(auth.SESSION_COOKIE, path="/", samesite="strict", httponly=True, secure=settings.cookie_secure)
        return resp
    return {"ok": True}


# ------------------------------------------------------------------ fitness & AI coach
@app.get("/api/overview")
def get_overview(days: int = Query(90, ge=7, le=365)):
    return fitness.overview(db, days)


@app.get("/api/insight")
def get_insight():
    p = prefs.get_all(db)
    return {"configured": ai.configured(settings, p), "provider": p["ai_provider"],
            "insight": ai.stored_insight(db), "today": date.today().isoformat()}


@app.post("/api/insight/refresh")
def refresh_insight():
    try:
        return {"insight": ai.generate_insight(settings, db, reason="manual")}
    except ai.AIError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/insight/ask")
def ask_coach(body: AskIn):
    try:
        return ai.ask(settings, db, body.question.strip())
    except ai.AIError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/ai/key")
def save_ai_key(request: Request, body: AiKeyIn):
    key = body.key.strip()
    if any(c.isspace() for c in key):
        raise HTTPException(422, "Paste the key exactly as the provider showed it.")
    stamp = ai_limiter.try_begin(_client_ip(request))
    if stamp is None:
        raise HTTPException(429, "Too many attempts. Wait 15 minutes.")
    try:
        models = ai.list_models(body.provider, key)
    except ai.AIError as e:
        raise HTTPException(400, str(e)) from e
    ai_limiter.success(_client_ip(request), stamp)
    ai.save_key(settings, body.provider, key)
    return {"ok": True, "models": models, "suggested": ai.suggest_model(body.provider, models)}


@app.post("/api/ai/key/remove")
def remove_ai_key(body: AiModelsIn):
    if body.provider in ("claude", "gemini", "openai"):
        ai.remove_key(settings, body.provider)
    return {"ok": True}


@app.post("/api/ai/models")
def ai_models(body: AiModelsIn):
    if body.provider == "ollama":
        try:
            url = prefs.validate({"ollama_url": body.url or ""})["ollama_url"]
        except prefs.PrefError as e:
            raise HTTPException(422, str(e)) from e
        if not url:
            raise HTTPException(422, "Enter the Ollama address first.")
        key = None
    else:
        url, key = None, ai.get_key(settings, body.provider)
        if not key:
            raise HTTPException(400, "Save an API key for this provider first.")
    try:
        models = ai.list_models(body.provider, key, url)
    except ai.AIError as e:
        raise HTTPException(502, str(e)) from e
    return {"models": models, "suggested": ai.suggest_model(body.provider, models)}


# ------------------------------------------------------------------ Discord
@app.post("/api/notify/webhook")
def save_webhook(request: Request, body: WebhookIn):
    url = body.url.strip()
    if not notify.WEBHOOK_RE.match(url):
        raise HTTPException(422, "That isn't a Discord webhook link. It starts with https://discord.com/api/webhooks/")
    stamp = ai_limiter.try_begin(_client_ip(request))
    if stamp is None:
        raise HTTPException(429, "Too many attempts. Wait 15 minutes.")
    try:
        notify.send(settings, "morning", "Connected", ["Garmin Dashboard will post your notifications here."], url=url)
    except notify.NotifyError as e:
        raise HTTPException(400, str(e)) from e
    ai_limiter.success(_client_ip(request), stamp)
    notify.save_webhook(settings, url)
    return {"ok": True}


@app.post("/api/notify/webhook/remove")
def remove_webhook():
    notify.remove_webhook(settings)
    return {"ok": True}


@app.post("/api/notify/test")
def test_notify():
    try:
        notify.morning(settings, db)
    except notify.NotifyError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


# ------------------------------------------------------------------ pages
@app.get("/raw")
def raw_page():
    return FileResponse(STATIC / "raw.html")


@app.get("/setup")
def setup_page():
    return FileResponse(STATIC / "setup.html")


@app.get("/settings")
def settings_page():
    return FileResponse(STATIC / "settings.html")


@app.get("/")
def index():
    if db.get_meta("setup_done") is None:
        return RedirectResponse("/setup", status_code=303)
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
