"""Training plan: your own dated plan, checked off against synced activities, and summarised for the AI coach.

You paste a plan (from a coach, a book or a chat) in Settings. It is read into one session per date, kept in the
database, and matched to the runs Garmin syncs. The coach builds each day's advice on today's planned session and may
suggest moving or adjusting sessions in the coming week; nothing changes until you press Apply.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from . import prefs as prefs_mod
from .db import Database

MAX_TEXT = 60_000
MAX_DAYS = 800
MAX_SESSION = 160
MI = 1609.344
DONE_SHARE = 0.85            # a run counts as done at 85% of the planned distance or time
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WD = {**{w: i for i, w in enumerate(WEEKDAYS)}, **{w[:3]: i for i, w in enumerate(WEEKDAYS)},
       "tues": 1, "weds": 2, "thur": 3, "thurs": 3}
_MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_MONTHS.update({"sept": 9, "june": 6, "july": 7})
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_WDAY = r"(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:s|nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\.?"
_DATE_RES = [
    re.compile(r"^(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b"),
    re.compile(r"^(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?\b"),
    re.compile(r"^" + _MON + r"\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<y>\d{4}))?\b", re.I),
    re.compile(r"^(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r"(?:,?\s+(?P<y>\d{4}))?\b", re.I),
]
_LEAD_WDAY = re.compile(r"^" + _WDAY + r"(?=[\s,:.\-–—|)]|$)[\s,]*", re.I)
_SEP = re.compile(r"^[\s:|.,\-–—•*>)]+")
_BULLET = re.compile(r"^\s*(?:[-*•>]|\d+[.)])\s+")
_WEEK = re.compile(r"^\s*(?:#+\s*)?\**\s*week\s*(\d{1,3})\b(.*)$", re.I)
_WEEK_OF = re.compile(r"^\s*(?:#+\s*)?\**\s*week\s+(?:of|starting|beginning)\s+(.*)$", re.I)
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}")


_SKIP_COLS = {"day", "weekday", "week", "#", "total", "weekly total", "week total", "mileage", "weekly mileage",
              "done", "status", "date"}


class PlanError(Exception):
    pass


# ------------------------------------------------------------------ reading a session's text
_KIND_RULES = [
    ("rest", re.compile(r"^\W*(rest|off|day off|no run)\b|\brest day\b|\bday off\b|\bcomplete rest\b|\bfull rest\b", re.I)),
    ("race", re.compile(r"\brace\b(?!\s*(pace|effort|simulation|sim)\b)|\brace day\b|\bgoal race\b", re.I)),
    ("long", re.compile(r"\blong\b|\blsd\b", re.I)),
    ("intervals", re.compile(r"interval|repeats?\b|\d+\s*[x×]\s*\d|\btrack\b|fartlek|\bhills?\b|\bvo2|\bspeed\b|yasso", re.I)),
    ("tempo", re.compile(r"tempo|threshold|\blt\b|progression|marathon pace|\bmp\b|half[- ]marathon pace|\bhmp\b|race pace|cruise", re.I)),
    ("easy", re.compile(r"\beasy\b|recovery|aerobic|\bbase\b|shake ?out|\bjog\b|\bz2\b|zone 2|conversational", re.I)),
    ("cross", re.compile(r"cross|\bbike\b|cycl|\bspin\b|\bswim|strength|yoga|pilates|elliptical|\bgym\b|\blift|\bwalk\b|\bct\b", re.I)),
]
_DIST = re.compile(r"(\d+(?:\.\d+)?)(?:\s*(?:-|–|to)\s*(\d+(?:\.\d+)?))?\s*(mi|miles?|km|kilomet(?:er|re)s?|k)\b(?!\s*pace)", re.I)
_MINS = re.compile(r"(\d+)(?:\s*(?:-|–|to)\s*(\d+))?\s*(?:min|mins|minutes)\b", re.I)


def read_session(text: str) -> dict:
    """Kind (rest, easy, long, tempo, intervals, race, cross, run or other), planned distance (m) and minutes."""
    t = " ".join(text.split())[:MAX_SESSION]
    kind = next((k for k, rx in _KIND_RULES if rx.search(t)), None)
    lo = hi = None
    # the day's distance, not a repeat's length ("5x1K · 6 mi" is 6 mi)
    m = next((x for x in _DIST.finditer(t) if not re.search(r"[x×]\s*$", t[:x.start()], re.I)), None)
    if m:
        scale = 1000.0 if m.group(3).lower().startswith("k") else MI
        lo = float(m.group(1)) * scale
        hi = float(m.group(2)) * scale if m.group(2) else lo
        if hi < lo:
            lo, hi = hi, lo
    mins = None
    mm = _MINS.search(t)
    if mm:
        mins = float(mm.group(1))
    if kind is None:
        kind = "run" if (lo or mins or re.search(r"\brun\b", t, re.I)) else "other"
    if kind == "cross" and lo and not re.search(r"\b(bike|cycl|spin|swim|walk)", t, re.I):
        kind = "run" if not re.search(r"strength|yoga|pilates|gym|lift", t, re.I) else kind
    return {"text": t, "kind": kind, "dist_lo_m": round(lo, 1) if lo else None,
            "dist_hi_m": round(hi, 1) if hi else None, "minutes": mins}


HARD = {"long", "tempo", "intervals", "race"}


# ------------------------------------------------------------------ reading a pasted plan
def _mk_date(y: int | None, m: int, d: int, ref: date) -> date | None:
    try:
        if y is None:
            cand = date(ref.year, m, d)
            if cand < ref - timedelta(days=200):
                cand = date(ref.year + 1, m, d)
            elif cand > ref + timedelta(days=500):
                cand = date(ref.year - 1, m, d)
            return cand
        if y < 100:
            y += 2000
        return date(y, m, d)
    except ValueError:
        return None


def _lead_date(s: str, ref: date) -> tuple[date | None, str]:
    """A date at the start of s (after an optional weekday), and the rest of the text."""
    rest = _LEAD_WDAY.sub("", s.strip(), count=1)
    for rx in _DATE_RES:
        m = rx.match(rest)
        if not m:
            continue
        g = m.groupdict()
        if g.get("m"):
            mon = int(g["m"])
        else:  # month written as a word: group 1 in "Nov 24", group 2 in "24 Nov"
            word = (m.group(1) if rx is _DATE_RES[2] else m.group(2)).lower().rstrip(".")
            mon = _MONTHS["sept"] if word.startswith("sept") else _MONTHS[word[:3]]
        d = _mk_date(int(g["y"]) if g.get("y") else None, mon, int(g["d"]), ref)
        if d:
            after = rest[m.end():]
            after = _LEAD_WDAY.sub("", after.lstrip(" ,()"), count=1).lstrip(")")
            return d, _SEP.sub("", after).strip()
    return None, s


def _weekday_of(s: str) -> int | None:
    m = re.match(r"^\W*" + _WDAY + r"\b", s.strip(), re.I)
    if not m:
        return None
    w = m.group(1).lower().rstrip(".")
    return _WD.get(w) if w in _WD else _WD.get(w[:3])


def _cells(line: str) -> list[str]:
    parts = [c.strip() for c in line.strip().strip("|").split("|")]
    return [re.sub(r"[*_`]", "", c).strip() for c in parts]


def _on_weekday(start: date, wd: int) -> date:
    return start + timedelta(days=(wd - start.weekday()) % 7)


def parse(text: str, week1: date | None = None, today: date | None = None) -> dict:
    """Read a pasted plan. Returns {"days": [{"date", "text", "kind", ...}], "skipped": [lines not understood]}.
    Understands: lines that start with a date; tables with a Date column; tables with one row per week and a
    column per weekday; and "Week N" headings followed by "Mon: …" lines (with week1, the date week 1 starts)."""
    if len(text) > MAX_TEXT:
        raise PlanError("That plan is too long to read (60,000 characters at most).")
    ref = today or date.today()
    found: dict[date, list[str]] = {}
    skipped: list[str] = []
    need_week1 = False
    header: list[str] | None = None
    week_start: date | None = None
    anchor: tuple[int, date] | None = None   # a "Week N" heading that had a date: later weeks count on from it
    last: date | None = None

    def add(d: date, session: str) -> None:
        nonlocal last
        session = " ".join(session.split()).strip(" -–—:|·")
        if not session or session in ("-", "–", "—"):
            return
        if last and d < last - timedelta(days=200):  # a plan running from December into January
            try:
                d = d.replace(year=d.year + 1)
            except ValueError:
                return
        found.setdefault(d, [])
        if session not in found[d]:
            found[d].append(session)
        last = max(last or d, d)

    def week_date(n: int) -> date | None:
        nonlocal need_week1
        if not week1:
            need_week1 = True
            return None
        return week1 + timedelta(days=7 * (n - 1))

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or _TABLE_RULE.match(line):
            continue
        if "|" in line:
            cells = _cells(line)
            low = [c.lower() for c in cells]
            wd_cols = {i: _weekday_of(c) for i, c in enumerate(cells) if _weekday_of(c) is not None and len(c) <= 10}
            if "date" in low or len(wd_cols) >= 3:
                header = low
                continue
            if header and len(cells) >= 2:
                hdr_wd = {i: _weekday_of(h) for i, h in enumerate(header) if _weekday_of(h) is not None and len(h) <= 10}
                if len(hdr_wd) >= 3:  # one row per week, one column per weekday
                    first = cells[0]
                    d0, _ = _lead_date(first, ref)
                    if d0 is None:
                        wk = re.search(r"(\d{1,3})", first)
                        d0 = week_date(int(wk.group(1))) if wk else None
                    if d0 is None:
                        if not need_week1:
                            skipped.append(line.strip())
                        continue
                    for i, wd in hdr_wd.items():
                        if i < len(cells):
                            add(_on_weekday(d0, wd), cells[i])
                    continue
                if "date" in header:  # one row per day
                    di = header.index("date")
                    d, _ = _lead_date(cells[di], ref) if di < len(cells) else (None, "")
                    if d is None:
                        skipped.append(line.strip())
                        continue
                    keep = []
                    for i, c in enumerate(cells):
                        h = header[i] if i < len(header) else ""
                        if i == di or not c.strip("-–— ") or h in _SKIP_COLS or (_weekday_of(c) is not None and len(c) <= 10):
                            continue  # the date itself, a bare weekday, week numbers and totals
                        keep.append(c)
                    add(d, " · ".join(keep))
                    continue
            # a table row we can't place: fall through to line rules using the joined text
            line = " ".join(c for c in _cells(line) if c)
        header = None if "|" not in raw else header
        s = _BULLET.sub("", line).strip().strip("*").strip()
        wo = _WEEK_OF.match(s)
        if wo:
            d0, _ = _lead_date(wo.group(1), ref)
            if d0:
                week_start = d0
                continue
        wk = _WEEK.match(s)
        if wk:
            rest = wk.group(2)
            d0 = None
            m = re.search(r"\(?\s*(.+?)\s*(?:[-–—]|to)\s*", rest)
            if m:
                d0, _ = _lead_date(m.group(1).lstrip("(: "), ref)
            if d0 is None:
                d0, _ = _lead_date(rest.lstrip("(:-–— "), ref)
            n = int(wk.group(1))
            if d0:
                anchor = (n, d0)
            elif anchor:
                d0 = anchor[1] + timedelta(days=7 * (n - anchor[0]))
            week_start = d0 or week_date(n)
            continue
        d, rest = _lead_date(s, ref)
        if d is not None:
            add(d, rest)
            continue
        wd = _weekday_of(s)
        if wd is not None and week_start is not None:
            body = _SEP.sub("", _LEAD_WDAY.sub("", s, count=1)).strip()
            add(_on_weekday(week_start, wd), body)
            continue
        if wd is not None and week_start is None and not week1:
            need_week1 = True
            continue
        if len(skipped) < 200:
            skipped.append(s[:120])
    if need_week1 and not found:
        raise PlanError("This plan is laid out by week without dates. Enter the date week 1 starts, then preview again.")
    if len(found) > MAX_DAYS:
        raise PlanError(f"That plan has {len(found)} days; the most it can hold is {MAX_DAYS}.")
    days = [{"date": d.isoformat(), **read_session(" + ".join(v))} for d, v in sorted(found.items())]
    return {"days": days, "skipped": skipped[:20], "skipped_count": len(skipped), "needs_week1": need_week1}


# ------------------------------------------------------------------ storage
def save(db: Database, days: list[dict], source: str = "plan") -> int:
    with db.connect() as con:
        con.execute("DELETE FROM plan_days")
        con.executemany("INSERT INTO plan_days(date, text, kind, dist_lo_m, dist_hi_m, minutes, source) VALUES (?,?,?,?,?,?,?)",
                        [(d["date"], d["text"], d["kind"], d["dist_lo_m"], d["dist_hi_m"], d["minutes"], source) for d in days])
    db.set_meta("plan_saved", datetime.now().isoformat(timespec="seconds"))
    db.set_meta("plan_undo", "")
    return len(days)


def clear(db: Database) -> None:
    with db.connect() as con:
        con.execute("DELETE FROM plan_days")
    db.set_meta("plan_undo", "")


def entries(db: Database, start: date | None = None, end: date | None = None) -> list[dict]:
    q, args = "SELECT date, text, kind, dist_lo_m, dist_hi_m, minutes, source FROM plan_days", []
    if start and end:
        q += " WHERE date BETWEEN ? AND ?"
        args = [start.isoformat(), end.isoformat()]
    with db.connect() as con:
        return [dict(r) for r in con.execute(q + " ORDER BY date", args)]


def as_text(db: Database) -> str:
    return "\n".join(f"{e['date']}  {e['text']}" for e in entries(db))


def info(db: Database) -> dict:
    with db.connect() as con:
        n, first, last = con.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM plan_days").fetchone()
    return {"days": n, "first": first, "last": last, "saved": db.get_meta("plan_saved") if n else None}


# ------------------------------------------------------------------ checking off
def _acts(db: Database, start: date, end: date) -> dict[str, list[dict]]:
    with db.connect() as con:
        rows = con.execute("SELECT start_local, type, distance_m, duration_s, moving_s FROM activities "
                           "WHERE substr(start_local,1,10) BETWEEN ? AND ?", (start.isoformat(), end.isoformat())).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["start_local"][:10], []).append(dict(r))
    return out


def _is_run(a: dict) -> bool:
    return "run" in (a.get("type") or "").lower()


def status(e: dict | None, acts: list[dict], day: date, today: date) -> dict:
    """How a planned day went: done, partial, missed, rested, extra (ran on a rest day), today or upcoming."""
    runs = [a for a in acts if _is_run(a)]
    ran_m = sum(a.get("distance_m") or 0 for a in runs)
    ran_s = sum(a.get("moving_s") or a.get("duration_s") or 0 for a in runs)
    other = [a for a in acts if not _is_run(a)]
    base = {"ran_m": round(ran_m, 1), "activities": len(acts)}
    if day > today:
        return {**base, "status": "upcoming"}
    if e is None:
        return {**base, "status": "extra" if runs else "none"}
    kind = e["kind"]
    if kind == "rest":
        if runs:
            return {**base, "status": "extra"}
        return {**base, "status": "today" if day == today else "rested"}
    if kind == "cross":
        return {**base, "status": "done" if acts else ("today" if day == today else "missed")}
    if kind == "other" and not runs:
        return {**base, "status": "done" if other else ("today" if day == today else "missed")}
    if not runs:
        return {**base, "status": "today" if day == today else "missed"}
    if e.get("dist_lo_m"):
        ok = ran_m >= DONE_SHARE * e["dist_lo_m"]
    elif e.get("minutes"):
        ok = ran_s >= DONE_SHARE * e["minutes"] * 60
    else:
        ok = True
    return {**base, "status": "done" if ok else "partial"}


def days_with_status(db: Database, start: date, end: date, today: date | None = None) -> list[dict]:
    today = today or date.today()
    plan = {e["date"]: e for e in entries(db, start, end)}
    acts = _acts(db, start, end)
    out = []
    d = start
    while d <= end:
        iso = d.isoformat()
        e = plan.get(iso)
        out.append({"date": iso, "weekday": WEEKDAYS[d.weekday()][:3].title(),
                    "planned": e, **status(e, acts.get(iso, []), d, today)})
        d += timedelta(days=1)
    return out


def week(db: Database, today: date | None = None) -> dict:
    """This week (Monday to Sunday) for the dashboard: planned vs done, plus today's session."""
    today = today or date.today()
    mon = today - timedelta(days=today.weekday())
    days = days_with_status(db, mon, mon + timedelta(days=6), today)
    planned_m = sum((d["planned"] or {}).get("dist_lo_m") or 0 for d in days)
    done_m = sum(d["ran_m"] for d in days)
    i = info(db)
    return {"has_plan": bool(i["days"]), "today": next(d for d in days if d["date"] == today.isoformat()),
            "days": days, "planned_m": round(planned_m), "done_m": round(done_m),
            "ended": bool(i["last"] and i["last"] < today.isoformat()), "first": i["first"], "last": i["last"]}


# ------------------------------------------------------------------ for the AI coach
RULES = (
    "The facts include training_plan: the athlete's own program. Build today's advice on today's planned session "
    "and say what it is. Keep to the plan when recovery looks normal. When the data shows poor recovery (HRV well below "
    "normal, resting heart rate well above normal, poor sleep, low training readiness, or a high acute:chronic load), "
    "you may move or adjust sessions in the next 7 days, but protect the training cycle: keep the week's long run and "
    "key workout in the same week where possible, never put two hard days (long, tempo, intervals, race) back to back, "
    "keep the week's total distance about the same, don't make up big missed volume, and don't add a run on a rest day "
    "unless you are moving another session there. If you change anything, list every date you change in plan_change."
)
INSIGHT_FIELD = ('"plan_change": null, or if you move or adjust sessions: {"reason": "one sentence", '
                 '"moves": [{"date": "YYYY-MM-DD", "session": "the new session for that date, e.g. Rest or Easy 4 mi"}]}')


def coach_facts(db: Database, today: date | None = None) -> dict | None:
    today = today or date.today()
    i = info(db)
    if not i["days"] or i["last"] < (today - timedelta(days=3)).isoformat() or i["first"] > (today + timedelta(days=14)).isoformat():
        return None
    metric = prefs_mod.get_all(db)["units"] == "metric"
    unit = "km" if metric else "mi"

    def dist(m: float | None) -> float | None:
        return round(m / (1000 if metric else MI), 1) if m else None

    def brief(d: dict, with_status: bool) -> dict:
        e = d["planned"]
        out = {"date": d["date"], "weekday": d["weekday"], "session": e["text"] if e else "(nothing planned)",
               "kind": e["kind"] if e else None}
        if with_status:
            out.update(status=d["status"], **({f"ran_{unit}": dist(d["ran_m"])} if d["ran_m"] else {}))
        return out

    past = days_with_status(db, today - timedelta(days=7), today - timedelta(days=1), today)
    ahead = days_with_status(db, today, today + timedelta(days=7), today)
    wk = week(db, today)
    return {
        "today": brief(ahead[0], True),
        "next_7_days": [brief(d, False) for d in ahead[1:]],
        "last_7_days": [brief(d, True) for d in past],
        "this_week": {f"planned_{unit}": dist(wk["planned_m"]), f"done_{unit}": dist(wk["done_m"])},
        "plan_ends": i["last"],
    }


def clean_change(change: Any, db: Database, today: date | None = None) -> dict | None:
    """Keep a coach's suggested change only if it is well-formed and within the next 7 days."""
    today = today or date.today()
    if not isinstance(change, dict) or not isinstance(change.get("moves"), list):
        return None
    current = {e["date"]: e["text"] for e in entries(db, today, today + timedelta(days=6))}
    moves, seen = [], set()
    for m in change["moves"][:4]:
        if not isinstance(m, dict):
            continue
        try:
            d = date.fromisoformat(str(m.get("date", ""))[:10])
        except ValueError:
            continue
        sess = " ".join(str(m.get("session") or "").split())[:MAX_SESSION]
        sess = re.sub(r"[\x00-\x1f<>]", "", sess)
        if not (today <= d <= today + timedelta(days=6)) or d in seen or len(sess) < 2:
            continue
        if current.get(d.isoformat(), "").strip().lower() == sess.lower():
            continue
        seen.add(d)
        moves.append({"date": d.isoformat(), "session": sess, "was": current.get(d.isoformat())})
    if not moves:
        return None
    reason = " ".join(str(change.get("reason") or "").split())[:300]
    return {"reason": reason, "moves": sorted(moves, key=lambda m: m["date"]), "state": "pending"}


def apply_change(db: Database, change: dict) -> dict:
    """Write a suggested change into the plan, remembering what it replaced so it can be undone."""
    undo = []
    with db.connect() as con:
        for m in change["moves"]:
            row = con.execute("SELECT text, kind, dist_lo_m, dist_hi_m, minutes, source FROM plan_days WHERE date=?",
                              (m["date"],)).fetchone()
            undo.append({"date": m["date"], "row": dict(row) if row else None})
            s = read_session(m["session"])
            con.execute("INSERT OR REPLACE INTO plan_days(date, text, kind, dist_lo_m, dist_hi_m, minutes, source) "
                        "VALUES (?,?,?,?,?,?,?)", (m["date"], s["text"], s["kind"], s["dist_lo_m"], s["dist_hi_m"], s["minutes"], "coach"))
    db.set_meta("plan_undo", json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "rows": undo}))
    return {"applied": len(undo)}


def undo_last(db: Database) -> bool:
    try:
        u = json.loads(db.get_meta("plan_undo") or "null")
    except ValueError:
        u = None
    if not isinstance(u, dict) or not isinstance(u.get("rows"), list):
        return False
    with db.connect() as con:
        for r in u["rows"]:
            if r.get("row"):
                x = r["row"]
                con.execute("INSERT OR REPLACE INTO plan_days(date, text, kind, dist_lo_m, dist_hi_m, minutes, source) "
                            "VALUES (?,?,?,?,?,?,?)", (r["date"], x["text"], x["kind"], x["dist_lo_m"], x["dist_hi_m"], x["minutes"], x["source"]))
            else:
                con.execute("DELETE FROM plan_days WHERE date=?", (r["date"],))
    db.set_meta("plan_undo", "")
    return True
