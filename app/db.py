"""SQLite storage: raw API responses plus tidy tables the dashboard reads."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

TEXT_COLUMNS = {
    "hrv_status", "start_local", "name", "type", "training_status", "acwr_status",
    "load_balance", "readiness_level", "readiness_feedback", "readiness_time",
}

DAILY_COLUMNS = [
    "steps", "step_goal", "distance_m", "floors",
    "resting_hr", "min_hr", "max_hr",
    "avg_stress", "max_stress",
    "bb_high", "bb_low", "bb_charged", "bb_drained",
    "sleep_s", "deep_s", "light_s", "rem_s", "awake_s", "sleep_score",
    "hrv_last_night", "hrv_weekly", "hrv_status",
    "calories_total", "calories_active",
    "intensity_moderate", "intensity_vigorous",
    "spo2_avg", "respiration_avg",
]

# Metrics only available through the unofficial Garmin Connect sync.
TRAINING_COLUMNS = [
    "vo2max", "vo2max_cycling", "fitness_age",
    "training_status", "acute_load", "chronic_load", "acwr", "acwr_status", "load_balance",
    "readiness_score", "readiness_level", "readiness_feedback",
    "recovery_min", "readiness_time",
]

ACTIVITY_COLUMNS = [
    "start_local", "name", "type", "distance_m", "duration_s", "moving_s",
    "avg_hr", "max_hr", "calories", "elev_gain_m", "avg_speed_ms",
    "avg_cadence", "training_load", "aerobic_te",
]


def _cols(columns: list[str]) -> str:
    return ", ".join(f"{c} {'TEXT' if c in TEXT_COLUMNS else 'REAL'}" for c in columns)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS raw (
    endpoint   TEXT NOT NULL,
    key        TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    body       TEXT NOT NULL,
    PRIMARY KEY (endpoint, key)
);
CREATE TABLE IF NOT EXISTS daily (date TEXT PRIMARY KEY, {_cols(DAILY_COLUMNS)});
CREATE TABLE IF NOT EXISTS training (date TEXT PRIMARY KEY, {_cols(TRAINING_COLUMNS)});
CREATE TABLE IF NOT EXISTS activities (id INTEGER PRIMARY KEY, {_cols(ACTIVITY_COLUMNS)}, body TEXT);
CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_local);
CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
"""

_TABLE_COLUMNS = {"daily": DAILY_COLUMNS, "training": TRAINING_COLUMNS}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # Default rollback journal (not WAL): safe on Unraid's /mnt/user FUSE filesystem.
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ---- raw -------------------------------------------------------------
    def save_raw(self, endpoint: str, key: str, body: Any) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO raw(endpoint, key, fetched_at, body) VALUES (?,?,?,?)",
                (endpoint, key, now_iso(), json.dumps(body)),
            )

    def raw_summary(self) -> list[dict]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT endpoint, COUNT(*) n, MAX(fetched_at) last FROM raw GROUP BY endpoint ORDER BY endpoint"
            ).fetchall()
        return [dict(r) for r in rows]

    def raw_latest(self, endpoint: str) -> Any:
        with self.connect() as con:
            row = con.execute(
                "SELECT key, fetched_at, body FROM raw WHERE endpoint=? ORDER BY fetched_at DESC, key DESC LIMIT 1",
                (endpoint,),
            ).fetchone()
        if not row:
            return None
        return {"key": row["key"], "fetched_at": row["fetched_at"], "body": json.loads(row["body"])}

    # ---- per-day tables ----------------------------------------------------
    def upsert_day(self, table: str, date: str, values: dict[str, Any]) -> None:
        allowed = _TABLE_COLUMNS[table]  # KeyError for anything else: table name is never user input
        values = {k: v for k, v in values.items() if k in allowed and v is not None}
        if not values:
            return
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        # New non-null values win; existing values survive when a source omits them.
        updates = ", ".join(f"{c}=COALESCE(excluded.{c}, {table}.{c})" for c in values)
        with self.connect() as con:
            con.execute(
                f"INSERT INTO {table}(date, {cols}) VALUES (?, {marks}) "
                f"ON CONFLICT(date) DO UPDATE SET {updates}",
                (date, *values.values()),
            )

    def upsert_daily(self, date: str, values: dict[str, Any]) -> None:
        self.upsert_day("daily", date, values)

    def upsert_training(self, date: str, values: dict[str, Any]) -> None:
        self.upsert_day("training", date, values)

    def rows_since(self, table: str, since: str) -> list[dict]:
        if table not in _TABLE_COLUMNS:
            raise ValueError(table)
        with self.connect() as con:
            rows = con.execute(f"SELECT * FROM {table} WHERE date >= ? ORDER BY date", (since,)).fetchall()
        return [dict(r) for r in rows]

    def count(self, table: str) -> int:
        if table not in ("daily", "training", "activities"):
            raise ValueError(table)
        with self.connect() as con:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # ---- activities ------------------------------------------------------
    def upsert_activity(self, activity_id: int, values: dict[str, Any], body: Any) -> None:
        with self.connect() as con:
            con.execute(
                f"INSERT OR REPLACE INTO activities(id, {', '.join(ACTIVITY_COLUMNS)}, body) "
                f"VALUES (?, {', '.join('?' for _ in ACTIVITY_COLUMNS)}, ?)",
                (activity_id, *(values.get(c) for c in ACTIVITY_COLUMNS), json.dumps(body)),
            )

    def activities(self, limit: int = 50) -> list[dict]:
        with self.connect() as con:
            rows = con.execute(
                f"SELECT id, {', '.join(ACTIVITY_COLUMNS)} FROM activities ORDER BY start_local DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_activity_date(self) -> str | None:
        with self.connect() as con:
            row = con.execute("SELECT MAX(start_local) FROM activities").fetchone()
        return row[0][:10] if row and row[0] else None

    def weekly(self, since: str, type_like: str = "%run%") -> list[dict]:
        """Totals per week (weeks start Monday) for activities whose type matches."""
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT date(substr(start_local,1,10),
                            '-' || ((CAST(strftime('%w', substr(start_local,1,10)) AS INTEGER) + 6) % 7) || ' days') AS week,
                       COUNT(*) AS count,
                       SUM(COALESCE(distance_m,0)) AS distance_m,
                       SUM(COALESCE(duration_s,0)) AS duration_s
                FROM activities
                WHERE substr(start_local,1,10) >= ? AND lower(type) LIKE ?
                GROUP BY week ORDER BY week
                """,
                (since, type_like),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- sync log / meta ---------------------------------------------------
    def log_start(self) -> int:
        with self.connect() as con:
            cur = con.execute("INSERT INTO sync_log(started_at, status) VALUES (?, 'running')", (now_iso(),))
            return cur.lastrowid

    def log_finish(self, log_id: int, status: str, message: str) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE sync_log SET finished_at=?, status=?, message=? WHERE id=?",
                (now_iso(), status, message[:4000], log_id),
            )
            # Keep the log from growing forever.
            con.execute("DELETE FROM sync_log WHERE id < ?", (log_id - 500,))

    def last_sync(self) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def mark_interrupted_syncs(self) -> None:
        """A sync still 'running' at startup was cut off by a restart."""
        with self.connect() as con:
            con.execute(
                "UPDATE sync_log SET status='error', message='Interrupted (container restarted)', finished_at=? "
                "WHERE status='running'",
                (now_iso(),),
            )

    def set_meta(self, k: str, v: str) -> None:
        with self.connect() as con:
            con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?,?)", (k, v))

    def get_meta(self, k: str) -> str | None:
        with self.connect() as con:
            row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def delete_meta(self, k: str) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM meta WHERE k=?", (k,))

    # ---- sessions ----------------------------------------------------------
    def session_add(self, token_hash: str, created: int, expires: int) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM sessions WHERE expires_at < ?", (created,))
            con.execute("INSERT INTO sessions VALUES (?,?,?)", (token_hash, created, expires))

    def session_valid(self, token_hash: str, now: int) -> bool:
        with self.connect() as con:
            row = con.execute("SELECT expires_at FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
        return bool(row and row[0] > now)

    def session_delete(self, token_hash: str) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def session_delete_all(self) -> int:
        with self.connect() as con:
            return con.execute("DELETE FROM sessions").rowcount
