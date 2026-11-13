"""v1.2: race switch, layout, resting HR, VO2 norms, 5K, AI providers."""
import json
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import ai, extract, fitness, main, prefs
from app.db import Database
from tests.helpers import JSON, client as new_client, login, make_account, reset

TODAY = date(2026, 11, 5)


@pytest.fixture()
def signed_in():
    reset()
    c = new_client()
    make_account()
    login(c)
    return c


# ------------------------------------------------------------------ race switch & migrations
def test_race_switch_hides_race(tmp_path):
    db = Database(tmp_path / "r.db")
    prefs.update(db, {"race_name": "NOLA", "race_date": "2026-11-14", "race_goal_time": "2:05:00"})
    assert fitness.race_card(db, TODAY) is None  # switch off by default
    prefs.update(db, {"race_enabled": True})
    card = fitness.race_card(db, TODAY)
    assert card["days_to_go"] == 9 and card["goal"] == "2:05:00" and card["goal_pace"] == "9:32/mi"


def test_v11_prefs_migrate(tmp_path):
    db = Database(tmp_path / "m.db")
    db.set_meta("prefs", json.dumps({"race_date": "2026-11-14", "ai_enabled": True,
                                     "ollama_url": "http://10.0.0.2:11434", "ollama_model": "llama3.1:8b"}))
    p = prefs.get_all(db)
    assert p["race_enabled"] is True
    assert p["ai_provider"] == "ollama" and p["ai_model"] == "llama3.1:8b"


# ------------------------------------------------------------------ layout
def test_layout_validation_and_completion():
    lay = prefs.validate_layout([{"id": "vo2", "section": "hidden", "w": 12, "h": 2}])
    ids = [i["id"] for i in lay]
    assert ids[0] == "vo2" and set(ids) == set(prefs.WIDGETS)  # missing boxes added back
    for bad in ([{"id": "nope", "section": "main", "w": 4, "h": 4}],
                [{"id": "vo2", "section": "sidebar", "w": 4, "h": 4}],
                [{"id": "vo2", "section": "main", "w": 13, "h": 4}],
                [{"id": "vo2", "section": "main", "w": True, "h": 4}],
                [{"id": "vo2", "section": "main", "w": 4, "h": 4}] * 2, "x"):
        with pytest.raises(prefs.PrefError):
            prefs.validate_layout(bad)


def test_layout_saved_via_api(signed_in):
    lay = [{"id": "five_k", "section": "main", "w": 12, "h": 3}, {"id": "coach", "section": "hidden", "w": 6, "h": 6}]
    r = signed_in.post("/api/settings", json={"values": {"layout": lay}}, headers=JSON)
    assert r.status_code == 200
    st = signed_in.get("/api/status").json()["layout"]
    assert st[0] == lay[0] and st[1] == lay[1] and len(st) == len(prefs.WIDGETS)


# ------------------------------------------------------------------ resting heart rate
@pytest.mark.parametrize("record,expected", [
    ({"calendarDate": "2026-11-05", "restingHeartRate": 52}, 52),
    ({"calendarDate": "2026-11-05", "restingHeartRateValue": 53}, 53),
    ({"calendarDate": "2026-11-05", "heartRate": {"resting_hr": 54}}, 54),
    ({"calendarDate": "2026-11-05", "lastSevenDaysAvgRestingHeartRate": 50, "restingHeartRate": 55}, 55),
    ({"calendarDate": "2026-11-05", "lastSevenDaysAvgRestingHeartRate": 50}, None),
    ({"calendarDate": "2026-11-05", "restingHeartRate": 0}, None),
])
def test_resting_hr_any_field(record, expected):
    assert extract.resting_hr(record) == expected


def test_personal_api_fills_missing_rhr(tmp_path):
    from app import config, sync
    db = Database(tmp_path / "h.db")
    for i in range(3):
        db.upsert_daily((TODAY - timedelta(days=i)).isoformat(), {"steps": 100})
    calls = []

    class Client:
        def get_daily_heart_rate(self, d):
            calls.append(d)
            return {"calendarDate": d, "restingHeartRate": 49, "maxHeartRate": 170, "heartRateValues": [[1, 2]] * 10}

    g = sync.GarminSync.__new__(sync.GarminSync)
    g.s, g.db, g.client, g.notes = config.load(), db, Client(), []
    g.call = lambda fn, *a, **kw: fn(*a, **kw)
    g.fill_resting_hr(TODAY - timedelta(days=5), TODAY)
    assert sorted(calls, reverse=True) == calls and len(calls) == 3  # newest first, only missing days
    assert all(r["resting_hr"] == 49 for r in db.rows_since("daily", "2000-01-01"))
    assert "heartRateValues" not in db.raw_latest("heart_rate")["body"]  # big arrays not stored
    calls.clear()
    g.fill_resting_hr(TODAY - timedelta(days=5), TODAY)
    assert calls == []  # nothing missing any more


def test_connect_stores_predictions_rhr_profile(tmp_path, monkeypatch):
    import garminconnect
    from app import config, connect
    from tests.test_connect import FakeGarmin
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONNECT_BACKFILL_DAYS", "7")
    monkeypatch.setattr(garminconnect, "Garmin", FakeGarmin)
    monkeypatch.setattr(connect.time, "sleep", lambda s: None)
    FakeGarmin.calls, FakeGarmin.mode = [], "ok"
    s = config.load()
    config.ensure_private_dir(s.connect_token_dir)
    s.connect_token_file.write_text('{"di_token": "t"}')
    db = Database(s.db_path)
    msg = connect.ConnectSync(s, db).run()
    assert "FAILED" not in msg
    today = date.today().isoformat()
    t = {r["date"]: r for r in db.rows_since("training", "2000-01-01")}
    d = {r["date"]: r for r in db.rows_since("daily", "2000-01-01")}
    assert t[today]["race_5k_s"] == 1500 and d[today]["resting_hr"] == 51
    assert json.loads(db.get_meta("profile")) == {"birth_year": 1996, "sex": "male"}


# ------------------------------------------------------------------ VO2, 5K, facts
def _seed(db, days=70, rhr=52, hrv=60, vo2=48.0):
    for i in range(days):
        d = (TODAY - timedelta(days=i)).isoformat()
        db.upsert_daily(d, {"resting_hr": rhr, "hrv_last_night": hrv, "sleep_score": 80, "sleep_s": 7.5 * 3600,
                            "bb_high": 85, "steps": 11000, "step_goal": 10000,
                            "intensity_moderate": 30, "intensity_vigorous": 10})
        db.upsert_training(d, {"vo2max": vo2, "readiness_score": 70})
    for i in range(1, days, 2):
        d = (TODAY - timedelta(days=i)).isoformat()
        db.upsert_activity(500 + i, {"start_local": f"{d} 06:30:00", "type": "running", "distance_m": 6000,
                                     "duration_s": 6 * 360}, {})


def test_vo2_norms_and_vdot():
    assert fitness.vo2_score(61, 25, "male") == pytest.approx(95)
    assert fitness.vo2_score(33, 25, "male") == pytest.approx(10)
    assert fitness.vo2_score(45, 25, "female") > fitness.vo2_score(45, 25, "male")
    assert 44 < fitness.vdot_from_race(5000, 22 * 60) < 45  # 22:00 5K ≈ VDOT 44.5 (Daniels tables)


def test_overview_with_empty_db(tmp_path):
    ov = fitness.overview(Database(tmp_path / "e.db"), 30, TODAY)
    assert ov["five_k"]["seconds"] is None and len(ov["five_k"]["series"]) == 30 and ov["vo2"]["value"] is None
    assert "today" not in ov and "history" not in ov  # the fitness score is gone


def test_five_k_prefers_garmin(tmp_path):
    db = Database(tmp_path / "k.db")
    _seed(db)
    data = fitness.Data(db, TODAY, 60)
    secs, src = fitness.five_k(data, TODAY)
    assert src == "runs" and 1500 < secs < 2100
    db.upsert_training(TODAY.isoformat(), {"race_5k_s": 1450})
    assert fitness.five_k(fitness.Data(db, TODAY, 60), TODAY) == (1450, "garmin")


def test_facts_have_no_personal_identifiers(signed_in):
    _seed(main.db, days=10)
    f = json.dumps(fitness.facts(main.db))
    assert "adam" not in f.lower() and "@" not in f


# ------------------------------------------------------------------ AI providers (mock servers)
class _Mock(BaseHTTPRequestHandler):
    seen: list = []
    reply = "{}"

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth_ok(self):
        h = self.headers
        return (h.get("x-api-key") == "sk-good-key-123" or h.get("Authorization") == "Bearer sk-good-key-123"
                or h.get("x-goog-api-key") == "sk-good-key-123")

    def do_GET(self):
        if "/api/tags" in self.path:
            return self._send(200, {"models": [{"name": "llama3.1:8b"}]})
        if not self._auth_ok():
            return self._send(401, {"error": {"message": "invalid x-api-key"}})
        if self.path.startswith("/v1/models"):
            return self._send(200, {"data": [{"id": "claude-sonnet-5-5"}, {"id": "claude-haiku-5-5"},
                                             {"id": "gpt-5-mini"}, {"id": "text-embedding-3-small"}, {"id": "gpt-audio"}]})
        if self.path.startswith("/v1beta/models"):
            return self._send(200, {"models": [{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                                               {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]}]})
        self._send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Mock.seen.append((self.path, dict(self.headers), body))
        if "/api/chat" not in self.path and not self._auth_ok():
            return self._send(401, {"error": {"message": "bad key"}})
        text = _Mock.reply
        if self.path == "/v1/messages":
            return self._send(200, {"content": [{"type": "text", "text": text}]})
        if self.path == "/v1/chat/completions":
            return self._send(200, {"choices": [{"message": {"content": text}}]})
        if ":generateContent" in self.path:
            return self._send(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})
        if self.path == "/api/chat":
            return self._send(200, {"message": {"content": text}})
        self._send(404, {})


@pytest.fixture()
def mock_ai(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    for p in ("claude", "gemini", "openai"):
        monkeypatch.setitem(ai.PROVIDERS[p], "base", base)
    _Mock.seen = []
    _Mock.reply = json.dumps({"headline": "Easy day, you've earned it", "today": "Run 3 easy miles.",
                              "insights": ["HRV is steady.", "Sleep improved."], "focus": "Keep easy runs easy."})
    yield base
    srv.shutdown()


@pytest.mark.parametrize("provider,model", [("claude", "claude-sonnet-5-5"), ("openai", "gpt-5-mini"), ("gemini", "gemini-2.5-flash")])
def test_cloud_provider_end_to_end(signed_in, mock_ai, provider, model):
    bad = signed_in.post("/api/ai/key", json={"provider": provider, "key": "sk-wrong-key-999"}, headers=JSON)
    assert bad.status_code == 400 and "rejected" in bad.json()["detail"]
    assert not (main.settings.secrets_dir / f"ai-key-{provider}").exists()
    ok = signed_in.post("/api/ai/key", json={"provider": provider, "key": "sk-good-key-123"}, headers=JSON).json()
    assert ok["suggested"] == model
    assert "embedding" not in " ".join(ok["models"]) and "audio" not in " ".join(ok["models"])
    assert "sk-good-key-123" not in signed_in.get("/api/settings").text
    assert signed_in.get("/api/settings").json()["ai_keys_saved"][provider] is True
    signed_in.post("/api/settings", json={"values": {"ai_provider": provider, "ai_model": model}}, headers=JSON)
    assert signed_in.get("/api/status").json()["ai_configured"] is True
    ins = signed_in.post("/api/insight/refresh", headers=JSON).json()["insight"]
    assert ins["headline"] == "Easy day, you've earned it" and ins["insights"] == ["HRV is steady.", "Sleep improved."]
    assert signed_in.get("/api/insight").json()["insight"]["today"] == "Run 3 easy miles."
    _Mock.reply = "Yes, an easy run is fine."
    a = signed_in.post("/api/insight/ask", json={"question": "Can I run tomorrow?"}, headers=JSON).json()
    assert a["answer"] == "Yes, an easy run is fine."
    path, headers, body = _Mock.seen[-1]
    sent = json.dumps(body)
    assert "Can I run tomorrow?" in sent and "not medical advice" in sent and "adam" not in sent.lower()


def test_ollama_provider_and_non_json_reply(signed_in, mock_ai):
    r = signed_in.post("/api/ai/models", json={"provider": "ollama", "url": mock_ai}, headers=JSON).json()
    assert r["models"] == ["llama3.1:8b"] and r["suggested"] == "llama3.1:8b"
    signed_in.post("/api/settings", json={"values": {"ai_provider": "ollama", "ollama_url": mock_ai,
                                                     "ai_model": "llama3.1:8b"}}, headers=JSON)
    _Mock.reply = "Just run easy today."  # model ignored the JSON instruction
    ins = signed_in.post("/api/insight/refresh", headers=JSON).json()["insight"]
    assert ins["today"] == "Just run easy today." and ins["headline"] == ""


def test_daily_caps(signed_in, mock_ai, monkeypatch):
    monkeypatch.setitem(ai.LIMITS, "ask", 2)
    signed_in.post("/api/ai/key", json={"provider": "claude", "key": "sk-good-key-123"}, headers=JSON)
    signed_in.post("/api/settings", json={"values": {"ai_provider": "claude", "ai_model": "claude-sonnet-5-5"}}, headers=JSON)
    codes = [signed_in.post("/api/insight/ask", json={"question": "hi there"}, headers=JSON).status_code for _ in range(3)]
    assert codes == [200, 200, 400]


def test_maybe_daily_writes_once(signed_in, mock_ai, monkeypatch):
    from datetime import datetime as real_dt

    class Morning(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt.now(tz).replace(hour=7)
    monkeypatch.setattr(ai, "datetime", Morning)
    signed_in.post("/api/ai/key", json={"provider": "claude", "key": "sk-good-key-123"}, headers=JSON)
    signed_in.post("/api/settings", json={"values": {"ai_provider": "claude", "ai_model": "claude-sonnet-5-5"}}, headers=JSON)
    syncs = []
    ai.maybe_daily(main.settings, main.db, lambda: syncs.append(1))
    ai.maybe_daily(main.settings, main.db, lambda: syncs.append(1))
    assert len([s for s in _Mock.seen if s[0] == "/v1/messages"]) == 1
    assert ai.stored_insight(main.db)["date"] == date.today().isoformat()


def test_ai_endpoints_need_session_and_header(mock_ai):
    reset()
    make_account()
    anon = new_client()
    for path in ("/api/insight/refresh", "/api/ai/key", "/api/ai/models"):
        assert anon.post(path, json={}, headers=JSON).status_code == 401
    assert anon.get("/api/overview").status_code == 401


def test_model_choices_have_friendly_names(monkeypatch):
    replies = {
        "claude": {"data": [{"id": "claude-sonnet-5-5", "display_name": "Claude Sonnet 5.5", "created_at": "2026-05-01T00:00:00Z"},
                            {"id": "claude-haiku-5-5", "display_name": "  Claude   Haiku 5.5 "}, {"id": "not-claude"}, {"id": 5}]},
        "gemini": {"models": [{"name": "models/gemini-2.5-flash", "displayName": "Gemini 2.5 Flash", "supportedGenerationMethods": ["generateContent"]},
                              {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]}]},
        "openai": {"data": [{"id": "gpt-4.1-mini", "created": 2}, {"id": "gpt-5", "created": 9}, {"id": "whisper-1", "created": 5}]},
        "ollama": {"models": [{"name": "llama3.1:8b", "size": 4_900_000_000, "details": {"parameter_size": "8.0B"}},
                              {"name": "bad name <script>"}]},
    }
    monkeypatch.setattr(ai, "_request", lambda url, body, headers, timeout, direct=False:
                        replies[next(k for h, k in (("anthropic", "claude"), ("googleapis", "gemini"),
                                                     ("openai", "openai"), ("/api/tags", "ollama")) if h in url)])
    c = ai.model_choices("claude", "k")
    assert [x["label"] for x in c] == ["Claude Sonnet 5.5", "Claude Haiku 5.5"] and c[0]["note"] == "2026-05-01"
    assert ai.model_choices("gemini", "k") == [{"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "note": ""}]
    assert [x["id"] for x in ai.model_choices("openai", "k")] == ["gpt-5", "gpt-4.1-mini"]  # newest first
    o = ai.model_choices("ollama", None, "http://ollama.local:11434")
    assert o == [{"id": "llama3.1:8b", "label": "llama3.1:8b", "note": "8.0B · 4.9 GB"}]  # odd names dropped
