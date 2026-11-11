"""Headline metric series, training paces, the goal-race card, and the fact sheet the AI coach reads.

Training guidance, not medical advice.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import date, timedelta
from typing import Any

from . import prefs as prefs_mod, weather
from .db import Database

MI = 1.609344
RIEGEL = 1.06
# Daniels: easy running sits around 59–74 % of VDOT, threshold around 86–88 %.
EASY_PCT, THRESHOLD_PCT = 0.65, 0.86

# Approximate VO2 max 10th and 95th percentiles by sex and age decade (treadmill norms).
_VO2_NORMS = {
    "male": {20: (33, 61), 30: (30, 57), 40: (27, 53), 50: (24, 49), 60: (21, 45), 70: (18, 41)},
    "female": {20: (27, 52), 30: (25, 48), 40: (23, 45), 50: (20, 41), 60: (18, 37), 70: (16, 34)},
}


# ------------------------------------------------------------------ helpers
def _d(iso: str) -> date:
    return date.fromisoformat(iso[:10])


def _clamp(v: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, v))


def fmt_duration(sec: float | None) -> str | None:
    if sec is None:
        return None
    sec = round(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def vdot_from_race(dist_m: float, secs: float) -> float | None:
    """Daniels' VO2 estimate from a race result."""
    if not dist_m or not secs or secs <= 0:
        return None
    t = secs / 60
    v = dist_m / t
    vo2 = -4.6 + 0.182258 * v + 0.000104 * v * v
    pct = 0.8 + 0.1894393 * math.exp(-0.012778 * t) + 0.2989558 * math.exp(-0.1932605 * t)
    return vo2 / pct if pct else None


def pace_for_vo2(vo2: float) -> float | None:
    """Seconds per km at the running speed that costs this much oxygen (Daniels' cost curve)."""
    a, b, c = 0.000104, 0.182258, -4.6 - vo2
    disc = b * b - 4 * a * c
    if vo2 <= 0 or disc <= 0:
        return None
    v = (-b + math.sqrt(disc)) / (2 * a)  # meters per minute
    return 60000 / v if v > 0 else None


def vo2_score(vo2: float | None, age: int | None, sex: str | None) -> float | None:
    if vo2 is None:
        return None
    decade = 30 if age is None else min(70, max(20, (age // 10) * 10))
    if sex in _VO2_NORMS:
        lo, hi = _VO2_NORMS[sex][decade]
    else:
        (a, b), (c, e) = _VO2_NORMS["male"][decade], _VO2_NORMS["female"][decade]
        lo, hi = (a + c) / 2, (b + e) / 2
    return _clamp(10 + 85 * (vo2 - lo) / (hi - lo))  # 10th pct → 10, 95th pct → 95


def _is_run(a: dict) -> bool:
    return "run" in (a.get("type") or "")


# ------------------------------------------------------------------ data bundle
class Data:
    """Everything the calculations need, loaded once."""

    def __init__(self, db: Database, today: date, days_back: int):
        self.today = today
        since = (today - timedelta(days=days_back + 70)).isoformat()
        self.daily = {r["date"]: r for r in db.rows_since("daily", since)}
        self.training = {r["date"]: r for r in db.rows_since("training", since)}
        self.acts = sorted((a for a in db.activities(5000) if a.get("start_local") and a["start_local"][:10] >= since),
                           key=lambda a: a["start_local"])
        self.prefs = prefs_mod.get_all(db)
        try:
            prof = json.loads(db.get_meta("profile") or "{}")
        except ValueError:
            prof = {}
        self.sex = prof.get("sex")
        self.age = (today.year - prof["birth_year"]) if prof.get("birth_year") else None

    def series(self, table: str, key: str, start: date, end: date) -> list[float]:
        src = self.daily if table == "daily" else self.training
        out = []
        d = start
        while d <= end:
            v = (src.get(d.isoformat()) or {}).get(key)
            if v is not None:
                out.append(v)
            d += timedelta(days=1)
        return out

    def value(self, table: str, key: str, d: date) -> Any:
        src = self.daily if table == "daily" else self.training
        return (src.get(d.isoformat()) or {}).get(key)

    def last_known(self, table: str, key: str, d: date, within: int) -> tuple[Any, date | None]:
        for i in range(within + 1):
            v = self.value(table, key, d - timedelta(days=i))
            if v is not None:
                return v, d - timedelta(days=i)
        return None, None

    def baseline(self, table: str, key: str, d: date) -> float | None:
        vals = self.series(table, key, d - timedelta(days=30), d - timedelta(days=3))
        return statistics.median(vals) if len(vals) >= 7 else None

    def acts_between(self, start: date, end: date) -> list[dict]:
        s, e = start.isoformat(), end.isoformat()
        return [a for a in self.acts if s <= a["start_local"][:10] <= e]


# ------------------------------------------------------------------ 5K estimate
def five_k(data: Data, d: date) -> tuple[float | None, str | None]:
    """(seconds, source): Garmin's race predictor if available, else from recent best runs."""
    garmin, _ = data.last_known("training", "race_5k_s", d, 14)
    if garmin:
        return garmin, "garmin"
    preds = []
    for a in data.acts_between(d - timedelta(days=42), d):
        if not _is_run(a):
            continue
        km = (a.get("distance_m") or 0) / 1000
        secs = a.get("moving_s") or a.get("duration_s")
        if km < 3 or not secs:
            continue
        pace = secs / km
        if not 150 <= pace <= 900:
            continue
        preds.append(secs * (5 / km) ** RIEGEL)
    if not preds:
        return None, None
    preds.sort()
    return statistics.median(preds[:3]), "runs"


# ------------------------------------------------------------------ helpers on the bundle
def vo2_now(data: Data, d: date) -> tuple[float | None, str | None]:
    """Garmin's VO2 max, or one estimated from the 5K when Garmin has none."""
    vo2, _ = data.last_known("training", "vo2max", d, 60)
    if vo2 is not None:
        return vo2, "garmin"
    secs, _ = five_k(data, d)
    vo2 = vdot_from_race(5000, secs) if secs else None
    return (vo2, "5k") if vo2 else (None, None)


def acwr(data: Data, d: date) -> float | None:
    """This week's load against your usual (Garmin's ratio, or one from workout minutes)."""
    r = data.value("training", "acwr", d)
    if r is not None:
        return r
    def load(start: date, end: date) -> float:
        return sum((a.get("duration_s") or 0) / 60 for a in data.acts_between(start, end))
    chronic = load(d - timedelta(days=27), d) / 4
    if chronic < 30:  # under ~30 min/week of history: not enough to judge
        return None
    return round(load(d - timedelta(days=6), d) / chronic, 2)


# ------------------------------------------------------------------ public API
_cache: dict[tuple, Any] = {}


def _cache_key(db: Database, kind: str, today: date, days: int) -> tuple:
    last = db.last_sync() or {}
    return (kind, today.isoformat(), days, last.get("id"), last.get("finished_at"),
            db.get_meta("prefs"), db.get_meta("profile"), db.count("daily"), db.count("activities"))


def overview(db: Database, days: int = 90, today: date | None = None) -> dict:
    """The 5K estimate (with its history) and how your VO2 max compares for your age."""
    today = today or date.today()
    key = _cache_key(db, "overview", today, days)
    if key in _cache:
        return _cache[key]
    data = Data(db, today, days)
    five = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        secs, src = five_k(data, d)
        five.append({"date": d.isoformat(), "seconds": round(secs) if secs else None, "source": src})
    secs, src = five_k(data, today)
    old_secs = next((f["seconds"] for f in reversed(five[:-30]) if f["seconds"]), None) if len(five) > 30 else None
    vo2, vo2_src = vo2_now(data, today)
    result = {
        "five_k": {"seconds": round(secs) if secs else None, "display": fmt_duration(secs), "source": src,
                   "change_30d_s": round(secs - old_secs) if (secs and old_secs) else None, "series": five},
        "vo2": {"value": round(vo2, 1) if vo2 else None, "source": vo2_src,
                "for_age": round(vo2_score(vo2, data.age, data.sex)) if vo2 else None},
        "profile_known": {"age": data.age is not None, "sex": data.sex is not None},
    }
    if len(_cache) > 20:
        _cache.clear()
    _cache[key] = result
    return result


def training_paces(db: Database, today: date | None = None) -> list[dict]:
    """Easy and threshold paces from your 5K estimate, plus goal-race pace, in seconds per km."""
    today = today or date.today()
    data = Data(db, today, 7)
    out = []
    secs, _ = five_k(data, today)
    vdot = vdot_from_race(5000, secs) if secs else None
    if vdot:
        for name, pct in (("Easy", EASY_PCT), ("Threshold", THRESHOLD_PCT)):
            per_km = pace_for_vo2(vdot * pct)
            if per_km:
                out.append({"name": name, "s_per_km": round(per_km, 1), "from": "5K estimate"})
    p = data.prefs
    km, goal = prefs_mod.race_km(p), prefs_mod.goal_seconds(p.get("race_goal_time", ""))
    if p.get("race_enabled") and km and goal and p.get("race_date") and p["race_date"] >= today.isoformat():
        out.append({"name": "Race goal", "s_per_km": round(goal / km, 1), "from": p.get("race_name") or "goal race"})
    return out


def race_card(db: Database, today: date | None = None) -> dict | None:
    """Goal-race summary when the race switch is on."""
    today = today or date.today()
    p = prefs_mod.get_all(db)
    if not p.get("race_enabled") or not p.get("race_date"):
        return None
    km = prefs_mod.race_km(p)
    goal = prefs_mod.goal_seconds(p.get("race_goal_time", ""))
    days = (date.fromisoformat(p["race_date"]) - today).days
    data = Data(db, today, 7)
    est = None
    if km:
        field = {5.0: "race_5k_s", 10.0: "race_10k_s", 21.0975: "race_half_s", 42.195: "race_marathon_s"}.get(km)
        garmin = data.last_known("training", field, today, 14)[0] if field else None
        if garmin:
            est = garmin
        else:
            s5, _ = five_k(data, today)
            est = s5 * (km / 5) ** RIEGEL if s5 else None
    metric = p["units"] == "metric"
    unit = "km" if metric else "mi"

    def pace(secs: float | None) -> str | None:
        if not secs or not km:
            return None
        per = round(secs / km * (1 if metric else MI))
        return f"{per // 60}:{per % 60:02d}/{unit}"

    return {
        "name": p.get("race_name") or "Goal race", "date": p["race_date"], "days_to_go": days,
        "distance": f"{(km if metric else km / MI):.1f} {unit}" if km else None,
        "goal": fmt_duration(goal), "goal_pace": pace(goal),
        "estimate": fmt_duration(est), "estimate_pace": pace(est),
        "gap_s": round(est - goal) if (est and goal) else None,
    }


def facts(db: Database, today: date | None = None) -> dict:
    """A compact, name-free summary of the numbers for the AI coach."""
    today = today or date.today()
    p = prefs_mod.get_all(db)
    ov = overview(db, 60, today)
    data = Data(db, today, 60)
    metric = p["units"] == "metric"
    unit = "km" if metric else "mi"

    def dist(m: float | None) -> float | None:
        return round(m / 1000 / (1 if metric else MI), 2) if m else None

    def avg(vals: list) -> float | None:
        return round(sum(vals) / len(vals), 1) if vals else None

    last7 = [{"date": a["start_local"][:10], "type": a.get("type"), "distance_" + unit: dist(a.get("distance_m")),
              "duration": fmt_duration(a.get("duration_s")), "avg_hr": a.get("avg_hr"),
              "aerobic_training_effect": a.get("aerobic_te")}
             for a in data.acts_between(today - timedelta(days=6), today)]
    weekly = []
    for w in range(4):
        end = today - timedelta(days=7 * w)
        wk = [a for a in data.acts_between(end - timedelta(days=6), end) if _is_run(a)]
        weekly.append({"weeks_ago": w, "runs": len(wk), "run_distance_" + unit: dist(sum(a.get("distance_m") or 0 for a in wk))})
    y = today - timedelta(days=1)
    out = {
        "date": today.isoformat(), "units": unit,
        "vo2max": {"now": data.last_known("training", "vo2max", today, 60)[0],
                   "30_days_ago": data.last_known("training", "vo2max", today - timedelta(days=30), 30)[0]},
        "training_readiness": {"today": data.value("training", "readiness_score", today),
                               "level": data.value("training", "readiness_level", today),
                               "7_day_avg": avg(data.series("training", "readiness_score", today - timedelta(days=6), today)),
                               "recovery_time_hours": round(r / 60) if (r := data.value("training", "recovery_min", today)) else r},
        "sleep": {"last_night_score": data.value("daily", "sleep_score", today) or data.value("daily", "sleep_score", y),
                  "last_night_hours": round(s / 3600, 1) if (s := data.value("daily", "sleep_s", today) or data.value("daily", "sleep_s", y)) else None,
                  "7_day_avg_score": avg(data.series("daily", "sleep_score", today - timedelta(days=6), today))},
        "hrv": {"last_night": data.value("daily", "hrv_last_night", today), "normal": data.baseline("daily", "hrv_last_night", today)},
        "resting_hr": {"today": data.value("daily", "resting_hr", today), "normal": data.baseline("daily", "resting_hr", today)},
        "body_battery_high": data.value("daily", "bb_high", today),
        "training_status": data.last_known("training", "training_status", today, 7)[0],
        "training_load": {"acute": data.value("training", "acute_load", today), "chronic": data.value("training", "chronic_load", today),
                          "acute_chronic_ratio": acwr(data, today)},
        "estimated_5k": {"time": ov["five_k"]["display"], "source": ov["five_k"]["source"],
                         "change_30d_seconds": ov["five_k"]["change_30d_s"]},
        "last_7_days_activities": last7,
        "weekly_running": weekly,
        "goal_race": race_card(db, today),
        "weather_now": weather.summary(db),
    }
    return out
