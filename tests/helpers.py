"""Shared test helpers."""
from fastapi.testclient import TestClient

from app import auth, main

ORIGIN = {"Origin": "http://testserver"}
JSON = {"Origin": "http://testserver", "X-Requested-With": "dashboard"}
USER = "adam"
PASSWORD = "correct horse battery staple"


def reset():
    """Fresh app state: no account, no sessions, default prefs, empty limiters."""
    s = main.settings
    for f in (s.account_file, s.password_file, s.api_key_file):
        f.unlink(missing_ok=True)
    main.db.session_delete_all()
    main.db.wipe_garmin_data()  # every test starts with no synced data
    for k in ("prefs", "setup_done", "insight", "insight_attempt", "ai_usage", "profile", "notify_sent", "weather_cache"):
        main.db.delete_meta(k)
    (s.secrets_dir / "discord-webhook").unlink(missing_ok=True)
    for prov in ("claude", "gemini", "openai"):
        (s.secrets_dir / f"ai-key-{prov}").unlink(missing_ok=True)
    main.limiter = auth.LoginLimiter()
    main.device_limiter = auth.LoginLimiter(per_ip=5, total=1_000_000)
    main.db.delete_meta("devices")
    main.confirm_limiter = auth.LoginLimiter(per_ip=5, total=1_000_000)
    main._warned_peers.clear()
    main.garmin_limiter = auth.LoginLimiter(per_ip=5, total=8)
    main._last_manual_sync = 0.0
    main.ai_limiter = auth.LoginLimiter(per_ip=10, total=20)


def client():
    return TestClient(main.app, follow_redirects=False)


def make_account():
    assert auth.create_account(main.settings, USER, PASSWORD)


def login(c, user=USER, password=PASSWORD):
    r = c.post("/login", data={"username": user, "password": password}, headers=ORIGIN)
    assert r.status_code == 303 and r.headers["location"] == "/", r.headers.get("location")
    return r
