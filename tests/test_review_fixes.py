"""Regression tests for the issues found in the independent review."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import auth, main, sync
from app.db import Database

from tests.helpers import ORIGIN, PASSWORD, USER, make_account, reset


@pytest.fixture()
def client():
    reset()
    make_account()
    return TestClient(main.app, follow_redirects=False)


def test_limiter_reservation_is_atomic():
    lim = auth.LoginLimiter(per_ip=5, total=20)
    barrier = threading.Barrier(50)

    def go(_):
        barrier.wait()
        return lim.try_begin("10.0.0.9")

    with ThreadPoolExecutor(50) as pool:
        got = [r for r in pool.map(go, range(50)) if r is not None]
    assert len(got) == 5


def test_global_limit_across_many_ips():
    lim = auth.LoginLimiter(per_ip=5, total=20)
    allowed = sum(lim.try_begin(f"10.0.{i}.1") is not None for i in range(100))
    assert allowed == 20


def test_wrong_logins_in_waves_still_capped_at_five(client, monkeypatch):
    """Waves small enough that the hashing cap never kicks in: only the limiter can stop them."""
    hashed = []
    real = auth.check_login
    monkeypatch.setattr(auth, "check_login", lambda st, u, pw: (hashed.append(1), real(st, u, pw))[1])

    def attempt(_):
        c = TestClient(main.app, follow_redirects=False)
        return c.post("/login", data={"username": USER, "password": "wrong"}, headers=ORIGIN).headers["location"]

    results = []
    for _ in range(10):
        with ThreadPoolExecutor(2) as pool:
            results += list(pool.map(attempt, range(2)))
    assert len(hashed) == 5 and results.count("/login?m=bad") == 5


def test_burst_of_wrong_logins(client):
    def attempt(_):
        c = TestClient(main.app, follow_redirects=False)
        return c.post("/login", data={"username": USER, "password": "wrong"}, headers=ORIGIN).headers["location"]

    with ThreadPoolExecutor(30) as pool:
        results = list(pool.map(attempt, range(30)))
    assert results.count("/login?m=bad") <= 5
    assert all(r in ("/login?m=bad", "/login?m=locked", "/login?m=busy") for r in results)


def test_hashing_concurrency_capped(client, monkeypatch):
    running, peak, lock = [0], [0], threading.Lock()
    real = auth.check_login

    def slow(settings, u, pw):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.2)
        with lock:
            running[0] -= 1
        return real(settings, u, pw)

    monkeypatch.setattr(auth, "check_login", slow)
    main.limiter = auth.LoginLimiter(per_ip=100, total=100)
    with ThreadPoolExecutor(12) as pool:
        list(pool.map(lambda _: TestClient(main.app, follow_redirects=False).post(
            "/login", data={"username": USER, "password": "x"}, headers=ORIGIN), range(12)))
    assert peak[0] <= 2


def test_successful_login_does_not_use_up_the_limit(client):
    for _ in range(25):
        c = TestClient(main.app, follow_redirects=False)
        assert c.post("/login", data={"username": USER, "password": PASSWORD}, headers=ORIGIN).headers["location"] == "/"


@pytest.mark.parametrize("host,ok", [
    ("testserver", True), ("192.168.1.50:8099", True), ("127.0.0.1", True), ("[::1]:8099", True),
    ("localhost:8099", True), ("garmin.example.net", True), ("testserver.", True), ("TESTSERVER", True),
    ("evil.example.com", False), ("evil.example.com:8099", False), ("", False),
])
def test_host_allowlist(client, host, ok):
    r = client.get("/login", headers={"Host": host})
    assert (r.status_code != 400) == ok, (host, r.status_code)


def test_reverse_proxy_origin_allowed_only_if_listed(client):
    # proxy rewrites Host to the container IP; browser Origin is the proxy's public address
    r = client.post("/login", data={"username": USER, "password": PASSWORD},
                    headers={"Host": "10.0.0.5:8099", "Origin": "https://garmin.example.net"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    for bad in ("https://other.example.net", "http://garmin.example.net", "https://garmin.example.net:8443", "null"):
        r = client.post("/login", data={"username": USER, "password": PASSWORD}, headers={"Host": "10.0.0.5:8099", "Origin": bad})
        assert r.status_code == 403, bad


def test_other_app_on_same_server_cannot_post(client):
    # another web UI on the same host but a different port must not count as same-origin
    r = client.post("/logout", headers={"Host": "testserver", "Origin": "http://testserver:8080"})
    assert r.status_code == 403


def test_busy_attempts_are_refunded():
    lim = auth.LoginLimiter(per_ip=5, total=20)
    stamps = [lim.try_begin("1.1.1.1") for _ in range(5)]
    assert lim.try_begin("1.1.1.1") is None
    lim.release(stamps[0])
    assert lim.try_begin("1.1.1.1") is not None
    lim.release(12345.0)  # unknown stamp: no error


def test_static_files_revalidate(client):
    assert client.get("/static/style.css").headers["cache-control"] == "no-cache"


def test_sync_refuses_while_another_process_holds_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    with sync.data_lock(main.settings) as held:
        assert held
        assert sync.is_running(main.settings)
        assert sync.run_sync(main.settings, main.db)["status"] == "busy"
    assert not sync.is_running(main.settings)


def test_status_peek_does_not_block_a_real_sync(monkeypatch):
    stop = threading.Event()

    def peek():
        while not stop.is_set():
            sync.is_running(main.settings)

    threads = [threading.Thread(target=peek) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        results = [sync.run_sync(main.settings, main.db)["status"] for _ in range(20)]
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert "busy" not in results  # (nothing configured, so each run reports "setup")


def test_db_failure_releases_lock_and_records_nothing_stuck(monkeypatch):
    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(main.db, "log_start", boom)
    r = sync.run_sync(main.settings, main.db)
    assert r["status"] == "partial" and "database is locked" in r["message"]
    assert not sync.is_running(main.settings)
    monkeypatch.undo()
    assert sync.run_sync(main.settings, main.db)["status"] == "setup"  # works again


def test_failed_backfill_is_retried(tmp_path):
    db = Database(tmp_path / "t.db")

    class FakeClient:
        def get_user_profile_settings(self): return {}
        def search_activities(self, **kw): return []
        def get_daily_summaries(self, a, b): return []
        def get_daily_steps(self, a, b): return []
        def get_daily_stress(self, a, b): raise RuntimeError("HTTP 503 after retries")
        def get_daily_sleep_summaries(self, a, b): return []
        def get_daily_sleep(self, d): return {}
        def get_daily_hrv_summaries(self, a, b): return []
        def get_daily_body_battery(self, a, b): return []

    g = sync.GarminSync.__new__(sync.GarminSync)
    g.s, g.db, g.client, g.notes, g.api_key = main.settings, db, FakeClient(), [], "k"
    g.call = lambda fn, *a, **kw: fn(*a, **kw)
    msg = g.run()
    assert "stress: FAILED" in msg
    assert db.get_meta("backfilled") is None  # will re-pull the full history next time


def test_first_run_delay_skips_recent_sync():
    log_id = main.db.log_start()
    main.db.log_finish(log_id, "ok", "fine")
    assert main._first_run_delay() > main.settings.sync_interval_hours * 3600 - 60
    log_id = main.db.log_start()
    main.db.log_finish(log_id, "error", "nope")
    assert main._first_run_delay() == 0
