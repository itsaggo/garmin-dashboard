"""User preferences edited in the dashboard's Settings tab (stored in the database).

Nothing secret lives here: keys, passwords and sign-in tokens are separate 0600 files.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Database

RACE_DISTANCES_KM = {"5k": 5.0, "10k": 10.0, "half": 21.0975, "marathon": 42.195}

# Dashboard boxes: id -> (title, default section, default width of 12, default height in rows)
WIDGETS: dict[str, tuple[str, str, int, int]] = {
    # Main page reads like a front page: the coach's note as the lead, today's weather and
    # heat-graded paces beside it, the race strip, then the four key numbers and the week in brief.
    "coach": ("AI coach", "main", 8, 8),
    "weather": ("Weather & heat-graded pace", "main", 4, 8),
    "race": ("Goal race", "main", 12, 2),
    "vo2": ("VO2 max", "main", 3, 4),
    "readiness": ("Training readiness", "main", 3, 4),
    "sleep": ("Sleep score", "main", 3, 4),
    "five_k": ("5K estimate", "main", 3, 4),
    "weekly_run": ("Weekly running", "main", 4, 4),
    "hrv": ("HRV", "main", 4, 4),
    "sleep_stages": ("Sleep stages", "main", 4, 4),
    "activities": ("Recent activities", "main", 12, 6),
    "rhr": ("Resting heart rate", "details", 6, 4),
    "body_battery": ("Body Battery", "details", 6, 4),
    "steps": ("Steps", "details", 6, 4),
    "stress": ("Stress", "details", 6, 4),
    "training_load": ("Training load", "details", 6, 4),
    "weekly_ride": ("Weekly cycling", "details", 6, 4),
}
SECTIONS = ("main", "details", "hidden")


def default_layout() -> list[dict]:
    return [{"id": k, "section": sec, "w": w, "h": h} for k, (_, sec, w, h) in WIDGETS.items()]


def complete_layout(items: list[dict] | None) -> list[dict]:
    """Saved layout plus any boxes added in newer versions (in their default spots)."""
    items = [dict(i) for i in (items or []) if isinstance(i, dict)]
    if not any(i.get("id") == "weather" for i in items):
        for i in items:  # v1.3: weather takes the fitness score's place and size
            if i.get("id") == "fitness":
                i["id"] = "weather"
                break
    items = [i for i in items if i.get("id") in WIDGETS]
    seen = {i["id"] for i in items}
    return items + [i for i in default_layout() if i["id"] not in seen]


DEFAULTS: dict[str, Any] = {
    "units": "imperial",
    "timezone": "America/Chicago",
    "theme": "auto",
    "race_enabled": False,
    "race_name": "",
    "race_date": "",
    "race_distance": "half",
    "race_custom_km": None,
    "race_goal_time": "",
    "include_cycling": True,
    "weather_place": "Houston, Texas, United States",
    "weather_lat": 29.76,
    "weather_lon": -95.37,
    "ai_provider": "none",
    "ai_model": "",
    "ollama_url": "",
    "layout": None,
    "notify_morning": True,
    "notify_weekly": True,
    "notify_problems": True,
}
AI_PROVIDERS = ("none", "claude", "gemini", "openai", "ollama")

_GOAL_RE = re.compile(r"^(\d{1,2}):([0-5]\d):([0-5]\d)$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")


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
    stored: dict = {}
    if raw:
        try:
            stored = json.loads(raw)
            if not isinstance(stored, dict):
                stored = {}
        except ValueError:
            stored = {}
    prefs.update({k: v for k, v in stored.items() if k in DEFAULTS})
    # v1.1 → v1.2 migrations
    if "race_enabled" not in stored and prefs.get("race_date"):
        prefs["race_enabled"] = True
    if "ai_provider" not in stored and stored.get("ai_enabled") and stored.get("ollama_url") and stored.get("ollama_model"):
        prefs["ai_provider"], prefs["ai_model"] = "ollama", stored["ollama_model"]
    prefs["layout"] = complete_layout(prefs.get("layout"))
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
        elif key in ("include_cycling", "race_enabled", "notify_morning", "notify_weekly", "notify_problems"):
            if not isinstance(value, bool):
                raise PrefError(key, "Must be on or off.")
        elif key == "ai_provider":
            if value not in AI_PROVIDERS:
                raise PrefError(key, "Choose an AI provider.")
        elif key == "layout":
            value = validate_layout(value)
        elif key == "ollama_url":
            value = str(value or "").strip().rstrip("/")
            if value:
                parts = urlsplit(value)
                if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password \
                        or parts.query or parts.fragment or parts.path not in ("", "/") or len(value) > 200:
                    raise PrefError(key, "Use an address like http://192.168.4.23:11434")
                try:
                    link_local = ipaddress.ip_address(parts.hostname).is_link_local
                except ValueError:
                    link_local = False  # a host name, not an IP
                if link_local:
                    raise PrefError(key, "That address range isn't allowed. Use your Ollama server's LAN address.")
                value = f"{parts.scheme}://{parts.netloc}"
        elif key == "weather_place":
            value = " ".join(str(value or "").split())
            if len(value) > 120 or any(ord(ch) < 32 for ch in value):
                raise PrefError(key, "Pick a place from the search results.")
        elif key in ("weather_lat", "weather_lon"):
            lim = 90 if key == "weather_lat" else 180
            if value in (None, ""):
                value = None
            else:
                if isinstance(value, bool):
                    raise PrefError(key, "Pick a place from the search results.")
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    raise PrefError(key, "Pick a place from the search results.") from None
                if not math.isfinite(value) or not -lim <= value <= lim:
                    raise PrefError(key, "Pick a place from the search results.")
                value = round(value, 2)  # about 1 km: enough for weather, no more precise than needed
        elif key == "ai_model":
            value = str(value or "").strip()
            if value and not _MODEL_RE.match(value):
                raise PrefError(key, "That doesn't look like a model name.")
        out[key] = value
    return out


def validate_layout(value: Any) -> list[dict]:
    if not isinstance(value, list) or len(value) > 60:
        raise PrefError("layout", "Invalid layout.")
    out, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            raise PrefError("layout", "Invalid layout.")
        wid, sec, w, h = item.get("id"), item.get("section"), item.get("w"), item.get("h")
        if wid not in WIDGETS or wid in seen or sec not in SECTIONS:
            raise PrefError("layout", "Invalid layout.")
        if isinstance(w, bool) or isinstance(h, bool) or not isinstance(w, int) or not isinstance(h, int) \
                or not 2 <= w <= 12 or not 2 <= h <= 12:
            raise PrefError("layout", "Box sizes must be 2–12 columns wide and 2–12 rows tall.")
        seen.add(wid)
        out.append({"id": wid, "section": sec, "w": w, "h": h})
    return complete_layout(out)


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
