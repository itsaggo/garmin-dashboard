"""Discord webhook notifications."""
import json
from datetime import date, datetime, timedelta

import pytest

from app import main, notify
from tests.helpers import JSON, client as new_client, login, make_account, reset

HOOK = "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz_ABCDEFG-123"


@pytest.fixture()
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "_post", lambda url, payload: out.append((url, payload)))
    return out


@pytest.fixture()
def c():
    reset()
    cl = new_client()
    make_account()
    login(cl)
    return cl


@pytest.mark.parametrize("url", [
    "https://evil.example/api/webhooks/1/x", "http://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuv",
    "https://discord.com.evil.net/api/webhooks/123456789012345678/abcdefghijklmnopqrstuv",
    "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuv?wait=true#",
    "https://127.0.0.1/api/webhooks/123456789012345678/abcdefghijklmnopqrstuv"])
def test_only_discord_webhooks_accepted(c, sent, url):
    r = c.post("/api/notify/webhook", json={"url": url + "x" * max(0, 40 - len(url))}, headers=JSON)
    assert r.status_code == 422 and not sent


def test_save_sends_test_and_hides_url(c, sent):
    r = c.post("/api/notify/webhook", json={"url": HOOK}, headers=JSON)
    assert r.status_code == 200 and sent[0][0] == HOOK
    assert sent[0][1]["allowed_mentions"] == {"parse": []}
    st = c.get("/api/settings")
    assert st.json()["discord_saved"] is True and HOOK not in st.text and "abcdefghijklmnop" not in st.text


def test_rejected_webhook_not_saved(c, monkeypatch):
    def boom(url, payload):
        raise notify.NotifyError("Discord rejected the webhook")
    monkeypatch.setattr(notify, "_post", boom)
    assert c.post("/api/notify/webhook", json={"url": HOOK}, headers=JSON).status_code == 400
    assert notify.get_webhook(main.settings) is None


def _at(monkeypatch, when: datetime):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return when if tz is None else when.astimezone(tz)
    monkeypatch.setattr(notify, "datetime", Fixed)


def test_morning_once_per_day_and_toggle(c, sent, monkeypatch):
    notify.save_webhook(main.settings, HOOK)
    main.db.upsert_daily(date.today().isoformat(), {"sleep_score": 81, "sleep_s": 26000})
    _at(monkeypatch, datetime.now().replace(hour=6))
    notify.maybe_send(main.settings, main.db)
    assert not sent  # too early
    _at(monkeypatch, datetime.now().replace(hour=7, minute=5))
    notify.maybe_send(main.settings, main.db)
    notify.maybe_send(main.settings, main.db)
    morning = [p for _, p in sent if p["embeds"][0]["title"].startswith("Good morning")]
    assert len(morning) == 1
    fields = {f["name"]: f["value"] for f in morning[0]["embeds"][0]["fields"]}
    assert "81" in fields["Sleep"]
    main.db.delete_meta("notify_sent")
    c.post("/api/settings", json={"values": {"notify_morning": False}}, headers=JSON)
    sent.clear()
    notify.maybe_send(main.settings, main.db)
    assert not [p for _, p in sent if p["embeds"][0]["title"].startswith("Good morning")]


def test_morning_waits_for_ai_insight(c, sent, monkeypatch):
    notify.save_webhook(main.settings, HOOK)
    monkeypatch.setattr(notify.ai, "configured", lambda s, p: True)
    _at(monkeypatch, datetime.now().replace(hour=7))
    notify.maybe_send(main.settings, main.db)
    assert not sent  # AI hasn't written today's insight yet
    main.db.set_meta("insight", json.dumps({"date": date.today().isoformat(), "headline": "Easy day @everyone",
                                            "today": "Run 3 easy miles.", "insights": ["HRV steady."], "focus": "Sleep."}))
    notify.maybe_send(main.settings, main.db)
    desc = sent[0][1]["embeds"][0]["description"]
    assert "Run 3 easy miles." in desc and sent[0][1]["allowed_mentions"] == {"parse": []}


def test_weekly_on_monday(c, sent, monkeypatch):
    notify.save_webhook(main.settings, HOOK)
    c.post("/api/settings", json={"values": {"notify_morning": False}}, headers=JSON)
    today = date.today()
    monday = datetime.now().replace(hour=8, minute=30) - timedelta(days=today.weekday())

    class D(date):
        @classmethod
        def today(cls):
            return monday.date()
    monkeypatch.setattr(notify, "date", D)
    _at(monkeypatch, monday)
    notify.maybe_send(main.settings, main.db)
    notify.maybe_send(main.settings, main.db)
    weekly = [p for _, p in sent if "recap" in p["embeds"][0]["title"]]
    assert len(weekly) == 1


def test_problem_alerts_sent_once(c, sent, monkeypatch):
    notify.save_webhook(main.settings, HOOK)
    c.post("/api/settings", json={"values": {"notify_morning": False, "notify_weekly": False}}, headers=JSON)
    lid = main.db.log_start()
    main.db.log_finish(lid, "partial", "Personal API: FAILED (Garmin rejected the Personal API key (expired or deleted).)")
    notify.maybe_send(main.settings, main.db)
    notify.maybe_send(main.settings, main.db)
    alerts = [p["embeds"][0]["description"] for _, p in sent]
    assert len(alerts) == 1 and "Personal API key" in alerts[0]


def test_high_resting_hr_alert(c, sent):
    notify.save_webhook(main.settings, HOOK)
    for i in range(30):
        main.db.upsert_daily((date.today() - timedelta(days=i)).isoformat(), {"resting_hr": 60 if i < 3 else 52})
    ids = [pid for pid, _ in notify.problems(main.db, main.settings)]
    assert any(pid.startswith("rhr-") for pid in ids)


def test_notify_endpoints_need_login(sent):
    reset()
    make_account()
    anon = new_client()
    for path in ("/api/notify/webhook", "/api/notify/test", "/api/notify/webhook/remove"):
        assert anon.post(path, json={"url": HOOK}, headers=JSON).status_code == 401
    assert not sent
