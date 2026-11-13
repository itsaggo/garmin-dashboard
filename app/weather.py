"""Local weather and heat-graded paces.

Forecasts come from Open-Meteo (https://open-meteo.com): free, no account or API key, data under
CC BY 4.0. The only thing sent is the place you pick in Settings, rounded to two decimals (about 1 km).
Answers are cached in the database, so the page, the coach and Discord share one fetch.

Heat grade uses the runner's rule of thumb of adding the air temperature and the dew point (both °F)
and slowing down by a set percentage as the sum climbs (popularised by coach Mark Hadley):

    sum  ≤100   no change          141–150  3–4.5 %
         101–110  0–0.5 %          151–160  4.5–6 %
         111–120  0.5–1 %          161–170  6–8 %
         121–130  1–2 %            171–180  8–10 %
         131–140  2–3 %            over 180  hard running not recommended

Training guidance, not medical advice.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from . import net, prefs as prefs_mod
from .db import Database

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FRESH_S = 30 * 60          # the page re-fetches when the cache is older than this
BACKGROUND_S = 60 * 60     # the scheduler keeps it at least this fresh for the coach and Discord
MAX_BYTES = 1_000_000
HOURS_SHOWN = 18

# (temp °F + dew point °F, % slower)
HEAT_POINTS = [(100, 0.0), (110, 0.5), (120, 1.0), (130, 2.0), (140, 3.0), (150, 4.5), (160, 6.0), (170, 8.0), (180, 10.0)]
GRADES = [(100, "None", "No heat adjustment needed. Run your normal paces."),
          (120, "Light", "Barely noticeable. Your normal paces are fine."),
          (140, "Moderate", "Slow easy runs a little and expect a higher heart rate than usual."),
          (160, "Heavy", "Noticeably harder. Slow down, carry water and run by feel."),
          (180, "Severe", "Hard efforts aren't worth it today. Keep it easy or go when it's cooler."),
          (math.inf, "Extreme", "Too hot and humid for hard running. Easy effort only, or move indoors.")]

# WMO weather codes, as Open-Meteo reports them.
SKY = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Freezing fog",
       51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
       61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
       71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Showers", 81: "Showers",
       82: "Heavy showers", 85: "Snow showers", 86: "Snow showers", 95: "Thunderstorms", 96: "Thunderstorms, hail",
       99: "Thunderstorms, hail"}

_fetch_lock = threading.Lock()
_geo_lock = threading.Lock()
_geo_times: list[float] = []


class WeatherError(Exception):
    pass


# ------------------------------------------------------------------ heat math
def heat_pct(temp_f: float | None, dew_f: float | None) -> float | None:
    """How much slower (in %) the same effort runs at this temperature and dew point."""
    if temp_f is None or dew_f is None:
        return None
    s = temp_f + dew_f
    if s <= HEAT_POINTS[0][0]:
        return 0.0
    for (x0, y0), (x1, y1) in zip(HEAT_POINTS, HEAT_POINTS[1:]):
        if s <= x1:
            return round(y0 + (y1 - y0) * (s - x0) / (x1 - x0), 1)
    return HEAT_POINTS[-1][1]


def heat_grade(temp_f: float | None, dew_f: float | None) -> dict | None:
    if temp_f is None or dew_f is None:
        return None
    s = round(temp_f + dew_f)
    name, advice = next((n, a) for cut, n, a in GRADES if s <= cut)
    return {"sum": s, "pct": heat_pct(temp_f, dew_f), "grade": name, "advice": advice, "too_hot": s > 180}


def adjust(s_per_km: float, pct: float | None) -> float | None:
    return round(s_per_km * (1 + pct / 100), 1) if (s_per_km and pct is not None) else None


# ------------------------------------------------------------------ Open-Meteo
def _get_json(url: str, params: dict[str, Any]) -> Any:
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "garmin-dashboard (self-hosted)", "Accept": "application/json"})
    try:
        with net.open_url(req, 10) as resp:
            body = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise WeatherError(f"The weather service returned an error ({e.code}).") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise WeatherError(f"Couldn't reach the weather service ({type(e).__name__}).") from e
    if len(body) > MAX_BYTES:
        raise WeatherError("The weather service sent an unexpectedly large answer.")
    try:
        return json.loads(body)
    except ValueError as e:
        raise WeatherError("The weather service sent something that wasn't JSON.") from e


def _num(v: Any, lo: float = -200, hi: float = 400) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        return None
    return float(v)


_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _t(v: Any) -> str | None:
    """'2026-11-11T12:15' (seconds or an offset, if ever sent, are dropped)."""
    return v[:16] if isinstance(v, str) and _TIME_RE.match(v) else None


def _clean_forecast(raw: Any) -> dict:
    """Keep only well-formed numbers and times from the reply."""
    if not isinstance(raw, dict):
        raise WeatherError("The weather service sent an unexpected answer.")
    cur = raw.get("current") if isinstance(raw.get("current"), dict) else {}
    hourly = raw.get("hourly") if isinstance(raw.get("hourly"), dict) else {}
    times = [_t(t) for t in hourly.get("time") or []]

    def col(name: str, lo: float, hi: float) -> list:
        vals = hourly.get(name) or []
        return [_num(v, lo, hi) for v in vals][: len(times)] + [None] * max(0, len(times) - len(vals))

    out = {
        "time": _t(cur.get("time")),
        "utc_offset_s": _num(raw.get("utc_offset_seconds"), -14 * 3600, 14 * 3600),
        "current": {"temp_f": _num(cur.get("temperature_2m"), -80, 140), "dew_f": _num(cur.get("dew_point_2m"), -100, 100),
                    "humidity": _num(cur.get("relative_humidity_2m"), 0, 100), "feels_f": _num(cur.get("apparent_temperature"), -100, 160),
                    "wind_mph": _num(cur.get("wind_speed_10m"), 0, 250), "code": _num(cur.get("weather_code"), 0, 99)},
        "hourly": [{"time": t, "temp_f": a, "dew_f": b, "rain_pct": c, "code": d}
                   for t, a, b, c, d in zip(times, col("temperature_2m", -80, 140), col("dew_point_2m", -100, 100),
                                            col("precipitation_probability", 0, 100), col("weather_code", 0, 99))
                   if t],
    }
    if out["current"]["temp_f"] is None and not out["hourly"]:
        raise WeatherError("The weather service had no data for this place.")
    return out


def fetch(lat: float, lon: float) -> dict:
    raw = _get_json(FORECAST_URL, {
        "latitude": f"{lat:.2f}", "longitude": f"{lon:.2f}",
        "current": "temperature_2m,relative_humidity_2m,dew_point_2m,apparent_temperature,weather_code,wind_speed_10m",
        "hourly": "temperature_2m,dew_point_2m,precipitation_probability,weather_code",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "auto", "forecast_days": 2,
    })
    return _clean_forecast(raw)


def places(query: str) -> list[dict]:
    """Search for a city by name (Open-Meteo's geocoder). A few searches a minute at most."""
    q = " ".join(str(query or "").split())
    if not 2 <= len(q) <= 80:
        raise WeatherError("Type at least 2 letters of a city name.")
    with _geo_lock:
        now = time.monotonic()
        _geo_times[:] = [t for t in _geo_times if now - t < 60]
        if len(_geo_times) >= 20:
            raise WeatherError("Too many searches. Try again in a minute.")
        _geo_times.append(now)
    raw = _get_json(GEOCODE_URL, {"name": q, "count": 8, "language": "en", "format": "json"})
    out = []
    for r in (raw.get("results") if isinstance(raw, dict) else None) or []:
        if not isinstance(r, dict):
            continue
        lat, lon = _num(r.get("latitude"), -90, 90), _num(r.get("longitude"), -180, 180)
        parts = [str(r.get(k)).strip()[:60] for k in ("name", "admin1", "country") if isinstance(r.get(k), str) and r.get(k).strip()]
        if lat is None or lon is None or not parts:
            continue
        out.append({"label": ", ".join(dict.fromkeys(parts)), "lat": round(lat, 2), "lon": round(lon, 2)})
    return out


# ------------------------------------------------------------------ cache
def _place(db: Database) -> tuple[str, float, float] | None:
    p = prefs_mod.get_all(db)
    lat, lon = p.get("weather_lat"), p.get("weather_lon")
    if lat is None or lon is None:
        return None
    return p.get("weather_place") or "", float(lat), float(lon)


def _cached(db: Database) -> dict | None:
    try:
        c = json.loads(db.get_meta("weather_cache") or "null")
    except ValueError:
        return None
    return c if isinstance(c, dict) and isinstance(c.get("data"), dict) else None


def _refresh(db: Database, max_age: float) -> tuple[dict | None, str | None]:
    """(cache entry, error). Fetches when the cache is missing, older than max_age, or for another place."""
    where = _place(db)
    if not where:
        return None, "Choose a weather location in Settings."
    key = f"{where[1]:.2f},{where[2]:.2f}"
    c = _cached(db)
    if c and c.get("key") == key and time.time() - float(c.get("fetched", 0)) < max_age:
        return c, None
    if not _fetch_lock.acquire(timeout=12):
        return (c if c and c.get("key") == key else None), "Weather is updating. Try again in a moment."
    try:
        c = _cached(db)  # someone else may have just fetched it
        if c and c.get("key") == key and time.time() - float(c.get("fetched", 0)) < max_age:
            return c, None
        try:
            data = fetch(where[1], where[2])
        except WeatherError as e:
            return (c if c and c.get("key") == key else None), str(e)
        c = {"key": key, "fetched": time.time(), "data": data}
        db.set_meta("weather_cache", json.dumps(c))
        return c, None
    finally:
        _fetch_lock.release()


def maybe_refresh(db: Database) -> None:
    """Scheduler hook: keep the forecast reasonably fresh for the morning coach note and Discord."""
    if _place(db):
        _refresh(db, BACKGROUND_S)


# ------------------------------------------------------------------ what the page shows
def _now_index(hours: list[dict], now_local: str | None) -> int:
    if not now_local:
        return 0
    floor = now_local[:13] + ":00"
    for i, h in enumerate(hours):
        if h["time"] >= floor:
            return i
    return max(0, len(hours) - 1)


def _best_hour(hours: list[dict]) -> dict | None:
    """Coolest (lowest temperature + dew point) hour between 5 a.m. and 8 p.m. in the next day."""
    pick = None
    for h in hours[:24]:
        hour = int(h["time"][11:13])
        if not 5 <= hour <= 20 or h["temp_f"] is None or h["dew_f"] is None:
            continue
        s = h["temp_f"] + h["dew_f"]
        if pick is None or s < pick[0] - 0.5:
            pick = (s, h)
    return pick[1] if pick else None


def report(db: Database, paces: list[dict] | None = None, fetch_ok: bool = True) -> dict:
    """Everything the weather box needs. Temperatures in °F, wind in mph, paces in seconds per km."""
    where = _place(db)
    if fetch_ok:
        entry, err = _refresh(db, FRESH_S)
    else:
        entry, err = _cached(db), None
        if entry and (not where or entry.get("key") != f"{where[1]:.2f},{where[2]:.2f}"):
            entry = None  # cached for a different place
    if not entry or not where:
        return {"ok": False, "error": err or "No weather yet.", "place": where[0] if where else None}
    d = entry["data"]
    cur = dict(d["current"])
    cur["sky"] = SKY.get(int(cur["code"])) if cur.get("code") is not None else None
    hours = d["hourly"]
    now_local = d.get("time")
    if not now_local and d.get("utc_offset_s") is not None:  # fall back to the place's clock
        now_local = datetime.fromtimestamp(time.time() + d["utc_offset_s"], timezone.utc).strftime("%Y-%m-%dT%H:%M")
    i = _now_index(hours, now_local)
    upcoming = hours[i: i + 24]
    shown = []
    for h in upcoming[:HOURS_SHOWN]:
        shown.append({**h, "pct": heat_pct(h["temp_f"], h["dew_f"]), "sky": SKY.get(int(h["code"])) if h["code"] is not None else None})
    best = _best_hour(upcoming)
    if best:
        best = {**best, **(heat_grade(best["temp_f"], best["dew_f"]) or {})}
    heat = heat_grade(cur.get("temp_f"), cur.get("dew_f"))
    now_pct = heat["pct"] if heat else None
    best_pct = best.get("pct") if best else None
    rows = [{**p, "now_s_per_km": adjust(p["s_per_km"], now_pct), "best_s_per_km": adjust(p["s_per_km"], best_pct)}
            for p in (paces or [])]
    fetched = datetime.fromtimestamp(float(entry["fetched"]), timezone.utc).isoformat(timespec="seconds")
    return {"ok": True, "place": where[0], "updated_local": now_local, "fetched_at": fetched,
            "stale": time.time() - float(entry["fetched"]) > 3 * 3600, "error": err,
            "current": cur, "heat": heat, "hours": shown, "best": best, "paces": rows,
            "source": "Open-Meteo.com"}


def summary(db: Database) -> dict | None:
    """Short, cached-only version for the AI coach and Discord (never calls out by itself, no place name)."""
    try:
        r = report(db, fetch_ok=False)
    except Exception:  # weather must never break the coach
        return None
    if not r.get("ok") or r.get("stale"):
        return None
    c, h, b = r["current"], r["heat"] or {}, r["best"] or {}
    return {"temp_f": c.get("temp_f"), "dew_point_f": c.get("dew_f"), "humidity_pct": c.get("humidity"),
            "sky": c.get("sky"), "heat_grade": h.get("grade"), "slow_down_pct": h.get("pct"),
            "coolest_hour_today": b.get("time"), "coolest_hour_slow_down_pct": b.get("pct")}
