"""Pull data from the Garmin Connect Personal API into SQLite.

Run once by hand:   python -m app.sync            (normal incremental sync)
                    python -m app.sync --full     (re-pull the whole backfill window)
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Callable, Iterator

from garmin_personal_api import GarminPersonalApiClient, HttpError, PersonalApiAuthenticator

from . import config, extract
from .db import Database

log = logging.getLogger("garmin.sync")
_lock = threading.Lock()


class EndpointUnavailable(Exception):
    pass


class GarminSync:
    CHUNK_DAYS = 28

    def __init__(self, settings: config.Settings, db: Database):
        self.api_key = settings.api_key()
        if not self.api_key:
            raise RuntimeError("No Garmin Personal API key saved. Run: docker exec -it garmin-dashboard python -m app.setup")
        self.s = settings
        self.db = db
        self.auth = PersonalApiAuthenticator(bearer_token_cache_file=settings.token_cache)
        try:
            token = self.auth.obtain_token(self.api_key)
        except HttpError as e:
            if e.status_code in (401, 403):
                raise RuntimeError("Garmin rejected the Personal API key (expired or deleted). Create a new one "
                                   "and run: docker exec -it garmin-dashboard python -m app.setup api-key") from e
            raise
        self.client = GarminPersonalApiClient(token.access_token)
        self.notes: list[str] = []

    def close(self) -> None:
        self.client.close()
        self.auth.close()

    # ------------------------------------------------------------ plumbing
    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        refreshed = False
        for attempt in range(5):
            try:
                result = fn(*args, **kwargs)
                time.sleep(self.s.request_delay)  # be polite to Garmin
                return result
            except HttpError as e:
                if e.status_code == 401 and not refreshed:
                    log.info("Bearer token rejected; fetching a new one")
                    token = self.auth.obtain_token(self.api_key, force_refresh=True)
                    self.client.update_bearer_token(token.access_token)
                    refreshed = True
                    continue
                if e.status_code == 429 or e.status_code >= 500:
                    wait = 30 * (2 ** attempt)
                    log.warning("Garmin returned %s; waiting %ss", e.status_code, wait)
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError("Gave up after repeated rate-limit/server errors")

    def fetch_range(self, name: str, fn: Callable, parser: Callable, start: date, end: date) -> int:
        """Fetch [start, end] in chunks; split a chunk in half if Garmin rejects its size."""
        stored = 0

        def run(a: date, b: date) -> None:
            nonlocal stored
            try:
                body = self.call(fn, a.isoformat(), b.isoformat())
            except HttpError as e:
                if e.status_code in (400, 413, 422) and (b - a).days >= 1:
                    mid = a + (b - a) // 2
                    run(a, mid)
                    run(mid + timedelta(days=1), b)
                    return
                if e.status_code in (403, 404):
                    raise EndpointUnavailable(f"{name}: HTTP {e.status_code}") from e
                raise
            self.db.save_raw(name, f"{a}_{b}", body)
            for day, row in parser(body):
                if row:
                    self.db.upsert_daily(day, row)
                    stored += 1

        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=self.CHUNK_DAYS - 1))
            run(cursor, chunk_end)
            cursor = chunk_end + timedelta(days=1)
        return stored

    # ------------------------------------------------------------ datasets
    def sync_daily(self, start: date, end: date) -> None:
        c = self.client
        datasets = [
            ("daily_summaries", c.get_daily_summaries, extract.daily_summaries),
            ("steps", c.get_daily_steps, extract.steps),
            ("stress", c.get_daily_stress, extract.stress),
            ("sleep", c.get_daily_sleep_summaries, extract.sleep),
            ("hrv", c.get_daily_hrv_summaries, extract.hrv),
            ("body_battery", c.get_daily_body_battery, extract.body_battery),
        ]
        for name, fn, parser in datasets:
            try:
                n = self.fetch_range(name, fn, parser, start, end)
                self.notes.append(f"{name}: {n} days")
                log.info("%s: stored %d day records", name, n)
                if name == "sleep" and n == 0:
                    self.sleep_per_day(max(start, end - timedelta(days=13)), end)
            except Exception as e:  # keep going; one dataset failing shouldn't stop the rest
                log.exception("%s failed", name)
                self.notes.append(f"{name}: FAILED ({e})")

    def sleep_per_day(self, start: date, end: date) -> None:
        """Fallback when the range endpoint returns nothing usable."""
        n = 0
        day = start
        while day <= end:
            try:
                body = self.call(self.client.get_daily_sleep, day.isoformat())
                self.db.save_raw("sleep_daily", day.isoformat(), body)
                for d, row in extract.sleep(body):
                    if row:
                        self.db.upsert_daily(d, row)
                        n += 1
            except HttpError as e:
                log.warning("sleep %s: HTTP %s", day, e.status_code)
            day += timedelta(days=1)
        self.notes.append(f"sleep (per-day fallback): {n} days")

    def sync_activities(self, start: date, end: date) -> None:
        page, limit, stored = 0, 50, 0
        while True:
            body = self.call(
                self.client.search_activities,
                start=page * limit, limit=limit,
                start_date=start.isoformat(), end_date=end.isoformat(),
            )
            items = extract.activity_list(body)
            self.db.save_raw("activities", f"{start}_{end}_p{page}", body)
            for a in items:
                aid, row = extract.activity(a)
                if aid:
                    self.db.upsert_activity(aid, row, a)
                    stored += 1
            if len(items) < limit or page > 200:
                break
            page += 1
        self.notes.append(f"activities: {stored}")
        log.info("activities: stored %d", stored)

    def run(self, full: bool = False) -> str:
        today = date.today()
        first_run = self.db.get_meta("backfilled") is None
        days = self.s.backfill_days if (full or first_run) else self.s.refresh_days
        start = today - timedelta(days=days - 1)
        log.info("Syncing %s → %s (%s)", start, today, "backfill" if days > self.s.refresh_days else "incremental")

        try:
            self.db.save_raw("profile_settings", "latest", self.call(self.client.get_user_profile_settings))
        except Exception as e:
            log.warning("profile settings: %s", e)

        self.sync_daily(start, today)
        try:
            act_start = start
            latest = self.db.latest_activity_date()
            if latest and not (full or first_run):
                act_start = min(start, date.fromisoformat(latest) - timedelta(days=2))
            self.sync_activities(act_start, today)
        except Exception as e:
            log.exception("activities failed")
            self.notes.append(f"activities: FAILED ({e})")

        # Only mark the history as fetched if every part succeeded; otherwise retry next time.
        if (first_run or full) and not any("FAILED" in n for n in self.notes):
            self.db.set_meta("backfilled", today.isoformat())
        return "; ".join(self.notes)


def _lock_path(settings: config.Settings) -> str:
    # In /tmp (a tmpfs shared by the server and `docker exec` commands), keyed by data folder.
    key = hashlib.sha256(str(settings.data_dir.resolve()).encode()).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"garmin-dashboard-{key}.lock")


@contextmanager
def data_lock(settings: config.Settings, blocking: bool = False) -> Iterator[bool]:
    """Cross-process lock so the scheduler, `app.sync` and `app.setup connect`
    never touch Garmin tokens or the database at the same time. Yields True if held."""
    fd = os.open(_lock_path(settings), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def run_sync(settings: config.Settings, db: Database, full: bool = False) -> dict:
    """Entry point used by the scheduler, the Sync button and the CLI.

    The official Personal API and the optional Garmin Connect sync run
    independently: one failing never stops the other.
    """
    if not _lock.acquire(blocking=False):
        return {"status": "busy", "message": "A sync is already running"}
    try:
        # is_running() peeks at the lock for a few microseconds; retry briefly so a
        # status check can never make a real sync think another one is running.
        for attempt in range(10):
            with data_lock(settings) as held:
                if held:
                    return _run_sync_locked(settings, db, full)
            time.sleep(0.3)
        return {"status": "busy", "message": "A sync is already running"}
    finally:
        _lock.release()


def _run_sync_locked(settings: config.Settings, db: Database, full: bool) -> dict:
    log_id = None
    parts: list[str] = []
    failed = 0
    attempted = 0
    try:
        log_id = db.log_start()
        if settings.api_key():
            attempted += 1
            try:
                syncer = GarminSync(settings, db)
                try:
                    msg = syncer.run(full=full)
                finally:
                    syncer.close()
                parts.append(msg)
                failed += "FAILED" in msg
            except Exception as e:
                log.exception("Personal API sync failed")
                parts.append(f"Personal API: FAILED ({e})")
                failed += 1
        if settings.connect_configured():
            attempted += 1
            try:
                from .connect import ConnectSync

                msg = ConnectSync(settings, db).run(full=full)
                parts.append(msg)
                failed += "FAILED" in msg
            except Exception as e:
                log.exception("Garmin Connect sync failed")
                parts.append(f"Connect: FAILED ({e})")
                failed += 1
        if not attempted:
            parts.append("Nothing to sync yet. Run: docker exec -it garmin-dashboard python -m app.setup")
    except Exception as e:  # e.g. the database itself is unavailable
        log.exception("Sync aborted")
        parts.append(f"Sync aborted: {type(e).__name__}: {e}")
        failed += 1
        attempted = max(attempted, 1)
    if attempted == 0:
        status = "setup"
    elif failed == 0:
        status = "ok"
    else:
        status = "partial"  # something failed; the message says what
    message = " | ".join(parts)
    if log_id is not None:
        try:
            db.log_finish(log_id, status, message)
        except Exception:
            log.exception("Couldn't record the sync result")
    return {"status": status, "message": message}


def is_running(settings: config.Settings | None = None) -> bool:
    if _lock.locked():
        return True
    if settings is None:
        return False
    with data_lock(settings) as held:  # held by another process (e.g. a manual `app.sync`)?
        return not held


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Sync Garmin data into the dashboard database")
    parser.add_argument("--full", action="store_true", help="re-pull the full backfill window")
    args = parser.parse_args()
    config.drop_root("app.sync")
    settings = config.load()
    result = run_sync(settings, Database(settings.db_path), full=args.full)
    print(f"{result['status']}: {result['message']}")


if __name__ == "__main__":
    main()
