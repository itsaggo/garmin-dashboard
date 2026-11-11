"""v1.3: weather box, heat-graded paces, fitness score removed."""
import json
from datetime import date, timedelta

import pytest

from app import fitness, main, notify, prefs, weather
from app.db import Database
from tests.helpers import JSON, client as new_client, login, make_account, reset


def _forecast(now="2026-11-11T12:15", start="2026-11-11T00:00", temp=84.0, dew=73.0):
    """An Open-Meteo-shaped reply: 48 hours, coolest at 6 a.m. tomorrow."""
    d0 = date.fromisoformat(start[:10])
    times, temps, dews = [], [], []
    for i in range(48):
        day, hour = d0 + timedelta(days=i // 24), i % 24
        times.append(f"{day.isoformat()}T{hour:02d}:00")
        cool = 0 if 5 <= hour <= 9 else 10
        temps.append(70.0 + cool + (2 if hour == 6 and i >= 24 else 0) - (3 if hour == 6 and i >= 24 else 0))
        dews.append(66.0 + cool / 2)
    return {
        "current": {"time": now, "temperature_2m": temp, "dew_point_2m": dew, "relative_humidity_2m": 70,
                    "apparent_temperature": 90, "weather_code": 2, "wind_speed_10m": 9},
        "hourly": {"time": times, "temperature_2m": temps, "dew_point_2m": dews,
                   "precipitation_probability": [10] * 48, "weather_code": [1] * 48},
    }


@pytest.fixture()
def c():
    reset()
    cl = new_client()
    make_account()
    login(cl)
    return cl


@pytest.fixture()
def calls(monkeypatch):
    seen = []

    def fake(url, params):
        seen.append((url, dict(params)))
        if "geocoding" in url:
            return {"results": [
                {"name": "Austin", "admin1": "Texas", "country": "United States", "latitude": 30.26715, "longitude": -97.74306},
                {"name": "<b>Bad</b>", "latitude": "x", "longitude": 1},  # dropped: not a number
                {"name": "Austin", "admin1": "Minnesota", "country": "United States", "latitude": 43.6666, "longitude": -92.9746},
            ]}
        return _forecast()
    monkeypatch.setattr(weather, "_get_json", fake)
    return seen


# ------------------------------------------------------------------ heat math
@pytest.mark.parametrize("t,d,pct", [(50, 40, 0.0), (60, 50, 0.5), (70, 60, 2.0), (80, 70, 4.5), (85, 70, 5.2), (95, 90, 10.0)])
def test_heat_rule(t, d, pct):
    assert weather.heat_pct(t, d) == pytest.approx(pct, abs=0.06)


def test_heat_grades():
    assert weather.heat_grade(50, 40)["grade"] == "None"
    assert weather.heat_grade(84, 73)["grade"] == "Heavy" and weather.heat_grade(84, 73)["sum"] == 157
    g = weather.heat_grade(98, 88)
    assert g["grade"] == "Extreme" and g["too_hot"] is True
    assert weather.heat_pct(None, 60) is None and weather.heat_grade(80, None) is None
    assert weather.adjust(300, 5) == 315 and weather.adjust(300, None) is None


def test_training_paces_from_5k_and_race(c):
    db = main.db
    db.upsert_training(date.today().isoformat(), {"race_5k_s": 1428})  # 23:48
    paces = {p["name"]: p["s_per_km"] for p in fitness.training_paces(db)}
    per_mile = {k: v * 1.609344 for k, v in paces.items()}
    assert 600 <= per_mile["Easy"] <= 640       # about 10:00–10:40 per mile
    assert 480 <= per_mile["Threshold"] <= 510  # about 8:00–8:30 per mile
    assert "Race goal" not in paces
    prefs.update(db, {"race_enabled": True, "race_date": (date.today() + timedelta(days=3)).isoformat(),
                      "race_distance": "half", "race_goal_time": "1:52:00"})
    goal = next(p for p in fitness.training_paces(db) if p["name"] == "Race goal")
    assert goal["s_per_km"] == pytest.approx(6720 / 21.0975, abs=0.1)


# ------------------------------------------------------------------ API
def test_weather_endpoint_grades_paces_and_caches(c, calls):
    main.db.upsert_training(date.today().isoformat(), {"race_5k_s": 1428})
    r = c.get("/api/weather")
    assert r.status_code == 200
    w = r.json()
    assert w["ok"] and w["place"].startswith("Houston")  # default place
    assert w["current"]["sky"] == "Partly cloudy" and w["heat"]["grade"] == "Heavy"
    assert w["hours"][0]["time"] == "2026-11-11T12:00" and len(w["hours"]) == weather.HOURS_SHOWN
    assert w["best"]["time"] == "2026-11-12T06:00" and w["best"]["pct"] < w["heat"]["pct"]
    easy = next(p for p in w["paces"] if p["name"] == "Easy")
    assert easy["now_s_per_km"] == pytest.approx(easy["s_per_km"] * (1 + w["heat"]["pct"] / 100), abs=0.2)
    assert easy["s_per_km"] < easy["best_s_per_km"] < easy["now_s_per_km"]
    # Only the rounded place is sent, in °F and mph.
    url, params = calls[0]
    assert url == weather.FORECAST_URL and params["latitude"] == "29.76" and params["longitude"] == "-95.37"
    assert params["temperature_unit"] == "fahrenheit"
    c.get("/api/weather")
    assert len(calls) == 1  # second load came from the cache


def test_weather_needs_sign_in(calls):
    reset()
    make_account()
    assert new_client().get("/api/weather").status_code == 401
    assert new_client().get("/api/weather/places", params={"q": "Austin"}).status_code == 401
    assert not calls


def test_weather_errors_fall_back_to_cache(c, calls, monkeypatch):
    c.get("/api/weather")
    cache = json.loads(main.db.get_meta("weather_cache"))
    cache["fetched"] -= 2 * 3600  # make it old enough to refetch
    main.db.set_meta("weather_cache", json.dumps(cache))

    def down(url, params):
        raise weather.WeatherError("Couldn't reach the weather service (URLError).")
    monkeypatch.setattr(weather, "_get_json", down)
    w = c.get("/api/weather").json()
    assert w["ok"] and "Couldn't reach" in w["error"]  # old numbers, with a note
    main.db.delete_meta("weather_cache")
    w = c.get("/api/weather").json()
    assert not w["ok"] and "Couldn't reach" in w["error"]


def test_bad_forecast_values_are_dropped(c, monkeypatch):
    raw = _forecast(temp="hot", dew=1e9)
    raw["hourly"]["time"][3] = "<script>"
    monkeypatch.setattr(weather, "_get_json", lambda u, p: raw)
    w = c.get("/api/weather").json()
    assert w["ok"] and w["current"]["temp_f"] is None and w["current"]["dew_f"] is None and w["heat"] is None
    assert all(h["time"] != "<script>" for h in w["hours"])


def test_place_search_and_save(c, calls):
    r = c.get("/api/weather/places", params={"q": "  Austin "})
    assert r.status_code == 200
    found = r.json()["places"]
    assert [p["label"] for p in found] == ["Austin, Texas, United States", "Austin, Minnesota, United States"]
    assert found[0]["lat"] == 30.27 and found[0]["lon"] == -97.74
    assert c.get("/api/weather/places", params={"q": "a"}).status_code == 400
    r = c.post("/api/settings", json={"values": {"weather_place": found[0]["label"], "weather_lat": 30.26715,
                                                  "weather_lon": -97.74306}}, headers=JSON)
    assert r.status_code == 200 and r.json()["prefs"]["weather_lat"] == 30.27


@pytest.mark.parametrize("values", [{"weather_lat": 91}, {"weather_lon": -181}, {"weather_lat": True},
                                    {"weather_lat": "north"}, {"weather_lat": float("nan")}, {"weather_place": "a\x07b"},
                                    {"weather_place": "x" * 121}])
def test_bad_places_rejected(values):
    with pytest.raises(prefs.PrefError):
        prefs.validate(values)


def test_cleared_place_turns_weather_off(c, calls):
    prefs.update(main.db, {"weather_place": "", "weather_lat": None, "weather_lon": None})
    w = c.get("/api/weather").json()
    assert not w["ok"] and "Settings" in w["error"] and not calls


# ------------------------------------------------------------------ fitness score is gone
def test_old_layout_gets_weather_in_the_fitness_spot():
    old = [{"id": "coach", "section": "main", "w": 8, "h": 6}, {"id": "fitness", "section": "main", "w": 5, "h": 7},
           {"id": "fitness_trend", "section": "details", "w": 6, "h": 4}]
    lay = prefs.complete_layout(old)
    assert lay[1] == {"id": "weather", "section": "main", "w": 5, "h": 7}
    assert not any(i["id"] in ("fitness", "fitness_trend") for i in lay)
    assert {"fitness", "fitness_trend"}.isdisjoint(prefs.WIDGETS)
    with pytest.raises(prefs.PrefError):
        prefs.validate_layout([{"id": "fitness", "section": "main", "w": 4, "h": 4}])


def test_coach_and_discord_get_heat_not_score(c, calls, monkeypatch):
    assert "fitness_score" not in fitness.facts(main.db)
    assert fitness.facts(main.db)["weather_now"] is None  # nothing cached yet, and it never fetches by itself
    assert not calls
    c.get("/api/weather")
    f = fitness.facts(main.db)
    assert f["weather_now"]["heat_grade"] == "Heavy" and f["weather_now"]["slow_down_pct"] > 4
    names = [n for n, _ in notify._metrics(main.db)]
    assert "Heat" in names and "Fitness score" not in names
    assert "Fitness score" not in json.dumps(f)


def test_weather_cache_for_other_place_is_ignored(c, calls):
    c.get("/api/weather")
    prefs.update(main.db, {"weather_place": "Austin, Texas, United States", "weather_lat": 30.27, "weather_lon": -97.74})
    assert weather.summary(main.db) is None  # cached numbers are for Houston
    w = c.get("/api/weather").json()
    assert w["ok"] and calls[-1][1]["latitude"] == "30.27"


def test_dashboard_has_no_fitness_score(c):
    js = (main.STATIC / "app.js").read_text()
    assert "fitness_trend" not in js and "S.ov.today" not in js
    assert "weather" in c.get("/api/settings").json()["widgets"]


def test_coach_summary_has_no_place_name(c, calls):
    c.get("/api/weather")
    assert "Houston" not in json.dumps(fitness.facts(main.db))
