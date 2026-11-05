"""Turn Garmin JSON into flat rows.

Garmin's Personal API is new and its response shapes aren't fully documented
(Garmin's own client treats them as loose JSON). So instead of hard-coding one
shape, each extractor:
  1. finds every record that carries a date (calendarDate / date / ...),
  2. tries a few likely key paths for each metric,
  3. falls back to a deep search for the key name anywhere inside the record.

Raw responses are always stored too, so if a metric comes back empty you can
open /raw in the dashboard, see the real field name, and add it here.
"""
from __future__ import annotations

from typing import Any, Iterable, Iterator

DATE_KEYS = ("calendarDate", "date", "calendarDay", "summaryDate")


def _date_of(d: dict) -> str | None:
    for k in DATE_KEYS:
        v = d.get(k)
        if isinstance(v, str) and len(v) >= 10 and v[4] == "-" and v[7] == "-":
            return v[:10]
    return None


def _has_dated_list(d: dict) -> bool:
    """True when d wraps a list of dated records (e.g. {"date": ..., "items": [{"calendarDate": ...}]})."""
    return any(
        isinstance(v, list) and any(isinstance(i, dict) and _date_of(i) for i in v)
        for v in d.values()
    )


def records(obj: Any) -> Iterator[tuple[str, dict]]:
    """Yield (YYYY-MM-DD, record) for the outermost dicts that carry a date."""
    if isinstance(obj, dict):
        date = _date_of(obj)
        if date and not _has_dated_list(obj):
            yield date, obj
            return
        for v in obj.values():
            yield from records(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from records(v)


def _path(d: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(part)
    return d


def _deep(d: Any, key: str, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if isinstance(d, dict):
        if key in d and not isinstance(d[key], (dict, list)):
            return d[key]
        for v in d.values():
            found = _deep(v, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(d, list):
        for v in d:
            found = _deep(v, key, depth + 1)
            if found is not None:
                return found
    return None


def pick(d: dict, *paths: str, number: bool = True) -> Any:
    """First non-null value among the candidate paths; deep search as fallback."""
    for p in paths:
        v = _path(d, p)
        if v is not None and not isinstance(v, (dict, list)):
            return _num(v) if number else v
    for p in paths:
        v = _deep(d, p.split(".")[-1])
        if v is not None:
            return _num(v) if number else v
    return None


def _num(v: Any) -> Any:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _clean(row: dict) -> dict:
    return {k: v for k, v in row.items() if v is not None}


# ---------------------------------------------------------------- daily data

def steps(obj: Any) -> Iterable[tuple[str, dict]]:
    for date, r in records(obj):
        yield date, _clean({
            "steps": pick(r, "totalSteps", "steps", "values.totalSteps"),
            "step_goal": pick(r, "stepGoal", "dailyStepGoal", "totalStepGoal"),
            "distance_m": pick(r, "totalDistance", "totalDistanceMeters", "values.totalDistance"),
        })


def stress(obj: Any) -> Iterable[tuple[str, dict]]:
    for date, r in records(obj):
        avg = pick(r, "values.overallStressLevel", "overallStressLevel", "averageStressLevel", "avgStressLevel")
        yield date, _clean({
            "avg_stress": avg if avg is None or avg >= 0 else None,  # Garmin uses -1/-2 for "no data"
            "max_stress": pick(r, "maxStressLevel", "values.maxStressLevel"),
        })


def sleep(obj: Any) -> Iterable[tuple[str, dict]]:
    for date, r in records(obj):
        row = _clean({
            "sleep_s": pick(r, "dailySleepDTO.sleepTimeSeconds", "sleepTimeSeconds", "totalSleepSeconds", "durationInSeconds"),
            "deep_s": pick(r, "dailySleepDTO.deepSleepSeconds", "deepSleepSeconds", "deepSleepDurationInSeconds"),
            "light_s": pick(r, "dailySleepDTO.lightSleepSeconds", "lightSleepSeconds", "lightSleepDurationInSeconds"),
            "rem_s": pick(r, "dailySleepDTO.remSleepSeconds", "remSleepSeconds", "remSleepInSeconds"),
            "awake_s": pick(r, "dailySleepDTO.awakeSleepSeconds", "awakeSleepSeconds", "awakeDurationInSeconds"),
            "sleep_score": pick(r, "dailySleepDTO.sleepScores.overall.value", "sleepScores.overall.value",
                                "overallSleepScore.value", "sleepScore", "overallScore"),
        })
        if row:
            yield date, row


def hrv(obj: Any) -> Iterable[tuple[str, dict]]:
    for date, r in records(obj):
        yield date, _clean({
            "hrv_last_night": pick(r, "lastNightAvg", "hrvSummary.lastNightAvg", "lastNightAverage"),
            "hrv_weekly": pick(r, "weeklyAvg", "hrvSummary.weeklyAvg", "weeklyAverage"),
            "hrv_status": pick(r, "status", "hrvSummary.status", number=False),
        })


def body_battery(obj: Any) -> Iterable[tuple[str, dict]]:
    for date, r in records(obj):
        high = pick(r, "bodyBatteryHighestValue", "highestBodyBattery", "maxBodyBattery", "highest")
        low = pick(r, "bodyBatteryLowestValue", "lowestBodyBattery", "minBodyBattery", "lowest")
        arr = r.get("bodyBatteryValuesArray")
        if (high is None or low is None) and isinstance(arr, list):
            levels = [p[1] for p in arr if isinstance(p, list) and len(p) > 1 and isinstance(p[1], (int, float))]
            if levels:
                high = max(levels) if high is None else high
                low = min(levels) if low is None else low
        yield date, _clean({
            "bb_high": high,
            "bb_low": low,
            "bb_charged": pick(r, "charged", "bodyBatteryChargedValue"),
            "bb_drained": pick(r, "drained", "bodyBatteryDrainedValue"),
        })


def daily_summaries(obj: Any) -> Iterable[tuple[str, dict]]:
    for date, r in records(obj):
        avg_stress = pick(r, "averageStressLevel", "stressAvg")
        yield date, _clean({
            "steps": pick(r, "totalSteps"),
            "step_goal": pick(r, "dailyStepGoal", "stepGoal"),
            "distance_m": pick(r, "totalDistanceMeters", "totalDistance"),
            "floors": pick(r, "floorsAscended"),
            "resting_hr": resting_hr(r),
            "min_hr": pick(r, "minHeartRate", "minHeartRateInBeatsPerMinute"),
            "max_hr": pick(r, "maxHeartRate", "maxHeartRateInBeatsPerMinute"),
            "avg_stress": avg_stress if avg_stress is None or avg_stress >= 0 else None,
            "max_stress": pick(r, "maxStressLevel"),
            "bb_high": pick(r, "bodyBatteryHighestValue"),
            "bb_low": pick(r, "bodyBatteryLowestValue"),
            "bb_charged": pick(r, "bodyBatteryChargedValue"),
            "bb_drained": pick(r, "bodyBatteryDrainedValue"),
            "calories_total": pick(r, "totalKilocalories", "totalKilocaloriesBurned"),
            "calories_active": pick(r, "activeKilocalories"),
            "intensity_moderate": pick(r, "moderateIntensityMinutes"),
            "intensity_vigorous": pick(r, "vigorousIntensityMinutes"),
            "spo2_avg": pick(r, "averageSpo2", "averageSpO2", "avgSpo2"),
            "respiration_avg": pick(r, "avgWakingRespirationValue", "averageRespiration"),
        })


def spo2(obj: Any) -> Iterable[tuple[str, dict]]:
    for date, r in records(obj):
        yield date, _clean({"spo2_avg": pick(r, "averageSpO2", "averageSpo2", "avgSpo2", "averageValue")})


# ---------------------------------------------------------------- activities

def activity_list(obj: Any) -> list[dict]:
    if isinstance(obj, list):
        return [a for a in obj if isinstance(a, dict)]
    if isinstance(obj, dict):
        for k in ("activities", "activityList", "activitySummaries"):
            if isinstance(obj.get(k), list):
                return [a for a in obj[k] if isinstance(a, dict)]
    return []


def activity(a: dict) -> tuple[int | None, dict]:
    aid = a.get("activityId") or a.get("id") or a.get("summaryId")
    try:
        aid = int(aid)
    except (TypeError, ValueError):
        return None, {}
    atype = a.get("activityType")
    if isinstance(atype, dict):
        atype = atype.get("typeKey") or atype.get("type")
    start = a.get("startTimeLocal") or a.get("startTimeGMT") or a.get("activityDate")
    return aid, {
        "start_local": str(start).replace("T", " ")[:19] if start else None,
        "name": a.get("activityName") or a.get("name") or a.get("title"),
        "type": str(atype or "unknown").lower(),
        "distance_m": pick(a, "distance", "distanceInMeters"),
        "duration_s": pick(a, "duration", "durationInSeconds", "elapsedDuration"),
        "moving_s": pick(a, "movingDuration", "movingDurationInSeconds"),
        "avg_hr": pick(a, "averageHR", "averageHeartRateInBeatsPerMinute", "avgHr"),
        "max_hr": pick(a, "maxHR", "maxHeartRateInBeatsPerMinute"),
        "calories": pick(a, "calories", "activeKilocalories"),
        "elev_gain_m": pick(a, "elevationGain", "totalElevationGainInMeters"),
        "avg_speed_ms": pick(a, "averageSpeed", "averageSpeedInMetersPerSecond"),
        "avg_cadence": pick(a, "averageRunningCadenceInStepsPerMinute", "averageRunCadence",
                            "averageBikingCadenceInRevPerMinute"),
        "training_load": pick(a, "activityTrainingLoad", "trainingLoad"),
        "aerobic_te": pick(a, "aerobicTrainingEffect"),
    }


# ------------------------------------------- Garmin Connect (unofficial) data
# Shapes verified against the garminconnect library's response models and
# test fixtures from other projects that parse real responses.

def _phrase(value: Any) -> str | None:
    """'PRODUCTIVE_1' -> 'Productive', 'NO_STATUS' -> 'No status'."""
    if not isinstance(value, str) or not value.strip():
        return None
    parts = value.strip().split("_")
    if len(parts) > 1 and parts[-1].isdigit():
        parts = parts[:-1]
    text = " ".join(parts).lower()
    return text[:1].upper() + text[1:]


def _vo2_of(block: Any) -> tuple[str | None, Any]:
    if not isinstance(block, dict):
        return None, None
    value = block.get("vo2MaxPreciseValue")
    if value is None:
        value = block.get("vo2MaxValue")
    return _date_of(block), _num(value)


def vo2max(obj: Any) -> Iterable[tuple[str, dict]]:
    """get_max_metrics_range: [{"generic": {calendarDate, vo2MaxPreciseValue, fitnessAge}, "cycling": {...}}]"""
    items = obj if isinstance(obj, list) else [obj] if isinstance(obj, dict) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        run_date, run_vo2 = _vo2_of(item.get("generic"))
        bike_date, bike_vo2 = _vo2_of(item.get("cycling"))
        if run_date and run_vo2 is not None:
            generic = item["generic"]
            yield run_date, _clean({"vo2max": run_vo2, "fitness_age": _num(generic.get("fitnessAge"))})
        if bike_date and bike_vo2 is not None:
            yield bike_date, {"vo2max_cycling": bike_vo2}


def readiness(obj: Any) -> Iterable[tuple[str, dict]]:
    """get_training_readiness_range: a list of snapshots, several per day.

    Score/level come from the morning (after-wake-up) snapshot, the way Garmin's
    morning report shows them. Recovery time comes from the latest snapshot of
    the day; it is in minutes, and REACHED_ZERO means fully recovered.
    """
    items = obj if isinstance(obj, list) else [obj] if isinstance(obj, dict) else []
    by_day: dict[str, list[dict]] = {}
    for s in items:
        if isinstance(s, dict) and _date_of(s):
            by_day.setdefault(_date_of(s), []).append(s)
    for day, snaps in by_day.items():
        snaps.sort(key=lambda s: str(s.get("timestampLocal") or s.get("timestamp") or ""))
        morning = next((s for s in snaps if s.get("inputContext") == "AFTER_WAKEUP_RESET"), snaps[0])
        latest = snaps[-1]
        recovery = _num(latest.get("recoveryTime"))
        if latest.get("recoveryTimeChangePhrase") == "REACHED_ZERO":
            recovery = 0
        yield day, _clean({
            "readiness_score": _num(morning.get("score")),
            "readiness_level": _phrase(morning.get("level")),
            "readiness_feedback": _phrase(morning.get("feedbackShort")),
            "recovery_min": recovery,
            "readiness_time": str(latest.get("timestampLocal") or latest.get("timestamp") or "")[:16].replace("T", " ") or None,
        })


def _primary(device_map: Any) -> dict | None:
    """Training status data is keyed by device id; prefer the primary training device."""
    if not isinstance(device_map, dict):
        return None
    entries = [v for v in device_map.values() if isinstance(v, dict)]
    if not entries:
        return None
    primary = [e for e in entries if e.get("primaryTrainingDevice") is True]
    pool = primary or entries
    return max(pool, key=lambda e: str(e.get("calendarDate") or ""))


def training_status(obj: Any) -> Iterable[tuple[str, dict]]:
    """get_training_status (aggregated): mostRecentTrainingStatus / TrainingLoadBalance / VO2Max."""
    if not isinstance(obj, dict):
        return
    status = _primary((obj.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData"))
    balance = _primary((obj.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap"))
    if status and _date_of(status):
        load = status.get("acuteTrainingLoadDTO") or {}
        yield _date_of(status), _clean({
            "training_status": _phrase(status.get("trainingStatusFeedbackPhrase")),
            "acute_load": _num(load.get("dailyTrainingLoadAcute")),
            "chronic_load": _num(load.get("dailyTrainingLoadChronic")),
            "acwr": _num(load.get("dailyAcuteChronicWorkloadRatio")),
            "acwr_status": _phrase(load.get("acwrStatus")),
            "load_balance": _phrase(balance.get("trainingBalanceFeedbackPhrase")) if balance else None,
        })
    vo2_date, vo2 = _vo2_of((obj.get("mostRecentVO2Max") or {}).get("generic"))
    if vo2_date and vo2 is not None:
        yield vo2_date, {"vo2max": vo2}


# ------------------------------------------- resting heart rate (any field name)
_RHR_EXACT = {"restingheartrate", "restingheartrateinbeatsperminute", "restingheartratevalue",
              "restinghr", "currentdayrestingheartrate", "rhr", "wellnessrestingheartrate"}


def resting_hr(record: Any, depth: int = 0) -> float | None:
    """Find today's resting heart rate in a record, whatever Garmin calls the field.
    Skips 7-day averages so the daily value is used."""
    if depth > 5:
        return None
    if isinstance(record, dict):
        for k, v in record.items():
            key = k.lower().replace("_", "")
            if key in _RHR_EXACT and not isinstance(v, (dict, list)):
                n = _num(v)
                if n and 25 <= n <= 140:
                    return n
        for k, v in record.items():
            key = k.lower()
            if "sevenday" in key or "avg" in key or "average" in key:
                continue
            if isinstance(v, (dict, list)):
                found = resting_hr(v, depth + 1)
                if found:
                    return found
    elif isinstance(record, list):
        for v in record[:50]:
            found = resting_hr(v, depth + 1)
            if found:
                return found
    return None


def heart_rate_day(obj: Any) -> Iterable[tuple[str, dict]]:
    """Per-day heart-rate summary (Personal API dailyHeartRate)."""
    for date, r in records(obj):
        row = _clean({
            "resting_hr": resting_hr(r),
            "min_hr": pick(r, "minHeartRate"),
            "max_hr": pick(r, "maxHeartRate"),
        })
        if row:
            yield date, row


def rhr_daily(obj: Any) -> Iterable[tuple[str, dict]]:
    """garminconnect get_rhr_daily: [{"calendarDate", "value"}]."""
    for item in obj if isinstance(obj, list) else []:
        if isinstance(item, dict) and _date_of(item):
            v = _num(item.get("value"))
            if v and 25 <= v <= 140:
                yield _date_of(item), {"resting_hr": v}


def race_predictions(obj: Any) -> Iterable[tuple[str, dict]]:
    """garminconnect get_race_predictions(daily): time5K / time10K / timeHalfMarathon / timeMarathon (seconds)."""
    for date, r in records(obj):
        row = _clean({
            "race_5k_s": _num(r.get("time5K")),
            "race_10k_s": _num(r.get("time10K")),
            "race_half_s": _num(r.get("timeHalfMarathon")),
            "race_marathon_s": _num(r.get("timeMarathon")),
        })
        if row.get("race_5k_s") and 600 <= row["race_5k_s"] <= 7200:
            yield date, row


def profile(obj: Any) -> dict:
    """Birth year and sex from a Garmin profile/settings response (for age-adjusted VO2 max)."""
    out: dict = {}
    birth = _deep(obj, "birthDate")
    if isinstance(birth, str) and len(birth) >= 4 and birth[:4].isdigit():
        out["birth_year"] = int(birth[:4])
    sex = _deep(obj, "gender") or _deep(obj, "sex")
    if isinstance(sex, str) and sex.strip():
        out["sex"] = "female" if sex.strip().lower().startswith("f") else "male" if sex.strip().lower().startswith("m") else None
    return {k: v for k, v in out.items() if v}
