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
    for k in ("prefs", "setup_done"):
        main.db.delete_meta(k)
    main.limiter = auth.LoginLimiter()
    main.garmin_limiter = auth.LoginLimiter(per_ip=5, total=8)
    main._last_manual_sync = 0.0
    main._ai_cache.clear()


def client():
    return TestClient(main.app, follow_redirects=False)


def make_account():
    assert auth.create_account(main.settings, USER, PASSWORD)


def login(c, user=USER, password=PASSWORD):
    r = c.post("/login", data={"username": user, "password": password}, headers=ORIGIN)
    assert r.status_code == 303 and r.headers["location"] == "/", r.headers.get("location")
    return r
