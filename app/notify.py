"""Discord notifications via a channel webhook (one-way).

The webhook URL is a secret (anyone with it can post to your channel), so it's a 0600 file
under DATA_DIR/secrets and never sent back to the browser. Messages never ping anyone
(allowed_mentions is empty), so AI text can't trigger @everyone.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any

from . import ai, config, fitness, net, prefs as prefs_mod
from .db import Database

WEBHOOK_RE = re.compile(r"^https://(?:discord\.com|discordapp\.com|ptb\.discord\.com|canary\.discord\.com)"
                        r"/api/webhooks/\d{5,25}/[A-Za-z0-9_-]{20,120}$")
COLOR = {"morning": 0x2A78D6, "weekly": 0x1BAF7A, "problem": 0xEB6834}
LINK_TEXT = "Open your dashboard for details."


class NotifyError(Exception):
    pass


def _path(s: config.Settings):
    return s.secrets_dir / "discord-webhook"


def get_webhook(s: config.Settings) -> str | None:
    try:
        url = _path(s).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return url if WEBHOOK_RE.match(url) else None


def save_webhook(s: config.Settings, url: str) -> None:
    config.write_secret(_path(s), url.strip() + "\n")


def remove_webhook(s: config.Settings) -> None:
    _path(s).unlink(missing_ok=True)


def _post(url: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "garmin-dashboard"})
    try:
        with net.open_url(req, 15) as resp:
            resp.read(10000)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise NotifyError("Discord rejected the webhook (it may have been deleted). Create a new one.") from e
        if e.code == 429:
            raise NotifyError("Discord is rate-limiting this webhook. Try again in a minute.") from e
        raise NotifyError(f"Discord returned an error ({e.code}).") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise NotifyError(f"Couldn't reach Discord ({type(e).__name__}).") from e


def send(s: config.Settings, kind: str, title: str, lines: list[str], fields: list[tuple[str, str]] | None = None,
         url: str | None = None) -> None:
    url = url or get_webhook(s)
    if not url:
        raise NotifyError("No Discord webhook saved.")
    embed: dict[str, Any] = {
        "title": title[:250], "color": COLOR.get(kind, COLOR["morning"]),
        "description": "\n".join(line for line in lines if line)[:3900],
        "footer": {"text": "Garmin Dashboard · training guidance, not medical advice"},
        "timestamp": datetime.now().astimezone().isoformat(),
    }
    if fields:
        embed["fields"] = [{"name": n[:250], "value": (v or "–")[:1000], "inline": True} for n, v in fields[:12]]
    _post(url, {"username": "Garmin Dashboard", "embeds": [embed], "allowed_mentions": {"parse": []}})


# ------------------------------------------------------------------ message builders
def _metrics(db: Database) -> list[tuple[str, str]]:
    f = fitness.facts(db)
    fields = []
    w = f.get("weather_now")
    if w and w.get("temp_f") is not None and w.get("dew_point_f") is not None:
        metric = f["units"] == "km"
        t, dp = (w["temp_f"], w["dew_point_f"]) if not metric else ((w["temp_f"] - 32) * 5 / 9, (w["dew_point_f"] - 32) * 5 / 9)
        u = "°C" if metric else "°F"
        slow = w.get("slow_down_pct") or 0
        fields.append(("Heat", f"**{t:.0f}{u}**, dew point {dp:.0f}{u} · " + (f"slow down ~{slow:g}%" if slow else "no adjustment")))
    tr = f["training_readiness"]
    if tr["today"] is not None:
        fields.append(("Readiness", f"**{tr['today']:.0f}**/100" + (f" · {tr['level']}" if tr.get("level") else "")))
    sl = f["sleep"]
    parts = [f"**{sl['last_night_score']:.0f}**/100" if sl["last_night_score"] is not None else "",
             f"{sl['last_night_hours']} h" if sl["last_night_hours"] else ""]
    if any(parts):
        fields.append(("Sleep", " · ".join(x for x in parts if x)))
    if f["vo2max"]["now"]:
        fields.append(("VO2 max", f"**{f['vo2max']['now']:.1f}**"))
    if f["estimated_5k"]["time"]:
        fields.append(("5K estimate", f"**{f['estimated_5k']['time']}**"))
    if f["resting_hr"]["today"]:
        fields.append(("Resting HR", f"**{f['resting_hr']['today']:.0f}** bpm"))
    race = f.get("goal_race")
    if race and race["days_to_go"] >= 0:
        fields.append((race["name"], f"**{race['days_to_go']}** days to go"))
    return fields


def morning(s: config.Settings, db: Database) -> None:
    ins = ai.stored_insight(db)
    lines = []
    if ins and ins.get("date") == date.today().isoformat():
        if ins.get("headline"):
            lines.append(f"**{ins['headline']}**")
        if ins.get("today"):
            lines.append(ins["today"])
        lines += [f"• {i}" for i in ins.get("insights") or []]
        if ins.get("focus"):
            lines.append(f"**This week:** {ins['focus']}")
    else:
        lines.append(LINK_TEXT)
    send(s, "morning", f"Good morning · {date.today().strftime('%A, %b')} {date.today().day}", lines, _metrics(db))


def weekly(s: config.Settings, db: Database) -> None:
    today = date.today()
    acts = [a for a in db.activities(500) if a.get("start_local")]
    last_mon = today - timedelta(days=today.weekday() + 7)

    def week(start: date) -> dict:
        s_, e_ = start.isoformat(), (start + timedelta(days=6)).isoformat()
        wk = [a for a in acts if s_ <= a["start_local"][:10] <= e_]
        runs = [a for a in wk if "run" in (a.get("type") or "")]
        return {"n": len(wk), "runs": len(runs), "run_m": sum(a.get("distance_m") or 0 for a in runs),
                "mins": sum(a.get("duration_s") or 0 for a in wk) / 60}

    lw, pw = week(last_mon), week(last_mon - timedelta(days=7))
    metric = prefs_mod.get_all(db)["units"] == "metric"
    unit, div = ("km", 1000) if metric else ("mi", 1609.344)
    change = f" ({(lw['run_m'] - pw['run_m']) / pw['run_m']:+.0%} vs the week before)" if pw["run_m"] else ""
    lines = [f"**{lw['n']} workouts**, {lw['mins'] / 60:.1f} h total",
             f"Running: {lw['runs']} runs, {lw['run_m'] / div:.1f} {unit}{change}"]
    send(s, "weekly", f"Week of {last_mon.strftime('%b')} {last_mon.day}: your recap", lines,
         [m for m in _metrics(db) if m[0] != "Heat"])


def problems(db: Database, s: config.Settings) -> list[tuple[str, str]]:
    """(id, message) for current problems. ids are stable so each is sent once."""
    out = []
    last = db.last_sync() or {}
    msg = str(last.get("message") or "")
    if last.get("status") == "partial" and msg:
        if "Personal API key" in msg or "rejected the Personal API" in msg:
            out.append(("api-key", "Garmin rejected your Personal API key (it probably expired). "
                                   "Create a new one at connect.garmin.com and paste it in Settings → Garmin."))
        elif "app.setup connect" in msg or "Connect sign-in expired" in msg:
            out.append(("connect", "Your Garmin Connect sign-in expired, so VO2 max and readiness stopped updating. "
                                   "Sign in again under Settings → Garmin."))
        else:
            out.append(("sync-" + hashlib.sha1(msg[:200].encode()).hexdigest()[:10],
                        "The last Garmin sync had problems: " + msg[:400]))
    if s.api_key():
        try:
            age = (datetime.now().astimezone() - datetime.fromisoformat(last["finished_at"])).total_seconds()
            if age > 26 * 3600:
                out.append(("stale", "No successful Garmin sync in over a day. Check that the container is running."))
        except (KeyError, TypeError, ValueError):
            pass
    rows = {r["date"]: r for r in db.rows_since("daily", (date.today() - timedelta(days=33)).isoformat())}
    base_vals = [rows[d]["resting_hr"] for d in sorted(rows)[:-3] if rows[d].get("resting_hr")]
    last3 = [rows[d].get("resting_hr") for d in sorted(rows)[-3:]]
    if len(base_vals) >= 10 and len(last3) == 3 and all(last3):
        base = statistics.median(base_vals)
        if all(v >= base + 5 for v in last3):
            out.append((f"rhr-{date.today().isoformat()[:8]}{date.today().day // 7}",
                        f"Your resting heart rate has been {min(last3) - base:.0f}+ bpm above your normal ({base:.0f}) "
                        "for 3 days. Consider resting, and check with a doctor if it continues or you feel unwell."))
    return out


# ------------------------------------------------------------------ scheduler hook
def _sent(db: Database) -> dict:
    try:
        v = json.loads(db.get_meta("notify_sent") or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def maybe_send(s: config.Settings, db: Database) -> None:
    if not get_webhook(s):
        return
    p = prefs_mod.get_all(db)
    now = datetime.now()
    today = date.today()
    sent = _sent(db)

    def mark(key: str, value: Any) -> None:
        sent[key] = value
        db.set_meta("notify_sent", json.dumps(sent))

    if p["notify_morning"] and now.hour >= 7 and sent.get("morning") != today.isoformat():
        ins = ai.stored_insight(db)
        waiting_for_ai = ai.configured(s, p) and not (ins and ins.get("date") == today.isoformat()) and now.hour < 9
        if not waiting_for_ai:
            mark("morning", today.isoformat())  # mark first so a Discord error can't cause repeats
            morning(s, db)
    week_id = f"{today.isocalendar().year}-W{today.isocalendar().week}"
    if p["notify_weekly"] and today.weekday() == 0 and now.hour >= 8 and sent.get("weekly") != week_id:
        mark("weekly", week_id)
        weekly(s, db)
    if p["notify_problems"]:
        seen = sent.get("problems") if isinstance(sent.get("problems"), dict) else {}
        cutoff = (today - timedelta(days=14)).isoformat()
        seen = {k: v for k, v in seen.items() if v >= cutoff}
        for pid, text in problems(db, s):
            if pid in seen:
                continue
            seen[pid] = today.isoformat()
            mark("problems", seen)
            send(s, "problem", "Heads up", [text, LINK_TEXT])
        mark("problems", seen)
