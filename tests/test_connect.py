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


class _FakeInnerClient:
    """garminconnect.Garmin().client: holds tokens and can dump them like the real one."""

    def __init__(self):
        self.di_token = None

    def dump(self, path):
        from pathlib import Path
        p = Path(path) / "garmin_tokens.json"
        p.write_text('{"di_token": "%s"}' % self.di_token)
        p.chmod(0o600)


class FakeGarmin:
    """Mimics the parts of garminconnect.Garmin the dashboard uses (incl. return_on_mfa)."""
    calls: list = []
    mode = "ok"  # ok | auth_expired | rate_limited_once | bad_range | mfa
    instances: list = []

    def __init__(self, email=None, password=None, prompt_mfa=None, return_on_mfa=False, retry_attempts=3, **kw):
        self.email, self.password, self.return_on_mfa = email, password, return_on_mfa
        self.client = _FakeInnerClient()
        self._rate_hit = False
        FakeGarmin.instances.append(self)

    def login(self, tokenstore=None):
        FakeGarmin.calls.append(("login", tokenstore))
        if FakeGarmin.mode == "auth_expired":
            raise garminconnect.GarminConnectAuthenticationError("Username and password are required")
        if self.email:  # credential sign-in from setup
            if self.password != "right-password":
                raise garminconnect.GarminConnectAuthenticationError("401 Unauthorized")
            if FakeGarmin.mode == "mfa":
                return "needs_mfa", {"cookies": "pending"}
            self.client.di_token = "fresh"
        return None, None

    def resume_login(self, state, code):
        FakeGarmin.calls.append(("resume", code))
        if code != "123456":
            raise garminconnect.GarminConnectAuthenticationError("bad code")
        self.client.di_token = "fresh"
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
    FakeGarmin.calls, FakeGarmin.mode, FakeGarmin.instances = [], "ok", []
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


def _answers(monkeypatch, inputs, password):
    it = iter(inputs)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(it))
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": password)


def test_connect_signin_never_saves_password(env, monkeypatch):
    s, db = env
    old = s.connect_token_file.read_text()
    _answers(monkeypatch, ["me@example.com"], "right-password")
    setup.cmd_connect(s)
    assert old != '{"di_token": "fresh"}'
    assert s.connect_token_file.read_text() == '{"di_token": "fresh"}'  # new sign-in swapped in
    assert not list(s.secrets_dir.glob("garminconnect.new*")) and not (s.secrets_dir / "garminconnect.old").exists()
    for f in s.data_dir.rglob("*"):
        if f.is_file():
            assert b"right-password" not in f.read_bytes(), f
    assert all(g.password is None for g in FakeGarmin.instances if g.email)  # dropped from memory too
    import stat
    assert stat.S_IMODE(s.connect_token_dir.stat().st_mode) == 0o700


def test_connect_two_factor_with_one_wrong_code(env, monkeypatch):
    s, db = env
    FakeGarmin.mode = "mfa"
    _answers(monkeypatch, ["me@example.com", "000000", "123456"], "right-password")
    setup.cmd_connect(s)
    assert s.connect_token_file.read_text() == '{"di_token": "fresh"}'
    assert [c for c in FakeGarmin.calls if c[0] == "resume"] == [("resume", "000000"), ("resume", "123456")]


def test_connect_two_factor_gives_up_after_three_wrong_codes(env, monkeypatch):
    s, db = env
    old = s.connect_token_file.read_text()
    FakeGarmin.mode = "mfa"
    _answers(monkeypatch, ["me@example.com", "1111", "2222", "3333"], "right-password")
    setup.cmd_connect(s)
    assert s.connect_token_file.read_text() == old
    assert not list(s.secrets_dir.glob("garminconnect.new*"))


def test_connect_failure_keeps_old_login(env, monkeypatch):
    s, db = env
    old = s.connect_token_file.read_text()
    _answers(monkeypatch, ["me@example.com"], "wrong")
    setup.cmd_connect(s)
    assert s.connect_token_file.read_text() == old  # previous working sign-in untouched
    assert not list(s.secrets_dir.glob("garminconnect.new*"))


def test_connect_rename_failure_restores_old_login(env, monkeypatch):
    from pathlib import Path
    s, db = env
    old = s.connect_token_file.read_text()
    _answers(monkeypatch, ["me@example.com"], "right-password")
    real_rename = Path.rename

    def flaky(self, target):
        if self.name.startswith("garminconnect.new"):
            raise OSError("disk full")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    setup.cmd_connect(s)
    assert s.connect_token_file.read_text() == old  # previous sign-in put back
    assert not list(s.secrets_dir.glob("garminconnect.new*"))
    assert not (s.secrets_dir / "garminconnect.old").exists()


def test_reset_password(env, monkeypatch):
    from app import auth
    s, db = env
    assert auth.create_account(s, "adam", "first password here")
    pw = iter(["short", "long enough password", "mismatch-mismatch", "long enough password", "long enough password"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(pw))
    monkeypatch.setattr(builtins, "input", lambda prompt="": "")  # keep the username
    setup.cmd_reset_password(s)
    assert auth.check_login(s, "adam", "long enough password")
    assert not auth.check_login(s, "adam", "first password here")
