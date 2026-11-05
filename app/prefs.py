"""User preferences edited in the dashboard's Settings tab (stored in the database).

Nothing secret lives here: keys, passwords and sign-in tokens are separate 0600 files.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Database

RACE_DISTANCES_KM = {"5k": 5.0, "10k": 10.0, "half": 21.0975, "marathon": 42.195}

DEFAULTS: dict[str, Any] = {
    "units": "imperial",
    "timezone": "America/Chicago",
    "theme": "auto",
    "race_name": "",
    "race_date": "",
    "race_distance": "half",
    "race_custom_km": None,
    "race_goal_time": "",
    "include_cycling": True,
    "ai_enabled": False,
    "ollama_url": "",
    "ollama_model": "",
}

_GOAL_RE = re.compile(r"^(\d{1,2}):([0-5]\d):([0-5]\d)$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,100}$")


class PrefError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def _env_defaults() -> dict[str, Any]:
    """Older installs set these as container variables; use them as starting values."""
    out: dict[str, Any] = {}
    units = os.environ.get("UNITS", "").strip().lower()
    if units in ("imperial", "metric"):
        out["units"] = units
    tz = os.environ.get("TZ", "").strip()
    if tz:
        out["timezone"] = tz
    for env, key in (("RACE_NAME", "race_name"), ("RACE_DATE", "race_date")):
        v = os.environ.get(env, "").strip().strip("'\"")
        if v:
            out[key] = v
    return out


def get_all(db: Database) -> dict[str, Any]:
    prefs = {**DEFAULTS, **_env_defaults()}
    raw = db.get_meta("prefs")
    if raw:
        try:
            stored = json.loads(raw)
            if isinstance(stored, dict):
                prefs.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except ValueError:
            pass
    return prefs


def goal_seconds(value: str) -> int | None:
    m = _GOAL_RE.match(value or "")
    if not m:
        return None
    h, mi, s = (int(x) for x in m.groups())
    total = h * 3600 + mi * 60 + s
    return total or None


def race_km(prefs: dict) -> float | None:
    if prefs.get("race_distance") == "custom":
        return prefs.get("race_custom_km") or None
    return RACE_DISTANCES_KM.get(prefs.get("race_distance"))


def validate(changes: dict[str, Any]) -> dict[str, Any]:
    """Return cleaned values for known keys; raise PrefError on the first bad one."""
    out: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise PrefError(key, "Unknown setting.")
        if key == "units":
            if value not in ("imperial", "metric"):
                raise PrefError(key, "Choose miles or kilometers.")
        elif key == "timezone":
            try:
                ZoneInfo(str(value))
            except (ZoneInfoNotFoundError, ValueError):
                raise PrefError(key, "Unknown time zone. Use a name like America/Chicago.") from None
        elif key == "theme":
            if value not in ("auto", "light", "dark"):
                raise PrefError(key, "Choose auto, light or dark.")
        elif key == "race_name":
            value = str(value or "").strip()
            if len(value) > 80:
                raise PrefError(key, "Keep the race name under 80 characters.")
        elif key == "race_date":
            value = str(value or "").strip()
            if value:
                try:
                    from datetime import date
                    value = date.fromisoformat(value).isoformat()
                except ValueError:
                    raise PrefError(key, "Use a date like 2026-11-14.") from None
        elif key == "race_distance":
            if value not in (*RACE_DISTANCES_KM, "custom"):
                raise PrefError(key, "Choose a race distance.")
        elif key == "race_custom_km":
            if value in (None, ""):
                value = None
            else:
                try:
                    value = round(float(value), 3)
                except (TypeError, ValueError):
                    raise PrefError(key, "Enter the distance in kilometers.") from None
                if not 0.4 <= value <= 500:
                    raise PrefError(key, "Distance must be between 0.4 and 500 km.")
        elif key == "race_goal_time":
            value = str(value or "").strip()
            if value and goal_seconds(value) is None:
                raise PrefError(key, "Use hours:minutes:seconds, like 2:05:00.")
        elif key in ("include_cycling", "ai_enabled"):
            if not isinstance(value, bool):
                raise PrefError(key, "Must be on or off.")
        elif key == "ollama_url":
            value = str(value or "").strip().rstrip("/")
            if value:
                parts = urlsplit(value)
                if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password \
                        or parts.query or parts.fragment or len(value) > 200:
                    raise PrefError(key, "Use an address like http://192.168.4.23:11434")
        elif key == "ollama_model":
            value = str(value or "").strip()
            if value and not _MODEL_RE.match(value):
                raise PrefError(key, "Model names look like llama3.1:8b")
        out[key] = value
    return out


def update(db: Database, changes: dict[str, Any]) -> dict[str, Any]:
    cleaned = validate(changes)
    prefs = get_all(db)
    prefs.update(cleaned)
    db.set_meta("prefs", json.dumps(prefs))
    if "timezone" in cleaned:
        apply_timezone(prefs["timezone"])
    return prefs


def apply_timezone(tz: str) -> None:
    """Make "today" follow the chosen time zone for the whole app."""
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return
    os.environ["TZ"] = tz
    time.tzset()
