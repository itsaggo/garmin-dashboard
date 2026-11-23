"""v1.5: training plan — reading pasted plans, checking days off, and how the coach uses it."""
import json
from datetime import date, timedelta

import pytest

from app import ai, main, notify, plan, prefs
from tests.helpers import JSON, PASSWORD
from tests.test_v12 import _Mock, mock_ai, signed_in  # noqa: F401  (fixtures)

T = date(2026, 11, 23)   # a Monday


# ------------------------------------------------------------------ reading a pasted plan
def _days(text, **kw):
    return [(d["date"], d["kind"], d["text"]) for d in plan.parse(text, today=T, **kw)["days"]]


def test_dated_lines_in_many_spellings():
    got = _days("""Mon Nov 23 – Rest
Tue Nov 24: Easy 4 mi
Wed 11/25 - Tempo 5 mi (2 @ threshold)
2026-11-26  6 x 800m intervals
Friday, November 27 — Rest or cross-train 30 min
Sat Nov 28 | Long run 10-11 miles
- Sunday Nov 29: Recovery 3 mi + strides
30 Nov 2026: Rest""")
    assert got == [("2026-11-23", "rest", "Rest"), ("2026-11-24", "easy", "Easy 4 mi"),
                   ("2026-11-25", "tempo", "Tempo 5 mi (2 @ threshold)"), ("2026-11-26", "intervals", "6 x 800m intervals"),
                   ("2026-11-27", "rest", "Rest or cross-train 30 min"), ("2026-11-28", "long", "Long run 10-11 miles"),
                   ("2026-11-29", "easy", "Recovery 3 mi + strides"), ("2026-11-30", "rest", "Rest")]


def test_table_with_a_date_column():
    got = _days("""| Date | Day | Workout | Distance |
|---|---|---|---|
| Nov 30 | Mon | Rest | — |
| Dec 1 | Tue | Easy | 5 mi |
| 12/2 | Wed | Intervals 5x1K | 6 mi |""")
    assert got == [("2026-11-30", "rest", "Rest"), ("2026-12-01", "easy", "Easy · 5 mi"),
                   ("2026-12-02", "intervals", "Intervals 5x1K · 6 mi")]


def test_week_grid_needs_week_one_or_dates():
    grid = """| Week | Mon | Tue | Wed | Thu | Fri | Sat | Sun | Total |
|---|---|---|---|---|---|---|---|---|
| 1 | Rest | 4 mi easy | 5 mi tempo | 4 mi easy | Rest | 10 mi long | 3 mi recovery | 26 |
| 2 | Rest | 5 mi easy | 6 x 800 | 4 mi easy | Rest | 11 mi long | 3 mi recovery | 29 |"""
    with pytest.raises(plan.PlanError, match="week 1 starts"):
        plan.parse(grid, today=T)
    got = _days(grid, week1=date(2026, 11, 23))
    assert len(got) == 14 and got[0] == ("2026-11-23", "rest", "Rest") and got[5] == ("2026-11-28", "long", "10 mi long")
    assert got[9] == ("2026-12-02", "intervals", "6 x 800") and got[13] == ("2026-12-06", "easy", "3 mi recovery")
    dated = grid.replace("| 1 |", "| Nov 23 |").replace("| 2 |", "| Nov 30 |")
    assert _days(dated) == got                                     # a week's date in the first column works too


def test_week_headings_count_on_from_the_first_dated_week():
    got = _days("""## Week 1 (Nov 23 – Nov 29)
- Mon: Rest
- Tue: Easy 4 miles
**Week 2**
- Monday: Rest
- Thursday — Hills 6 x 90 sec
Week 3
Sat: Long 12 mi""")
    assert got == [("2026-11-23", "rest", "Rest"), ("2026-11-24", "easy", "Easy 4 miles"), ("2026-11-30", "rest", "Rest"),
                   ("2026-12-03", "intervals", "Hills 6 x 90 sec"), ("2026-12-12", "long", "Long 12 mi")]


def test_year_rolls_over_and_odd_lines_are_reported():
    r = plan.parse("Dec 30 Easy 4 mi\nDec 31 Rest\nJan 1 Long 12 mi\nRemember to hydrate!\n\n", today=T)
    assert [d["date"] for d in r["days"]] == ["2026-12-30", "2026-12-31", "2027-01-01"]
    assert r["skipped"] == ["Remember to hydrate!"] and r["skipped_count"] == 1


def test_two_sessions_on_one_day_are_joined_and_limits_apply():
    assert _days("Nov 24 Easy 4 mi\nNov 24 Strength 20 min") == [("2026-11-24", "easy", "Easy 4 mi + Strength 20 min")]
    with pytest.raises(plan.PlanError, match="too long"):
        plan.parse("x" * (plan.MAX_TEXT + 1), today=T)


@pytest.mark.parametrize("text,kind,lo_km,mins", [
    ("Easy 4 mi", "easy", 6.437, None), ("Long run 10-11 miles", "long", 16.093, None), ("8 km tempo", "tempo", 8.0, None),
    ("Intervals 5x1K · 6 mi", "intervals", 9.656, None), ("4 x 800m at 5k pace", "intervals", None, None),
    ("Rest", "rest", None, None), ("Off", "rest", None, None), ("Bike 20 km", "cross", 20.0, None),
    ("Strength 30 min", "cross", None, 30), ("Half marathon race", "race", None, None), ("3 mi at race pace", "tempo", 4.828, None),
    ("Run 45 min", "run", None, 45), ("Stretch and foam roll", "other", None, None),
])
def test_reading_a_session(text, kind, lo_km, mins):
    s = plan.read_session(text)
    assert s["kind"] == kind and s["minutes"] == mins
    assert (s["dist_lo_m"] / 1000 if s["dist_lo_m"] else None) == (pytest.approx(lo_km, abs=0.01) if lo_km else None)


# ------------------------------------------------------------------ checking days off
def _act(day, kind="running", km=0.0, mins=0):
    return {"start_local": f"{day} 07:00:00", "type": kind, "distance_m": km * 1000, "duration_s": mins * 60, "moving_s": mins * 60}


@pytest.mark.parametrize("session,acts,day,want", [
    ("Easy 4 mi", [_act("2026-11-20", km=6.5)], "2026-11-20", "done"),
    ("Easy 4 mi", [_act("2026-11-20", km=4.0)], "2026-11-20", "partial"),
    ("Easy 4 mi", [], "2026-11-20", "missed"),
    ("Easy 4 mi", [], "2026-11-23", "today"),
    ("Rest", [], "2026-11-20", "rested"),
    ("Rest", [_act("2026-11-20", km=5)], "2026-11-20", "extra"),
    ("Rest", [], "2026-11-23", "today"),
    ("Run 45 min", [_act("2026-11-20", km=7, mins=44)], "2026-11-20", "done"),
    ("Strength 30 min", [_act("2026-11-20", kind="strength_training", mins=30)], "2026-11-20", "done"),
    ("6 x 800", [_act("2026-11-20", km=8)], "2026-11-20", "done"),
    ("Long 12 mi", [], "2026-11-25", "upcoming"),
])
def test_check_off(session, acts, day, want):
    e = {"date": day, **plan.read_session(session)}
    assert plan.status(e, acts, date.fromisoformat(day), T)["status"] == want


def _seed_week(db, today=None):
    today = today or date.today()
    mon = today - timedelta(days=today.weekday())
    sessions = ["Rest", "Easy 4 mi", "Tempo 5 mi", "Easy 4 mi", "Rest", "Long 10 mi", "Recovery 3 mi"]
    plan.save(db, [{"date": (mon + timedelta(days=i)).isoformat(), **plan.read_session(t)} for i, t in enumerate(sessions)]
              + [{"date": (mon + timedelta(days=7 + i)).isoformat(), **plan.read_session(t)} for i, t in enumerate(sessions)])
    return mon


def test_week_view_and_api(signed_in):
    mon = _seed_week(main.db)
    if date.today() > mon + timedelta(days=1):    # Tuesday's run is in the past: give it one
        main.db.upsert_activity(7001, {**_act((mon + timedelta(days=1)).isoformat(), km=6.6), "name": "Run"}, {})
    w = signed_in.get("/api/plan/week").json()
    assert w["has_plan"] and len(w["days"]) == 7 and w["days"][0]["date"] == mon.isoformat()
    assert w["today"]["date"] == date.today().isoformat() and w["planned_m"] > 0
    if date.today() > mon + timedelta(days=1):
        assert w["days"][1]["status"] == "done" and w["done_m"] == 6600
    full = signed_in.get("/api/plan").json()
    assert full["info"]["days"] == 14 and full["text"].splitlines()[1].endswith("Easy 4 mi") and len(full["days"]) == 14


def test_plan_endpoints_need_sign_in_and_header(signed_in):
    from tests.helpers import client as new_client
    anon = new_client()
    assert anon.get("/api/plan/week").status_code == 401 and anon.post("/api/plan/preview", json={"text": "x"}, headers=JSON).status_code == 401
    r = signed_in.post("/api/plan", json={"text": "Nov 24 Easy 4 mi"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 403


def test_save_preview_clear(signed_in):
    p = signed_in.post("/api/plan/preview", json={"text": "Mon Dec 7 Rest\nTue Dec 8 Easy 5 mi"}, headers=JSON).json()
    assert [d["kind"] for d in p["days"]] == ["rest", "easy"]
    assert signed_in.get("/api/plan").json()["info"]["days"] == 0     # preview saves nothing
    bad = signed_in.post("/api/plan", json={"text": "just some notes"}, headers=JSON)
    assert bad.status_code == 400 and "No dated sessions" in bad.json()["detail"]
    week_only = signed_in.post("/api/plan", json={"text": "Week 1\nMon: Rest\nTue: Easy 4 mi"}, headers=JSON)
    assert week_only.status_code == 400 and "week 1 starts" in week_only.json()["detail"]
    ok = signed_in.post("/api/plan", json={"text": "Week 1\nMon: Rest\nTue: Easy 4 mi", "week1": "2026-12-07"}, headers=JSON).json()
    assert ok["saved"] == 2 and ok["info"]["first"] == "2026-12-07"
    assert signed_in.post("/api/plan", json={"text": "x" * (plan.MAX_TEXT + 1)}, headers=JSON).status_code == 422
    signed_in.post("/api/plan/clear", headers=JSON)
    assert signed_in.get("/api/plan").json()["info"]["days"] == 0


# ------------------------------------------------------------------ the coach
def _use_claude(c):
    c.post("/api/ai/key", json={"provider": "claude", "key": "sk-good-key-123"}, headers=JSON)
    c.post("/api/settings", json={"values": {"ai_provider": "claude", "ai_model": "claude-sonnet-5-5"}}, headers=JSON)


def test_coach_gets_the_plan_and_the_rules(signed_in, mock_ai):
    _use_claude(signed_in)
    signed_in.post("/api/insight/refresh", headers=JSON)
    sent = json.dumps(_Mock.seen[-1][2])
    assert "training_plan" not in sent and "plan_change" not in sent   # no plan, no plan talk
    _seed_week(main.db)
    signed_in.post("/api/insight/refresh", headers=JSON)
    body = _Mock.seen[-1][2]
    prompt = body["messages"][0]["content"]
    facts = json.loads(prompt.split("Facts (JSON):\n", 1)[1].split("\n\n", 1)[0])
    tp = facts["training_plan"]
    today = date.today()
    expected = ["Rest", "Easy 4 mi", "Tempo 5 mi", "Easy 4 mi", "Rest", "Long 10 mi", "Recovery 3 mi"][today.weekday()]
    assert tp["today"]["session"] == expected and len(tp["next_7_days"]) == 7 and len(tp["last_7_days"]) == 7
    assert plan.RULES in body["system"] and '"plan_change"' in prompt
    signed_in.post("/api/insight/ask", json={"question": "What's on today?"}, headers=JSON)
    assert plan.RULES in _Mock.seen[-1][2]["system"]


def _suggest(moves, reason="Low HRV after a hard weekend."):
    _Mock.reply = json.dumps({"headline": "Easy does it", "today": "Swap today's tempo for an easy run.", "insights": [],
                              "focus": "Recover.", "plan_change": {"reason": reason, "moves": moves}})


def test_suggested_change_apply_and_undo(signed_in, mock_ai):
    _use_claude(signed_in)
    _seed_week(main.db)
    today, tmw = date.today(), date.today() + timedelta(days=1)
    before = {e["date"]: e["text"] for e in plan.entries(main.db)}
    _suggest([{"date": today.isoformat(), "session": "Easy 3 mi"}, {"date": tmw.isoformat(), "session": "Tempo 5 mi"},
              {"date": (today + timedelta(days=30)).isoformat(), "session": "Rest"},       # too far ahead: dropped
              {"date": "not a date", "session": "Rest"}, "junk"])
    ins = signed_in.post("/api/insight/refresh", headers=JSON).json()["insight"]
    ch = ins["plan_change"]
    assert ch["state"] == "pending" and [m["date"] for m in ch["moves"]] == [today.isoformat(), tmw.isoformat()]
    assert ch["moves"][0]["was"] == before[today.isoformat()] and ch["reason"].startswith("Low HRV")
    assert {e["date"]: e["text"] for e in plan.entries(main.db)} == before          # nothing changes on its own
    r = signed_in.post("/api/plan/change", json={"action": "apply"}, headers=JSON).json()
    assert r["insight"]["plan_change"]["state"] == "applied" and r["week"]["can_undo"] is True
    after = {e["date"]: e for e in plan.entries(main.db)}
    assert after[today.isoformat()]["text"] == "Easy 3 mi" and after[today.isoformat()]["kind"] == "easy"
    assert after[today.isoformat()]["source"] == "coach" and after[tmw.isoformat()]["kind"] == "tempo"
    assert signed_in.post("/api/plan/change", json={"action": "apply"}, headers=JSON).status_code == 400   # only once
    u = signed_in.post("/api/plan/change", json={"action": "undo"}, headers=JSON).json()
    assert {e["date"]: e["text"] for e in plan.entries(main.db)} == before and u["insight"]["plan_change"]["state"] == "pending"
    assert signed_in.post("/api/plan/change", json={"action": "undo"}, headers=JSON).status_code == 400
    d = signed_in.post("/api/plan/change", json={"action": "dismiss"}, headers=JSON).json()
    assert d["insight"]["plan_change"]["state"] == "dismissed"
    assert signed_in.post("/api/plan/change", json={"action": "explode"}, headers=JSON).status_code == 422


def test_suggestion_is_ignored_without_a_plan_or_when_it_changes_nothing(signed_in, mock_ai):
    _use_claude(signed_in)
    _suggest([{"date": date.today().isoformat(), "session": "Rest"}])
    assert signed_in.post("/api/insight/refresh", headers=JSON).json()["insight"]["plan_change"] is None
    _seed_week(main.db)
    same = plan.entries(main.db, date.today(), date.today())[0]["text"]
    _suggest([{"date": date.today().isoformat(), "session": same.upper()}])
    assert signed_in.post("/api/insight/refresh", headers=JSON).json()["insight"]["plan_change"] is None
    _suggest([{"date": date.today().isoformat(), "session": "<img src=x onerror=alert(1)> Easy 2 mi"}])
    m = signed_in.post("/api/insight/refresh", headers=JSON).json()["insight"]["plan_change"]["moves"][0]
    assert "<" not in m["session"] and ">" not in m["session"]


def test_yesterdays_suggestion_cant_be_applied(signed_in, mock_ai):
    _use_claude(signed_in)
    _seed_week(main.db)
    _suggest([{"date": date.today().isoformat(), "session": "Easy 2 mi"}])
    signed_in.post("/api/insight/refresh", headers=JSON)
    ins = json.loads(main.db.get_meta("insight"))
    ins["date"] = (date.today() - timedelta(days=1)).isoformat()
    main.db.set_meta("insight", json.dumps(ins))
    assert signed_in.post("/api/plan/change", json={"action": "apply"}, headers=JSON).status_code == 400


def test_plan_outside_the_coachs_window_is_left_out(signed_in):
    old = date.today() - timedelta(days=40)
    plan.save(main.db, [{"date": (old + timedelta(days=i)).isoformat(), **plan.read_session("Easy 3 mi")} for i in range(10)])
    assert plan.coach_facts(main.db) is None
    w = signed_in.get("/api/plan/week").json()
    assert w["has_plan"] and w["ended"] is True


# ------------------------------------------------------------------ elsewhere
def test_discord_morning_shows_todays_session(signed_in, monkeypatch):
    sent = {}
    monkeypatch.setattr(notify, "send", lambda s, kind, title, lines, fields=None, **kw: sent.update(lines=lines))
    _seed_week(main.db)
    notify.morning(main.settings, main.db)
    today_text = plan.entries(main.db, date.today(), date.today())[0]["text"]
    assert sent["lines"][0] == f"**Plan today:** {today_text}"


def test_export_and_delete(signed_in):
    _seed_week(main.db)
    exp = signed_in.get("/api/export").json()
    assert len(exp["training_plan"]) == 14
    r = signed_in.post("/api/data/delete", json={"password": PASSWORD, "scope": "garmin_data"}, headers=JSON)
    assert r.status_code == 200 and plan.info(main.db)["days"] == 14          # your plan isn't Garmin data
    r = signed_in.post("/api/data/delete", json={"password": PASSWORD, "scope": "everything"}, headers=JSON)
    assert r.status_code == 200 and plan.info(main.db)["days"] == 0


def test_layout_puts_the_plan_under_the_race_strip():
    saved = [{"id": "coach", "section": "main", "w": 8, "h": 8}, {"id": "race", "section": "main", "w": 12, "h": 2},
             {"id": "vo2", "section": "main", "w": 3, "h": 4}]
    ids = [i["id"] for i in prefs.complete_layout(saved)]
    assert ids[:4] == ["coach", "race", "plan", "vo2"]
    again = prefs.complete_layout([{"id": "plan", "section": "hidden", "w": 6, "h": 2}, *saved])
    assert [i for i in again if i["id"] == "plan"] == [{"id": "plan", "section": "hidden", "w": 6, "h": 2}]
