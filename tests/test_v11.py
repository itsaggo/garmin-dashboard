"""v1.1: account creation, web setup, Settings, export/delete, coach, Ollama."""
import json
import os
import re
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import garminconnect
import pytest

from app import auth, main, prefs
from tests.helpers import JSON, ORIGIN, PASSWORD, USER, client as new_client, login, make_account, reset
from tests.test_connect import FakeGarmin

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture()
def c():
    reset()
    return new_client()


@pytest.fixture()
def signed_in(c):
    make_account()
    login(c)
    return c


# ------------------------------------------------------------------ account creation
def test_welcome_validates_and_creates_account(c):
    def post(**form):
        data = {"username": USER, "password": PASSWORD, "password2": PASSWORD, **form}
        return c.post("/welcome", data=data, headers=ORIGIN).headers["location"]

    assert post(username="a b") == "/welcome?m=name"
    assert post(password="short", password2="short") == "/welcome?m=short"
    assert post(password2="different password!!") == "/welcome?m=match"
    assert not auth.account_exists(main.settings)
    assert post() == "/setup"
    assert auth.SESSION_COOKIE in c.cookies  # signed straight in
    assert c.get("/api/settings").status_code == 200
    # the password is stored only as a hash, in a private file
    raw = main.settings.account_file.read_text()
    assert PASSWORD not in raw and json.loads(raw)["username"] == USER
    assert stat.S_IMODE(main.settings.account_file.stat().st_mode) == 0o600
    # a second visitor can't create another account or replace this one
    other = new_client()
    r = other.post("/welcome", data={"username": "intruder", "password": "x" * 20, "password2": "x" * 20}, headers=ORIGIN)
    assert r.headers["location"] == "/login"
    assert auth.current_username(main.settings) == USER
    assert other.get("/welcome").headers["location"] == "/login"


def test_simultaneous_signups_only_one_wins(c):
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: auth.create_account(main.settings, f"user{i}", PASSWORD), range(8)))
    assert results.count(True) == 1


def test_legacy_password_only_install_migrates_to_admin(c):
    main.settings.password_file.write_text(auth.hash_password(PASSWORD))
    assert auth.current_username(main.settings) == "admin"
    assert main.settings.account_file.exists() and not main.settings.password_file.exists()
    login(c, user="admin")


def test_login_username_case_insensitive_and_wrong_user_fails(c):
    make_account()
    login(c, user="ADAM")
    r = new_client().post("/login", data={"username": "eve", "password": PASSWORD}, headers=ORIGIN)
    assert r.headers["location"] == "/login?m=bad"


def test_json_posts_need_custom_header(signed_in):
    assert signed_in.post("/api/settings", json={"values": {}}, headers=ORIGIN).status_code == 403
    assert signed_in.post("/api/settings", json={"values": {}}, headers=JSON).status_code == 200


# ------------------------------------------------------------------ preferences
def test_prefs_defaults_are_houston():
    p = prefs.DEFAULTS
    assert p["timezone"] == "America/Chicago" and p["units"] == "imperial"


@pytest.mark.parametrize("field,value", [
    ("units", "furlongs"), ("timezone", "Mars/Olympus"), ("theme", "neon"), ("race_date", "next week"),
    ("race_distance", "ultra"), ("race_custom_km", 9999), ("race_goal_time", "2 hours"),
    ("include_cycling", "yes"), ("ollama_url", "file:///etc/passwd"), ("ollama_url", "http://user:pw@host"),
    ("ai_model", "bad model; rm"), ("ai_provider", "skynet"), ("race_enabled", "yes"), ("layout", "big"),
    ("race_name", "x" * 81), ("not_a_setting", 1),
])
def test_bad_prefs_rejected(signed_in, field, value):
    r = signed_in.post("/api/settings", json={"values": {field: value}}, headers=JSON)
    assert r.status_code == 422 and r.json()["field"] == field


def test_prefs_save_and_timezone_applies(signed_in):
    vals = {"units": "metric", "timezone": "Europe/London", "theme": "dark", "race_enabled": True, "race_name": "New Orleans Half",
            "race_date": "2026-11-14", "race_distance": "half", "race_goal_time": "2:05:00", "include_cycling": False}
    r = signed_in.post("/api/settings", json={"values": vals}, headers=JSON)
    assert r.status_code == 200 and r.json()["prefs"]["units"] == "metric"
    assert os.environ["TZ"] == "Europe/London"
    st = signed_in.get("/api/status").json()
    assert st["units"] == "metric" and st["theme"] == "dark" and st["race"]["name"] == "New Orleans Half"
    signed_in.post("/api/settings", json={"values": {"timezone": "America/Chicago"}}, headers=JSON)
    assert os.environ["TZ"] == "America/Chicago"


# ------------------------------------------------------------------ Personal API key from the browser
def test_api_key_saved_only_after_garmin_accepts(signed_in, monkeypatch):
    from garmin_personal_api import HttpError, PersonalApiAuthenticator

    def fake_obtain(self, key, force_refresh=False):
        if key != "GOODKEY123":
            raise HttpError(status_code=401, method="POST", url="x", response_text="")
    monkeypatch.setattr(PersonalApiAuthenticator, "obtain_token", fake_obtain)
    bad = signed_in.post("/api/garmin/api-key", json={"key": "nope"}, headers=JSON)
    assert bad.status_code == 400 and not main.settings.api_key_file.exists()
    assert signed_in.post("/api/garmin/api-key", json={"key": "has space"}, headers=JSON).status_code == 422
    ok = signed_in.post("/api/garmin/api-key", json={"key": "GOODKEY123"}, headers=JSON)
    assert ok.status_code == 200 and main.settings.api_key() == "GOODKEY123"
    assert stat.S_IMODE(main.settings.api_key_file.stat().st_mode) == 0o600
    assert signed_in.get("/api/settings").json()["api_key_configured"] is True
    assert "GOODKEY123" not in signed_in.get("/api/settings").text  # never sent back to the browser
    signed_in.post("/api/garmin/api-key/remove", headers=JSON)
    assert not main.settings.api_key_file.exists()


def test_garmin_attempts_rate_limited(signed_in, monkeypatch):
    from garmin_personal_api import HttpError, PersonalApiAuthenticator
    monkeypatch.setattr(PersonalApiAuthenticator, "obtain_token",
                        lambda self, k, force_refresh=False: (_ for _ in ()).throw(
                            HttpError(status_code=401, method="POST", url="x", response_text="")))
    codes = [signed_in.post("/api/garmin/api-key", json={"key": f"k{i}"}, headers=JSON).status_code for i in range(7)]
    assert codes[:5] == [400] * 5 and codes[5:] == [429, 429]


# ------------------------------------------------------------------ Garmin Connect sign-in from the browser
@pytest.fixture()
def fake_garmin(monkeypatch):
    monkeypatch.setattr(garminconnect, "Garmin", FakeGarmin)
    FakeGarmin.calls, FakeGarmin.mode, FakeGarmin.instances = [], "ok", []
    import shutil
    shutil.rmtree(main.settings.connect_token_dir, ignore_errors=True)
    yield FakeGarmin
    shutil.rmtree(main.settings.connect_token_dir, ignore_errors=True)


def test_web_connect_without_2fa(signed_in, fake_garmin):
    r = signed_in.post("/api/garmin/connect", json={"email": "me@example.com", "password": "right-password"}, headers=JSON)
    assert r.json() == {"status": "connected"}
    assert main.settings.connect_configured()
    assert b"right-password" not in main.settings.connect_token_file.read_bytes()


def test_web_connect_with_2fa_retry(signed_in, fake_garmin):
    fake_garmin.mode = "mfa"
    r = signed_in.post("/api/garmin/connect", json={"email": "me@example.com", "password": "right-password"}, headers=JSON)
    assert r.json()["status"] == "mfa"
    pending = r.json()["pending"]
    bad = signed_in.post("/api/garmin/connect/mfa", json={"pending": pending, "code": "000000"}, headers=JSON)
    assert bad.status_code == 400 and "try again" in bad.json()["detail"]
    good = signed_in.post("/api/garmin/connect/mfa", json={"pending": pending, "code": "123456"}, headers=JSON)
    assert good.json() == {"status": "connected"} and main.settings.connect_configured()
    again = signed_in.post("/api/garmin/connect/mfa", json={"pending": pending, "code": "123456"}, headers=JSON)
    assert again.status_code == 400 and "expired" in again.json()["detail"]  # single use


def test_web_connect_wrong_password(signed_in, fake_garmin):
    r = signed_in.post("/api/garmin/connect", json={"email": "me@example.com", "password": "nope"}, headers=JSON)
    assert r.status_code == 400 and not main.settings.connect_configured()
    assert not list(main.settings.secrets_dir.glob("garminconnect.new*"))


def test_mfa_code_format_validated(signed_in):
    r = signed_in.post("/api/garmin/connect/mfa", json={"pending": "x" * 20, "code": "abc"}, headers=JSON)
    assert r.status_code == 422


# ------------------------------------------------------------------ account management
def test_change_password_keeps_this_session_only(signed_in):
    other = new_client()
    login(other)
    bad = signed_in.post("/api/account/password", json={"current": "wrong", "new": "another long passphrase"}, headers=JSON)
    assert bad.status_code == 400
    short = signed_in.post("/api/account/password", json={"current": PASSWORD, "new": "short"}, headers=JSON)
    assert short.status_code == 422
    ok = signed_in.post("/api/account/password", json={"current": PASSWORD, "new": "a brand new passphrase"}, headers=JSON)
    assert ok.status_code == 200 and ok.json()["signed_out"] == 1
    assert signed_in.get("/api/status").status_code == 200
    assert other.get("/api/status").status_code == 401
    assert auth.check_login(main.settings, USER, "a brand new passphrase")


def test_logout_others(signed_in):
    other = new_client()
    login(other)
    assert signed_in.post("/api/account/logout-others", headers=JSON).json()["signed_out"] == 1
    assert other.get("/api/status").status_code == 401 and signed_in.get("/api/status").status_code == 200


# ------------------------------------------------------------------ export & delete
def test_export_has_data_but_no_secrets(signed_in):
    main.settings.api_key_file.write_text("SECRETKEY999")
    main.db.upsert_daily("2026-11-01", {"steps": 1234})
    r = signed_in.get("/api/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    body = r.text
    assert '"steps": 1234' in body
    acct = json.loads(main.settings.account_file.read_text())
    for secret in ("SECRETKEY999", acct["hash"], PASSWORD):
        assert secret not in body


def test_delete_garmin_data_keeps_account(signed_in):
    main.db.upsert_daily("2026-11-01", {"steps": 1})
    bad = signed_in.post("/api/data/delete", json={"password": "wrong", "scope": "garmin_data"}, headers=JSON)
    assert bad.status_code == 400 and main.db.count("daily") == 1
    ok = signed_in.post("/api/data/delete", json={"password": PASSWORD, "scope": "garmin_data"}, headers=JSON)
    assert ok.status_code == 200 and main.db.count("daily") == 0
    assert auth.account_exists(main.settings) and signed_in.get("/api/status").status_code == 200


def test_delete_everything_resets_to_first_run(signed_in):
    main.settings.api_key_file.write_text("SECRETKEY999")
    main.db.upsert_daily("2026-11-01", {"steps": 1})
    r = signed_in.post("/api/data/delete", json={"password": PASSWORD, "scope": "everything"}, headers=JSON)
    assert r.json()["next"] == "/welcome"
    assert not auth.account_exists(main.settings) and not main.settings.api_key_file.exists()
    assert main.db.count("daily") == 0
    assert signed_in.get("/").headers["location"] == "/welcome"


# ------------------------------------------------------------------ static pages: CSP-safe
@pytest.mark.parametrize("name", ["index.html", "setup.html", "settings.html", "raw.html"])
def test_pages_have_no_inline_code(name):
    html = (STATIC / name).read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script>"
    assert not re.search(r"\son[a-z]+=", html), "inline event handler"
    assert not re.search(r"\sstyle=", html), "inline style attribute"


