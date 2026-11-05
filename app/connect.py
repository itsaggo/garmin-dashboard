"""Optional sync of the metrics the Personal API leaves out (VO2 max, training
status, training readiness, recovery time) via the community `garminconnect`
library, which uses the same internal endpoints as the Garmin Connect website.

This module only ever *reads*. It authenticates with saved tokens created by
`python -m app.setup connect`; your Garmin password is never stored.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any, Callable

from . import extract
from .config import Settings
from .db import Database

log = logging.getLogger("garmin.connect")


class ConnectAuthExpired(Exception):
    pass


class ConnectSync:
    CHUNK_DAYS = 28

    def __init__(self, settings: Settings, db: Database):
        from garminconnect import Garmin, GarminConnectAuthenticationError

        self.s = settings
        self.db = db
        self.notes: list[str] = []
        self.client = Garmin(retry_attempts=2)
        try:
            # Loads saved tokens and refreshes them if needed (refreshed tokens are re-saved, 0600).
            self.client.login(tokenstore=str(settings.connect_token_dir))
        except GarminConnectAuthenticationError as e:
            raise ConnectAuthExpired(
                "Garmin Connect sign-in expired or was revoked. Run: "
                "docker exec -it garmin-dashboard python -m app.setup connect"
            ) from e

    def call(self, fn: Callable[..., Any], *args: Any) -> Any:
        from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

        for attempt in range(4):
            try:
                result = fn(*args)
                time.sleep(1.0)  # unofficial access: stay well below anything that looks abusive
                return result
            except GarminConnectTooManyRequestsError:
                if attempt == 3:
                    raise
                wait = 60 * (2 ** attempt)
                log.warning("Garmin Connect rate limit; waiting %ss", wait)
                time.sleep(wait)
            except GarminConnectAuthenticationError as e:
                raise ConnectAuthExpired(
                    "Garmin Connect rejected the saved sign-in. Run: "
                    "docker exec -it garmin-dashboard python -m app.setup connect"
                ) from e
        raise RuntimeError("unreachable")

    def fetch_range(self, name: str, fn: Callable, parser: Callable, start: date, end: date, table: str = "training") -> int:
        from garminconnect import GarminConnectConnectionError, GarminConnectNotFoundError

        stored = 0

        def run(a: date, b: date) -> None:
            nonlocal stored
            try:
                body = self.call(fn, a.isoformat(), b.isoformat())
            except GarminConnectNotFoundError:
                return  # no data for this window
            except GarminConnectConnectionError:
                if (b - a).days >= 1:  # range may be too long for this endpoint: split it
                    mid = a + (b - a) // 2
                    run(a, mid)
                    run(mid + timedelta(days=1), b)
                    return
                raise
            self.db.save_raw(f"connect_{name}", f"{a}_{b}", body)
            for day, row in parser(body):
                if row:
                    self.db.upsert_day(table, day, row)
                    stored += 1

        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=self.CHUNK_DAYS - 1))
            run(cursor, chunk_end)
            cursor = chunk_end + timedelta(days=1)
        return stored

    def sync_training_status(self, start: date, end: date) -> int:
        """Training status only has a per-day endpoint; one request per day."""
        from garminconnect import GarminConnectConnectionError

        stored, day = 0, start
        while day <= end:
            try:
                body = self.call(self.client.get_training_status, day.isoformat())
            except GarminConnectConnectionError as e:  # includes 404 "no data"
                log.info("training status %s: %s", day, type(e).__name__)
                body = None
            if body:
                self.db.save_raw("connect_training_status", day.isoformat(), body)
                for d, row in extract.training_status(body):
                    if row:
                        self.db.upsert_training(d, row)
                        stored += 1
            day += timedelta(days=1)
        return stored

    def run(self, full: bool = False) -> str:
        today = date.today()
        first_run = self.db.get_meta("connect_backfilled") is None
        days = self.s.connect_backfill_days if (full or first_run) else 3
        start = today - timedelta(days=days - 1)
        log.info("Garmin Connect sync %s → %s", start, today)

        def predictions(a: str, b: str):
            return self.client.get_race_predictions(a, b, "daily")

        def profile_job() -> int:
            from .sync import save_profile
            found = extract.profile(self.call(self.client.get_userprofile_settings))
            save_profile(self.db, found)
            return len(found)

        jobs = [
            ("vo2max", lambda: self.fetch_range("vo2max", self.client.get_max_metrics_range, extract.vo2max, start, today)),
            ("race predictions", lambda: self.fetch_range("race_predictions", predictions, extract.race_predictions, start, today)),
            ("resting HR", lambda: self.fetch_range("rhr", self.client.get_rhr_daily, extract.rhr_daily, start, today, table="daily")),
            ("profile", profile_job),
            ("readiness", lambda: self.fetch_range("readiness", self.client.get_training_readiness_range, extract.readiness, start, today)),
            ("training status", lambda: self.sync_training_status(start, today)),
        ]
        for name, job in jobs:
            try:
                n = job()
                self.notes.append(f"{name}: {n}")
            except ConnectAuthExpired:
                raise
            except Exception as e:
                log.exception("connect %s failed", name)
                self.notes.append(f"{name}: FAILED ({type(e).__name__})")
        if (first_run or full) and not any("FAILED" in n for n in self.notes):
            self.db.set_meta("connect_backfilled", today.isoformat())
        return "Connect — " + ", ".join(self.notes)
