"""Garmin Connect sync and setup tests, using a fake garminconnect client (no network)."""
import builtins
import getpass
from datetime import date, timedelta

import garminconnect
import pytest

from app import config, connect, setup, sync
from app.db import Database

TODAY = date.today()


def d(offset):
    return (TODAY - timedelta(days=offset)).isoformat()


class FakeGarmin:
    """Mimics the parts of garminconnect.Garmin the dashboard uses."""
    calls: list = []
    mode = "ok"  # ok | auth_expired | rate_limited_once | bad_range

    def __init__(self, email=None, password=None, prompt_mfa=None, retry_attempts=3, **kw):
        self.email, self.password, self.prompt_mfa = email, password, prompt_mfa
        self._rate_hit = False

    def login(self, tokenstore=None):
        FakeGarmin.calls.append(("login", tokenstore))
        if FakeGarmin.mode == "auth_expired":
            raise garminconnect.GarminConnectAuthenticationError("Username and password are required")
        if self.email:  # credential login from setup: write a token file like the real library
            if self.prompt_mfa:
                assert self.prompt_mfa() == "123456"
            from pathlib import Path
            p = Path(tokenstore) / "garmin_tokens.json"
            p.write_text('{"di_token": "fresh"}')
            p.chmod(0o600)
        return None, None

    def get_max_metrics_range(self, start, end):
        FakeGarmin.calls.append(("vo2", start, end))
        if FakeGarmin.mode == "bad_range" and start != end:
            raise garminconnect.GarminConnectConnectionError("Max metrics client error (400)")
        return [{"generic": {"calendarDate": end, "vo2MaxPreciseValue": 51.7}, "cycling": None}]

    def get_training_readiness_range(self, start, end):
        FakeGarmin.calls.append(("ready", start, end))
        if FakeGarmin.mode == "rate_limited_once" and not self._rate_hit:
            self._rate_hit = True
            raise garminconnect.GarminConnectTooManyRequestsError("Rate limit exceeded")
        return [{"calendarDate": end, "timestampLocal": f"{end}T06:30:00", "score": 72, "level": "HIGH",
                 "recoveryTime": 600, "inputContext": "AFTER_WAKEUP_RESET"}]

    def get_training_status(self, cdate):
        FakeGarmin.calls.append(("status", cdate))
        if cdate == d(1):
            raise garminconnect.GarminConnectNotFoundError("no data")
        return {"mostRecentTrainingStatus": {"latestTrainingStatusData": {"1": {
            "calendarDate": cdate, "trainingStatusFeedbackPhrase": "PRODUCTIVE_2", "primaryTrainingDevice": True,
            "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 300, "dailyTrainingLoadChronic": 250}}}}}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONNECT_BACKFILL_DAYS", "10")
    monkeypatch.setattr(garminconnect, "Garmin", FakeGarmin)
    monkeypatch.setattr(connect.time, "sleep", lambda s: None)
    FakeGarmin.calls, FakeGarmin.mode = [], "ok"
    s = config.load()
    config.ensure_private_dir(s.connect_token_dir)
    s.connect_token_file.write_text('{"di_token": "t"}')
    return s, Database(s.db_path)


def test_connect_sync_stores_training_rows(env):
    s, db = env
    msg = connect.ConnectSync(s, db).run()
    assert "FAILED" not in msg
    rows = {r["date"]: r for r in db.rows_since("training", d(30))}
    assert rows[TODAY.isoformat()]["vo2max"] == 51.7
    assert rows[TODAY.isoformat()]["readiness_score"] == 72
    assert rows[TODAY.isoformat()]["recovery_min"] == 600
    assert rows[d(0)]["training_status"] == "Productive"
    assert d(1) not in rows or rows[d(1)].get("training_status") is None  # 404 day skipped
    assert db.get_meta("connect_backfilled")
    # first run backfills 10 days of status, one call each
    assert sum(1 for c in FakeGarmin.calls if c[0] == "status") == 10
    # incremental run afterwards: 3 days
    FakeGarmin.calls = []
    connect.ConnectSync(s, db).run()
    assert sum(1 for c in FakeGarmin.calls if c[0] == "status") == 3


def test_connect_splits_rejected_ranges(env):
    s, db = env
    FakeGarmin.mode = "bad_range"
    msg = connect.ConnectSync(s, db).run()
    assert "FAILED" not in msg
    single_day = [c for c in FakeGarmin.calls if c[0] == "vo2" and c[1] == c[2]]
    assert len(single_day) == 10  # the 10-day range was split down to each day
    assert len([r for r in db.rows_since("training", d(30)) if r["vo2max"]]) == 10


def test_connect_rate_limit_backoff(env):
    s, db = env
    FakeGarmin.mode = "rate_limited_once"
    msg = connect.ConnectSync(s, db).run()
    assert "FAILED" not in msg


def test_connect_expired_login_reported_and_personal_sync_unaffected(env):
    s, db = env
    FakeGarmin.mode = "auth_expired"
    with pytest.raises(connect.ConnectAuthExpired):
        connect.ConnectSync(s, db)
    result = sync.run_sync(s, db)  # no Personal API key in this folder; Connect fails cleanly
    assert result["status"] == "partial" and "app.setup connect" in result["message"]


def test_setup_connect_uses_staging_and_never_saves_password(env, monkeypatch):
    s, db = env
    old = s.connect_token_file.read_text()
    answers = iter(["y", "me@example.com"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": "123456" if "code" in prompt else next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "s3cret-password")
    setup.cmd_connect(s)
    assert s.connect_token_file.is_file()
    assert not (s.secrets_dir / "garminconnect.new").exists()
    for f in s.data_dir.rglob("*"):
        if f.is_file():
            assert b"s3cret-password" not in f.read_bytes(), f
    assert old != '{"di_token": "fresh"}'
    assert s.connect_token_file.read_text() == '{"di_token": "fresh"}'  # new sign-in swapped in
    import stat
    assert stat.S_IMODE(s.connect_token_dir.stat().st_mode) == 0o700


def test_setup_connect_failure_keeps_old_login(env, monkeypatch):
    s, db = env
    old = s.connect_token_file.read_text()
    FakeGarmin.mode = "auth_expired"
    answers = iter(["y", "me@example.com"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "wrong")
    setup.cmd_connect(s)
    assert s.connect_token_file.read_text() == old  # previous working sign-in untouched
    assert not (s.secrets_dir / "garminconnect.new").exists()


def test_setup_password_rules(env, monkeypatch):
    s, db = env
    pw = iter(["short", "long enough password", "mismatch-mismatch", "long enough password", "long enough password"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(pw))
    setup.cmd_password(s)
    from app import auth
    assert auth.check_password(s, "long enough password")


def test_setup_connect_rename_failure_restores_old_login(env, monkeypatch):
    from pathlib import Path
    s, db = env
    old = s.connect_token_file.read_text()
    answers = iter(["y", "me@example.com"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": "123456" if "code" in prompt else next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "pw")
    real_rename = Path.rename

    def flaky(self, target):
        if self.name == "garminconnect.new":
            raise OSError("disk full")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    setup.cmd_connect(s)
    assert s.connect_token_file.read_text() == old  # previous sign-in put back
    assert not (s.secrets_dir / "garminconnect.new").exists()
    assert not (s.secrets_dir / "garminconnect.old").exists()
