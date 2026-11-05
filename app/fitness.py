"""Fitness score (1–100), headline metric series, and the fact sheet the AI coach reads.

The score blends four pillars, each 0–100, from your own data:

  Engine        VO2 max against age/sex norms (or estimated from your 5K when VO2 max is missing)
  Recovery      HRV and resting HR against your own 30-day normal, sleep score, readiness, Body Battery
  Balance       this week's training load against your usual (acute:chronic ratio sweet spot 0.8–1.3)
  Consistency   active weeks out of the last 8, step-goal days, weekly intensity minutes vs 150

Weights: Engine 35 %, Recovery 25 %, Consistency 25 %, Balance 15 %. Missing pillars are
left out and the rest re-weighted. Training guidance, not medical advice.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import date, timedelta
from typing import Any

from . import prefs as prefs_mod
from .db import Database

MI = 1.609344
RIEGEL = 1.06
WEIGHTS = {"engine": 0.35, "recovery": 0.25, "consistency": 0.25, "balance": 0.15}
PILLAR_NAMES = {"engine": "Engine", "recovery": "Recovery", "balance": "Training balance", "consistency": "Consistency"}
TIERS = [(85, "Peak"), (70, "Strong"), (55, "Solid"), (40, "Building"), (0, "Recharge")]

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


def _interp(x: float, pts: list[tuple[float, float]]) -> float:
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def tier(score: float | None) -> str | None:
    if score is None:
        return None
    return next(label for cut, label in TIERS if score >= cut)


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


# ------------------------------------------------------------------ pillars
def _engine(data: Data, d: date) -> tuple[float | None, list[str], dict]:
    vo2, when = data.last_known("training", "vo2max", d, 60)
    source = "garmin"
    if vo2 is None:
        secs, _ = five_k(data, d)
        vo2 = vdot_from_race(5000, secs) if secs else None
        source = "5k"
    score = vo2_score(vo2, data.age, data.sex)
    notes = []
    if score is not None:
        notes.append(f"VO2 max {vo2:.1f}" + (" (estimated from your 5K)" if source == "5k" else "")
                     + (f", for age {data.age}" if data.age else ""))
    return score, notes, {"vo2max": round(vo2, 1) if vo2 else None, "vo2_source": source if vo2 else None}


def _recovery(data: Data, d: date) -> tuple[float | None, list[str], dict]:
    parts, notes, raw = [], [], {}
    hrv, base = data.value("daily", "hrv_last_night", d), data.baseline("daily", "hrv_last_night", d)
    if hrv and base:
        r = hrv / base
        parts.append(_clamp(75 + (r - 1) * 200))
        if abs(r - 1) >= 0.03:
            notes.append(f"HRV {abs(r - 1):.0%} {'above' if r >= 1 else 'below'} your normal")
        else:
            notes.append("HRV right at your normal")
        raw["hrv_vs_normal_pct"] = round((r - 1) * 100)
    rhr, rbase = data.value("daily", "resting_hr", d), data.baseline("daily", "resting_hr", d)
    if rhr and rbase:
        diff = rhr - rbase
        parts.append(_clamp(75 - diff * 7))
        if abs(diff) >= 2:
            notes.append(f"Resting HR {abs(diff):.0f} bpm {'above' if diff > 0 else 'below'} your normal")
        raw["rhr_vs_normal_bpm"] = round(diff, 1)
    sleep = data.value("daily", "sleep_score", d)
    if sleep is None and data.value("daily", "sleep_s", d):
        sleep = _clamp(25 + data.value("daily", "sleep_s", d) / 3600 * 7.5)
    if sleep is not None:
        parts.append(_clamp(sleep))
        raw["sleep"] = round(sleep)
    ready = data.value("training", "readiness_score", d)
    if ready is not None:
        parts.append(_clamp(ready))
    bb = data.value("daily", "bb_high", d)
    if bb is not None:
        parts.append(_clamp(bb))
    if not parts:
        return None, [], raw
    return sum(parts) / len(parts), notes, raw


def _balance(data: Data, d: date) -> tuple[float | None, list[str], dict]:
    acwr = data.value("training", "acwr", d)
    if acwr is None:
        def load(start: date, end: date) -> float:
            return sum((a.get("duration_s") or 0) / 60 for a in data.acts_between(start, end))
        acute = load(d - timedelta(days=6), d)
        chronic = load(d - timedelta(days=27), d) / 4
        if chronic < 30:  # under ~30 min/week of history: not enough to judge
            return None, [], {}
        acwr = acute / chronic
    score = _interp(acwr, [(0, 20), (0.5, 45), (0.8, 90), (1.0, 100), (1.3, 95), (1.5, 70), (2.0, 25), (3.0, 10)])
    note = None
    if acwr > 1.5:
        note = f"Load is {acwr:.1f}× your usual: injury risk rises"
    elif acwr < 0.6:
        note = "Training load well below your usual"
    return score, [note] if note else [], {"acwr": round(acwr, 2)}


def _consistency(data: Data, d: date) -> tuple[float | None, list[str], dict]:
    parts, notes = [], []
    if data.acts:
        active = 0
        for w in range(8):
            end = d - timedelta(days=7 * w)
            wk = data.acts_between(end - timedelta(days=6), end)
            minutes = sum((a.get("duration_s") or 0) for a in wk) / 60
            active += len(wk) >= 2 or minutes >= 75
        parts.append((active / 8 * 100, 0.5))
        notes.append(f"Active {active} of the last 8 weeks")
    steps = [(r.get("steps"), r.get("step_goal")) for i in range(14)
             if (r := data.daily.get((d - timedelta(days=i)).isoformat())) and r.get("steps") is not None]
    if steps:
        hit = sum(1 for s, g in steps if s >= (g or 7500))
        parts.append((hit / len(steps) * 100, 0.25))
    mins = [(r.get("intensity_moderate") or 0) + 2 * (r.get("intensity_vigorous") or 0) for i in range(7)
            if (r := data.daily.get((d - timedelta(days=i)).isoformat()))
            and (r.get("intensity_moderate") is not None or r.get("intensity_vigorous") is not None)]
    if mins:
        parts.append((_clamp(sum(mins) / 150 * 100), 0.25))
    if not parts:
        return None, [], {}
    total_w = sum(w for _, w in parts)
    return sum(v * w for v, w in parts) / total_w, notes, {}


PILLARS = {"engine": _engine, "recovery": _recovery, "balance": _balance, "consistency": _consistency}


def score_day(data: Data, d: date) -> dict:
    pillars, notes, raw = {}, {}, {}
    for key, fn in PILLARS.items():
        val, n, r = fn(data, d)
        if val is not None:
            pillars[key] = round(val)
            notes[key] = n
        raw.update(r)
    total = None
    if len(pillars) >= 2:
        w = sum(WEIGHTS[k] for k in pillars)
        total = int(round(_clamp(sum(pillars[k] * WEIGHTS[k] for k in pillars) / w, 1, 100)))
    return {"date": d.isoformat(), "score": total, "tier": tier(total), "pillars": pillars, "notes": notes, "raw": raw}


# ------------------------------------------------------------------ public API
_cache: dict[tuple, Any] = {}


def _cache_key(db: Database, kind: str, today: date, days: int) -> tuple:
    last = db.last_sync() or {}
    return (kind, today.isoformat(), days, last.get("id"), last.get("finished_at"),
            db.get_meta("prefs"), db.get_meta("profile"), db.count("daily"), db.count("activities"))


def overview(db: Database, days: int = 90, today: date | None = None) -> dict:
    """Fitness score today (with drivers) + history, and series for the headline metrics."""
    today = today or date.today()
    key = _cache_key(db, "overview", today, days)
    if key in _cache:
        return _cache[key]
    data = Data(db, today, days)
    history, five = [], []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        s = score_day(data, d)
        history.append({"date": s["date"], "score": s["score"], **{k: s["pillars"].get(k) for k in PILLARS}})
        secs, src = five_k(data, d)
        five.append({"date": d.isoformat(), "seconds": round(secs) if secs else None, "source": src})
    now = score_day(data, today)
    week_ago = next((h for h in reversed(history[:-7]) if h["score"] is not None), None)
    drivers = []
    if week_ago:
        for k in PILLARS:
            if now["pillars"].get(k) is not None and week_ago.get(k) is not None:
                delta = now["pillars"][k] - week_ago[k]
                if abs(delta) >= 3:
                    drivers.append({"pillar": PILLAR_NAMES[k], "delta": delta})
        drivers.sort(key=lambda x: -abs(x["delta"]))
    secs, src = five_k(data, today)
    old_secs = next((f["seconds"] for f in reversed(five[:-30]) if f["seconds"]), None) if len(five) > 30 else None
    result = {
        "today": {**now, "pillar_names": PILLAR_NAMES, "weights": WEIGHTS,
                  "change_7d": (now["score"] - week_ago["score"]) if (now["score"] is not None and week_ago) else None,
                  "drivers": drivers[:3]},
        "history": history,
        "five_k": {"seconds": round(secs) if secs else None, "display": fmt_duration(secs), "source": src,
                   "change_30d_s": round(secs - old_secs) if (secs and old_secs) else None, "series": five},
        "profile_known": {"age": data.age is not None, "sex": data.sex is not None},
    }
    if len(_cache) > 20:
        _cache.clear()
    _cache[key] = result
    return result


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
    t = ov["today"]
    y = today - timedelta(days=1)
    out = {
        "date": today.isoformat(), "units": unit,
        "fitness_score": {"score": t["score"], "tier": t["tier"], "change_7d": t["change_7d"],
                          "pillars": t["pillars"], "notes": t["notes"]},
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
                          "acute_chronic_ratio": ov["today"]["raw"].get("acwr")},
        "estimated_5k": {"time": ov["five_k"]["display"], "source": ov["five_k"]["source"],
                         "change_30d_seconds": ov["five_k"]["change_30d_s"]},
        "last_7_days_activities": last7,
        "weekly_running": weekly,
        "goal_race": race_card(db, today),
    }
    return out
