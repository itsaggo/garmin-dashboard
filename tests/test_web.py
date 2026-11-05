"""Login, session and request-hardening tests against the real FastAPI app."""
import pytest
from fastapi.testclient import TestClient

from app import auth, main

from tests.helpers import JSON, ORIGIN, PASSWORD, USER, client as _client, login, make_account, reset


@pytest.fixture()
def client():
    reset()
    return _client()


def test_first_visit_goes_to_account_creation(client):
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/welcome"
    assert client.get("/api/daily").status_code == 503
    assert client.get("/welcome").status_code == 200
    assert client.get("/healthz").json() == {"ok": True}


def test_everything_requires_login(client):
    make_account()
    for path in ("/", "/raw", "/static/app.js", "/static/index.html"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    for path in ("/api/status", "/api/daily", "/api/training", "/api/raw", "/api/raw/x", "/api/activities"):
        assert client.get(path).status_code == 401, path
    # the login page and its stylesheet are the only public things
    assert client.get("/login").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_login_cookie_flags_and_logout(client):
    make_account()
    r = login(client)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert client.get("/api/status").status_code == 200
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/setup"  # wizard until setup is finished
    assert client.post("/api/setup/done", headers=JSON).json() == {"ok": True}
    assert client.get("/").status_code == 200
    r = client.post("/logout", headers=ORIGIN)
    assert r.status_code == 303
    assert client.get("/api/status").status_code == 401


def test_stolen_cookie_dies_on_logout(client):
    make_account()
    login(client)
    token = client.cookies.get(auth.SESSION_COOKIE)
    client.post("/logout", headers=ORIGIN)
    other = TestClient(main.app, follow_redirects=False)
    other.cookies.set(auth.SESSION_COOKIE, token)
    assert other.get("/api/status").status_code == 401


def test_bogus_cookie_rejected(client):
    make_account()
    client.cookies.set(auth.SESSION_COOKIE, "x" * 43)
    assert client.get("/api/status").status_code == 401
    client.cookies.set(auth.SESSION_COOKIE, "x" * 5000)
    assert client.get("/api/status").status_code == 401


def test_wrong_password_then_lockout(client):
    make_account()
    for _ in range(5):
        r = client.post("/login", data={"username": USER, "password": "nope"}, headers=ORIGIN)
        assert r.headers["location"] == "/login?m=bad"
    # 6th attempt is refused even with the right password
    r = client.post("/login", data={"username": USER, "password": PASSWORD}, headers=ORIGIN)
    assert r.headers["location"] == "/login?m=locked"
    assert auth.SESSION_COOKIE not in client.cookies


def test_cross_site_posts_blocked(client):
    make_account()
    assert client.post("/login", data={"username": USER, "password": PASSWORD}).status_code == 403  # no Origin at all
    r = client.post("/login", data={"username": USER, "password": PASSWORD}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    login(client)
    assert client.post("/logout", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/sync", headers={"Origin": "http://evil.example", "X-Requested-With": "dashboard"}).status_code == 403


def test_sync_needs_custom_header(client):
    make_account()
    login(client)
    assert client.post("/api/sync", headers=ORIGIN).status_code == 403
    r = client.post("/api/sync", headers={**ORIGIN, "X-Requested-With": "dashboard"})
    assert r.status_code == 400  # nothing configured to sync in the test data folder


def test_other_methods_rejected(client):
    make_account()
    login(client)
    for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
        assert client.request(method, "/api/status").status_code == 405


def test_security_headers(client):
    make_account()
    for r in (client.get("/login"), client.get("/api/status"), client.get("/")):
        h = r.headers
        assert "default-src 'none'" in h["content-security-policy"]
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
        assert h["cache-control"] == "no-store"
        assert h["referrer-policy"] == "same-origin"


def test_login_page_never_echoes_input(client):
    make_account()
    r = client.get("/login?m=<script>alert(1)</script>")
    assert "<script>alert" not in r.text and r.status_code == 200


def test_raw_endpoint_lookup_is_parameterized(client):
    make_account()
    login(client)
    assert client.get("/api/raw/x' OR '1'='1").status_code == 404


def test_weekly_type_validated(client):
    make_account()
    login(client)
    assert client.get("/api/weekly?type=run").status_code == 200
    assert client.get("/api/weekly?type=ride").status_code == 200
    assert client.get("/api/weekly?type=%25' OR 1=1--").status_code == 422


def test_password_hashing():
    h = auth.hash_password("hunter2hunter2")
    assert h.startswith("scrypt$") and "hunter2" not in h
    assert auth.verify_password("hunter2hunter2", h)
    assert not auth.verify_password("hunter2hunter3", h)
    assert not auth.verify_password("x", "garbage")
    assert auth.hash_password("same") != auth.hash_password("same")  # salted


def test_secret_files_are_private(client):
    import stat
    make_account()
    mode = stat.S_IMODE(main.settings.account_file.stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(main.settings.secrets_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(main.settings.db_path.stat().st_mode) == 0o600
