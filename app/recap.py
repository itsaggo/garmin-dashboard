"""Race recap: everything the Recap page needs to animate one run.

For a run you pick, the server fetches (once, then keeps):
  * the run's original activity file through the official Personal API, read for the GPS track,
    elapsed time, heart rate, cadence and altitude along the run;
  * an aerial photo of the area from USGS (public domain, US mainland only);
  * building outlines near the route from OpenStreetMap, turned into simple boxes.

Only the area's rectangle is sent to USGS and OpenStreetMap, never the route itself. The browser
does all the drawing and the video encoding; nothing here renders anything.
"""
from __future__ import annotations

import io
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, net, prefs
from .db import Database

log = logging.getLogger("garmin.recap")

R_EARTH = 6378137.0                       # Web Mercator sphere radius (EPSG:3857)
MAX_POINTS = 4000                         # track points sent to the browser
MAX_ZIP_BYTES = 40 * 1024 * 1024          # a long run's original file is a few MB
MAX_FIT_BYTES = 64 * 1024 * 1024
MARGIN_M = 1600                           # ground shown around the route
MIN_SIDE_M = 4000
IMAGE_PX = 4000                           # the USGS service's largest export
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MAX_OSM_BYTES = 120 * 1024 * 1024
BUILDING_REACH_M = 450                    # keep buildings this close to the route
MAX_BUILDINGS = 9000
KEEP_IMAGES = 20                          # cached photos kept on disk (newest first)
RETRY_AFTER_S = 6 * 3600                  # a failed optional download is retried after this

NAIP_URL = "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer/exportImage"
NAIP_CREDIT = "Imagery: USGS, USDA, The National Map"
# The service covers the US mainland (its published extent, in Web Mercator metres).
NAIP_EXTENT = (-13896162.9, 2812730.7, -7441890.6, 6372359.7)
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OSM_CREDIT = "Map data © OpenStreetMap contributors"
USER_AGENT = "garmin-dashboard (self-hosted; https://github.com/itsaggo/garmin-dashboard)"

_EXCLUDE_TYPES = ("treadmill", "indoor", "virtual")


class RecapError(Exception):
    """A problem worth showing to the person, in plain words."""


# ------------------------------------------------------------------ which runs
def is_outdoor_run(activity_type: str | None) -> bool:
    t = (activity_type or "").lower()
    return "run" in t and not any(x in t for x in _EXCLUDE_TYPES)


def runs(db: Database, limit: int = 300) -> list[dict]:
    """Outdoor runs, newest first, with whether a recap is already prepared."""
    with db.connect() as con:
        rows = con.execute(
            "SELECT id, start_local, name, type, distance_m, duration_s, moving_s, avg_hr, elev_gain_m "
            "FROM activities WHERE lower(type) LIKE '%run%' ORDER BY start_local DESC LIMIT ?",
            (limit * 2,),
        ).fetchall()
        ready = {r[0] for r in con.execute("SELECT id FROM recap_cache WHERE kind='track'")}
    out = []
    for r in rows:
        if not is_outdoor_run(r["type"]) or not (r["distance_m"] or 0) > 200:
            continue
        out.append({**dict(r), "ready": r["id"] in ready})
        if len(out) >= limit:
            break
    return out


def _run_row(db: Database, activity_id: int) -> dict:
    with db.connect() as con:
        row = con.execute("SELECT id, start_local, name, type, distance_m, duration_s FROM activities WHERE id=?",
                          (activity_id,)).fetchone()
    if not row:
        raise RecapError("That run isn't in your synced activities.")
    if not is_outdoor_run(row["type"]):
        raise RecapError("Recaps are for outdoor runs with GPS.")
    return dict(row)


# ------------------------------------------------------------------ the activity file
def fit_from_zip(blob: bytes) -> bytes:
    """The FIT file inside Garmin's 'original file' download, with size limits (no zip bombs)."""
    if len(blob) > MAX_ZIP_BYTES:
        raise RecapError("The activity file is unexpectedly large.")
    if blob[:4] != b"PK\x03\x04":
        if len(blob) >= 12 and blob[8:12] == b".FIT":
            return blob  # some downloads aren't zipped
        raise RecapError("Garmin didn't send an activity file.")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            fits = [i for i in z.infolist() if i.filename.lower().endswith(".fit") and not i.is_dir()]
            if not fits:
                raise RecapError("This run's original file isn't a FIT file (it may have been imported from another app).")
            info = fits[0]
            if info.file_size > MAX_FIT_BYTES:
                raise RecapError("The activity file is unexpectedly large.")
            with z.open(info) as f:
                data = f.read(MAX_FIT_BYTES + 1)
    except zipfile.BadZipFile as e:
        raise RecapError("The activity file from Garmin was damaged.") from e
    if len(data) > MAX_FIT_BYTES:
        raise RecapError("The activity file is unexpectedly large.")
    return data


_STOP = {"stop", "stop_all", "stop_disable", "stop_disable_all"}
SEMI = 180.0 / 2 ** 31


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def parse_fit(data: bytes) -> dict:
    """Read the samples of a FIT activity: timer time (pauses left out), distance, position, heart rate,
    cadence and altitude, plus the totals from the session summary."""
    import fitdecode

    samples: list[dict] = []
    session: dict = {}
    running, last_ts, timer = True, None, 0.0
    try:
        with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.WARN,
                                 error_handling=fitdecode.ErrorHandling.WARN) as reader:
            for frame in reader:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue
                name = frame.name
                if name not in ("record", "event", "session"):
                    continue
                vals = {f.name: f.value for f in frame.fields}
                ts = vals.get("timestamp")
                ts = ts.timestamp() if isinstance(ts, datetime) else None
                if name == "event":
                    if vals.get("event") == "timer" and ts is not None:
                        kind = vals.get("event_type")
                        if kind == "start":
                            running, last_ts = True, ts
                        elif kind in _STOP:
                            if running and last_ts is not None:
                                timer += max(0.0, ts - last_ts)
                            running, last_ts = False, ts
                    continue
                if name == "session":
                    if not session:  # a multisport file has several; the first is enough here
                        session = vals
                    continue
                if ts is None:
                    continue
                if running and last_ts is not None:
                    gap = ts - last_ts
                    if 0 < gap < 3600:
                        timer += gap
                last_ts = ts
                lat, lon = vals.get("position_lat"), vals.get("position_long")
                cad = _num(vals.get("cadence"))
                if cad is not None:
                    cad = cad * 2 + (_num(vals.get("fractional_cadence")) or 0) * 2  # strides/min → steps/min
                alt = _num(vals.get("enhanced_altitude"))
                if alt is None:
                    alt = _num(vals.get("altitude"))
                samples.append({
                    "t": timer, "d": _num(vals.get("distance")),
                    "lat": lat * SEMI if isinstance(lat, int) and abs(lat) < 2 ** 31 - 1 else None,
                    "lon": lon * SEMI if isinstance(lon, int) and abs(lon) < 2 ** 31 - 1 else None,
                    "hr": _num(vals.get("heart_rate")), "cad": cad, "alt": alt,
                })
    except fitdecode.FitError as e:
        raise RecapError("The activity file couldn't be read.") from e
    return {"samples": samples, "session": session}


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(h)))


def merc(lat: float, lon: float) -> tuple[float, float]:
    lat = max(-85.0, min(85.0, lat))
    return R_EARTH * math.radians(lon), R_EARTH * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def unmerc(x: float, y: float) -> tuple[float, float]:
    return math.degrees(2 * math.atan(math.exp(y / R_EARTH)) - math.pi / 2), math.degrees(x / R_EARTH)


def _crossings(t: list[float], d: list[float], step: float) -> list[float]:
    """Timer time at each whole `step` of distance (linear between samples)."""
    out, k = [], 1
    for i in range(1, len(d)):
        while d[i] >= k * step:
            span = d[i] - d[i - 1]
            f = (k * step - d[i - 1]) / span if span > 0 else 1.0
            out.append(round(t[i - 1] + f * (t[i] - t[i - 1]), 1))
            k += 1
    return out


def build_track(parsed: dict) -> dict:
    """Turn parsed samples into what the browser draws: local metres (x east, y north) around the centre
    of the route, the matching Web Mercator frame for the aerial photo, mile/km split times and totals."""
    samples, session = parsed["samples"], parsed["session"]
    # distance: the watch's own, or measured from positions when it's missing
    have_d = sum(1 for s in samples if s["d"] is not None)
    if have_d < len(samples) * 0.5:
        total, prev = 0.0, None
        for s in samples:
            if s["lat"] is not None:
                if prev:
                    total += _haversine(prev, (s["lat"], s["lon"]))
                prev = (s["lat"], s["lon"])
            s["d"] = total
    best = 0.0
    for s in samples:  # never let distance run backwards
        if s["d"] is None:
            s["d"] = best
        best = max(best, s["d"])
        s["d"] = best
    pos = [s for s in samples if s["lat"] is not None and s["lon"] is not None
           and -90 <= s["lat"] <= 90 and -180 <= s["lon"] <= 180 and not (s["lat"] == 0 and s["lon"] == 0)]
    total_d = _num(session.get("total_distance")) or (samples[-1]["d"] if samples else 0)
    if len(pos) < 30 or total_d < 200:
        raise RecapError("This run has no GPS track to draw.")
    total_t = _num(session.get("total_timer_time")) or samples[-1]["t"]
    # scale the sample clock so it ends exactly on the official time
    if samples[-1]["t"] > 0 and total_t:
        k = total_t / samples[-1]["t"]
        if 0.9 < k < 1.1:
            for s in samples:
                s["t"] *= k
    t_all, d_all = [s["t"] for s in samples], [s["d"] for s in samples]
    splits = {"mi": _crossings(t_all, d_all, 1609.344), "km": _crossings(t_all, d_all, 1000.0)}

    mx = [merc(s["lat"], s["lon"]) for s in pos]
    x0, x1 = min(p[0] for p in mx), max(p[0] for p in mx)
    y0, y1 = min(p[1] for p in mx), max(p[1] for p in mx)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    clat, clon = unmerc(cx, cy)
    k = math.cos(math.radians(clat))                     # Mercator metres → ground metres here
    side = max(MIN_SIDE_M, max(x1 - x0, y1 - y0) * k + 2 * MARGIN_M)

    stride = max(1, math.ceil(len(pos) / MAX_POINTS))
    keep = list(range(0, len(pos), stride))
    if keep[-1] != len(pos) - 1:
        keep.append(len(pos) - 1)

    def col(key: str, nd: int = 0) -> list:
        return [None if pos[i][key] is None else round(pos[i][key], nd) if nd else round(pos[i][key]) for i in keep]

    hrs = [s["hr"] for s in samples if s["hr"]]
    cads = [s["cad"] for s in samples if s["cad"]]
    alts = [s["alt"] for s in samples if s["alt"] is not None]
    gain = _num(session.get("total_ascent"))
    if gain is None and alts:
        gain = sum(max(0.0, b - a) for a, b in zip(alts, alts[1:]))
    avg_cad = _num(session.get("avg_running_cadence")) or _num(session.get("avg_cadence"))
    if avg_cad is not None:
        avg_cad = avg_cad * 2 + (_num(session.get("avg_fractional_cadence")) or 0) * 2
    return {
        "version": 1,
        "points": {
            "x": [round((mx[i][0] - cx) * k, 1) for i in keep],
            "y": [round((mx[i][1] - cy) * k, 1) for i in keep],
            "d": col("d", 1), "t": col("t", 1), "hr": col("hr"), "cad": col("cad"), "alt": col("alt", 1),
        },
        "splits": splits,
        "totals": {
            "distance_m": round(total_d, 1), "timer_s": round(total_t, 1),
            "avg_hr": round(_num(session.get("avg_heart_rate")) or (sum(hrs) / len(hrs) if hrs else 0)) or None,
            "max_hr": round(_num(session.get("max_heart_rate")) or (max(hrs) if hrs else 0)) or None,
            "avg_cadence": round(avg_cad or (sum(cads) / len(cads) if cads else 0)) or None,
            "gain_m": round(gain, 1) if gain is not None else None,
        },
        # The square of ground around the route, shared by the photo and the buildings.
        "frame": {"side_m": round(side, 1), "merc": [cx, cy], "k": k, "center": [round(clat, 6), round(clon, 6)]},
    }


# ------------------------------------------------------------------ aerial photo (USGS NAIP)
def _bbox_merc(frame: dict, side_m: float | None = None) -> tuple[float, float, float, float]:
    cx, cy = frame["merc"]
    half = (side_m or frame["side_m"]) / 2 / frame["k"]
    return cx - half, cy - half, cx + half, cy + half


def naip_covers(frame: dict) -> bool:
    cx, cy = frame["merc"]
    x0, y0, x1, y1 = NAIP_EXTENT
    return x0 <= cx <= x1 and y0 <= cy <= y1


def naip_url(frame: dict) -> str:
    bx = _bbox_merc(frame)
    q = {
        "bbox": ",".join(f"{v:.2f}" for v in bx), "bboxSR": "3857", "imageSR": "3857",
        "size": f"{IMAGE_PX},{IMAGE_PX}", "format": "jpg", "compressionQuality": "82",
        "bandIds": "0,1,2", "interpolation": "RSP_BilinearInterpolation", "f": "image",
    }
    return NAIP_URL + "?" + urllib.parse.urlencode(q)


def _read_limited(resp, limit: int) -> bytes:
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise RecapError("The download was unexpectedly large.")
    return data


def fetch_imagery(frame: dict, path: Path) -> None:
    req = urllib.request.Request(naip_url(frame), headers={"User-Agent": USER_AGENT, "Accept": "image/jpeg"})
    try:
        with net.open_url(req, timeout=120) as resp:
            data = _read_limited(resp, MAX_IMAGE_BYTES)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RecapError("Couldn't reach the USGS imagery service.") from e
    if not data.startswith(b"\xff\xd8\xff"):
        raise RecapError("The USGS imagery service didn't send a photo.")
    tmp = path.with_suffix(".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# ------------------------------------------------------------------ buildings (OpenStreetMap)
_NUM = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m|meters?|metres?|ft|feet|')?\s*$", re.I)


def building_height(tags: dict) -> float:
    """Height in metres from OSM tags, with sensible guesses for the many buildings that don't say."""
    h = _NUM.match(tags.get("height", "") or "")
    if h:
        v = float(h.group(1)) * (0.3048 if (h.group(2) or "").lower() in ("ft", "feet", "'") else 1)
        if 2 <= v <= 600:
            return v
    lv = _NUM.match(tags.get("building:levels", "") or "")
    if lv and not lv.group(2):
        v = float(lv.group(1))
        if 1 <= v <= 150:
            return v * 3.2 + 1
    kind = tags.get("building", "yes")
    if kind in ("house", "detached", "residential", "semidetached_house", "bungalow", "terrace"):
        return 6.0
    if kind in ("garage", "garages", "shed", "carport", "roof", "hut", "kiosk"):
        return 3.0
    if kind in ("apartments", "office", "hotel", "hospital", "university", "dormitory"):
        return 14.0
    if kind in ("commercial", "retail", "industrial", "warehouse", "school", "church", "supermarket"):
        return 8.0
    return 6.0


def footprint_box(pts: list[tuple[float, float]]) -> tuple[float, float, float, float, float] | None:
    """A rotated rectangle around a footprint, lined up with its longest wall: (cx, cy, w, d, angle)."""
    if len(pts) < 3:
        return None
    best, ang = -1.0, 0.0
    for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1]):
        L = (bx - ax) ** 2 + (by - ay) ** 2
        if L > best:
            best, ang = L, math.atan2(by - ay, bx - ax)
    c, s = math.cos(-ang), math.sin(-ang)
    rx = [x * c - y * s for x, y in pts]
    ry = [x * s + y * c for x, y in pts]
    u0, u1, v0, v1 = min(rx), max(rx), min(ry), max(ry)
    w, d = u1 - u0, v1 - v0
    if w < 2 or d < 2 or w > 600 or d > 600:
        return None
    mu, mv = (u0 + u1) / 2, (v0 + v1) / 2
    c2, s2 = math.cos(ang), math.sin(ang)
    return mu * c2 - mv * s2, mu * s2 + mv * c2, w, d, ang


class _Near:
    """Fast 'is this point within R metres of the route' test on a coarse grid."""

    def __init__(self, xs: list[float], ys: list[float], reach: float):
        self.cell, self.reach, self.grid = reach, reach, {}
        for x, y in zip(xs, ys):
            self.grid.setdefault((int(x // reach), int(y // reach)), []).append((x, y))

    def dist(self, x: float, y: float) -> float:
        gx, gy, best = int(x // self.cell), int(y // self.cell), math.inf
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                for px, py in self.grid.get((gx + i, gy + j), ()):
                    best = min(best, (px - x) ** 2 + (py - y) ** 2)
        return math.sqrt(best)


def overpass_query(frame: dict, track: dict) -> str:
    """Buildings in the route's own rectangle plus the reach (not the whole photo)."""
    p = track["points"]
    x0, x1 = min(p["x"]) - BUILDING_REACH_M, max(p["x"]) + BUILDING_REACH_M
    y0, y1 = min(p["y"]) - BUILDING_REACH_M, max(p["y"]) + BUILDING_REACH_M
    cx, cy, k = frame["merc"][0], frame["merc"][1], frame["k"]
    s, w = unmerc(cx + x0 / k, cy + y0 / k)
    n, e = unmerc(cx + x1 / k, cy + y1 / k)
    return (f"[out:xml][timeout:150][maxsize:{MAX_OSM_BYTES}];"
            f'way["building"]({s:.6f},{w:.6f},{n:.6f},{e:.6f});out tags geom;')


def parse_buildings(stream, frame: dict, track: dict) -> list[list[float]]:
    """Stream Overpass XML into boxes near the route: [x, y, w, d, angle, height] in local metres."""
    p = track["points"]
    near = _Near(p["x"], p["y"], BUILDING_REACH_M)
    cx, cy, k = frame["merc"][0], frame["merc"][1], frame["k"]
    found: list[tuple[float, list[float]]] = []
    for _, el in ET.iterparse(stream, events=("end",)):
        if el.tag != "way":
            continue
        pts, tags = [], {}
        for ch in el:
            if ch.tag == "nd" and "lat" in ch.attrib:
                try:
                    mxy = merc(float(ch.attrib["lat"]), float(ch.attrib["lon"]))
                except ValueError:
                    continue
                pts.append(((mxy[0] - cx) * k, (mxy[1] - cy) * k))
            elif ch.tag == "tag":
                tags[ch.attrib.get("k", "")] = ch.attrib.get("v", "")
        el.clear()
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        box = footprint_box(pts)
        if not box:
            continue
        dist = near.dist(box[0], box[1])
        if dist > BUILDING_REACH_M:
            continue
        found.append((dist, [round(box[0], 1), round(box[1], 1), round(box[2], 1), round(box[3], 1),
                             round(box[4], 3), round(building_height(tags), 1)]))
    found.sort(key=lambda f: f[0])
    return [b for _, b in found[:MAX_BUILDINGS]]


def fetch_buildings(frame: dict, track: dict) -> list[list[float]]:
    body = urllib.parse.urlencode({"data": overpass_query(frame, track)}).encode()
    req = urllib.request.Request(OVERPASS_URL, data=body, headers={
        "User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/xml"})
    try:
        with net.open_url(req, timeout=180) as resp:
            return parse_buildings(io.BufferedReader(_Limited(resp, MAX_OSM_BYTES)), frame, track)
    except ET.ParseError as e:
        raise RecapError("OpenStreetMap sent something unexpected.") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RecapError("Couldn't reach OpenStreetMap for building outlines.") from e


class _Limited(io.RawIOBase):
    """A read-only stream that stops with an error after `limit` bytes."""

    def __init__(self, inner, limit: int):
        self.inner, self.left = inner, limit

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if self.left <= 0:
            raise RecapError("The download was unexpectedly large.")
        chunk = self.inner.read(min(len(b), self.left, 1 << 16))
        n = len(chunk)
        b[:n] = chunk
        self.left -= n
        return n


# ------------------------------------------------------------------ cache
def _dir(settings: config.Settings) -> Path:
    d = settings.data_dir / "recap"
    config.ensure_private_dir(d)
    return d


def image_path(settings: config.Settings, activity_id: int) -> Path:
    return _dir(settings) / f"{int(activity_id)}.jpg"


def _prune_images(settings: config.Settings) -> None:
    files = sorted(_dir(settings).glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP_IMAGES:]:
        old.unlink(missing_ok=True)


def wipe(settings: config.Settings) -> None:
    """Delete every cached photo (the database rows go with the Garmin data)."""
    d = settings.data_dir / "recap"
    if d.exists():
        for f in d.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)


def _goal(db: Database) -> dict | None:
    p = prefs.get_all(db)
    km, goal = prefs.race_km(p), prefs.goal_seconds(p.get("race_goal_time", ""))
    if not p.get("race_enabled") or not km or not goal:
        return None
    return {"name": p.get("race_name") or "Goal race", "km": km, "goal_s": goal}


def hr_max(db: Database, run_start: str | None) -> int | None:
    """Your highest heart rate over the year before this run: a fair stand-in for max heart rate,
    which sets the zones (Garmin's default bands are percentages of it)."""
    end = (run_start or "9999")[:10]
    with db.connect() as con:
        row = con.execute("SELECT MAX(max_hr) FROM activities WHERE max_hr BETWEEN 120 AND 230 AND substr(start_local,1,10) <= ? "
                          "AND substr(start_local,1,10) >= date(?, '-365 day')", (end, end if run_start else "now")).fetchone()
    v = row[0] if row else None
    return int(v) if v and 120 <= v <= 230 else None


def payload(settings: config.Settings, db: Database, activity_id: int) -> dict | None:
    """Everything the page needs for one run, or None if it hasn't been prepared."""
    track = db.recap_get(activity_id, "track")
    if not track:
        return None
    run = _run_row(db, activity_id)
    extra = db.recap_get(activity_id, "extras") or {}
    has_photo = image_path(settings, activity_id).exists()
    if has_photo:
        os.utime(image_path(settings, activity_id))  # keep recently used photos when pruning
    p = prefs.get_all(db)
    return {
        "id": activity_id, "name": run["name"] or "Run", "start_local": run["start_local"],
        "units": p["units"], "goal": _goal(db), "hr_max": hr_max(db, run["start_local"]),
        **{k: track[k] for k in ("points", "splits", "totals")},
        "side_m": track["frame"]["side_m"],
        "imagery": {"available": has_photo, "credit": NAIP_CREDIT if has_photo else None,
                    "reason": extra.get("imagery_error")},
        "buildings": extra.get("buildings") or [],
        "buildings_credit": OSM_CREDIT if extra.get("buildings") else None,
        "buildings_reason": extra.get("buildings_error"),
    }


# ------------------------------------------------------------------ preparing a run (background job)
_jobs: dict[int, dict] = {}
_jobs_lock = threading.Lock()
_one_at_a_time = threading.Lock()


def job_state(activity_id: int) -> dict:
    with _jobs_lock:
        return dict(_jobs.get(activity_id) or {"state": "idle"})


def _set(activity_id: int, **kw) -> None:
    with _jobs_lock:
        _jobs.setdefault(activity_id, {}).update(kw)


def start(settings: config.Settings, db: Database, activity_id: int, retry: bool = False) -> dict:
    """Begin preparing a run in the background; returns the job's state right away."""
    _run_row(db, activity_id)  # refuses anything that isn't one of your outdoor runs
    with _jobs_lock:
        cur = _jobs.get(activity_id)
        if cur and cur.get("state") == "running":
            return dict(cur)
        if any(j.get("state") == "running" for j in _jobs.values()):
            raise RecapError("Another run is being prepared. Try again when it's done.")
        _jobs[activity_id] = {"state": "running", "step": "Starting", "started": time.time()}
    threading.Thread(target=_prepare, args=(settings, db, activity_id, retry), daemon=True,
                     name=f"recap-{activity_id}").start()
    return job_state(activity_id)


def _download_fit(settings: config.Settings, db: Database, activity_id: int) -> bytes:
    from . import sync

    if not settings.api_key():
        raise RecapError("Add your Garmin Personal API key in Settings → Garmin first.")
    # Garmin's tokens and the database are shared with the regular sync: wait for it briefly.
    for _ in range(40):
        with sync.data_lock(settings) as held:
            if held:
                syncer = sync.GarminSync(settings, db)
                try:
                    return syncer.call(syncer.client.download_activity, activity_id)
                finally:
                    syncer.close()
        time.sleep(1.5)
    raise RecapError("A Garmin sync is running. Try again in a minute.")


def _prepare(settings: config.Settings, db: Database, activity_id: int, retry: bool) -> None:
    try:
        with _one_at_a_time:
            track = db.recap_get(activity_id, "track")
            if not track:
                _set(activity_id, step="Downloading the run from Garmin")
                try:
                    blob = _download_fit(settings, db, activity_id)
                except RecapError:
                    raise
                except Exception as e:
                    log.warning("recap download failed: %s", type(e).__name__)
                    raise RecapError("Couldn't download this run from Garmin. Try again later.") from e
                _set(activity_id, step="Reading the GPS track")
                track = build_track(parse_fit(fit_from_zip(blob)))
                db.recap_put(activity_id, "track", track)
            frame = track["frame"]
            extra = db.recap_get(activity_id, "extras") or {}
            now = time.time()

            def due(key: str) -> bool:
                return retry or now - extra.get(key + "_at", 0) > RETRY_AFTER_S

            img = image_path(settings, activity_id)
            if not img.exists() and due("imagery"):
                if not naip_covers(frame):
                    extra.update(imagery_error="Aerial photos are only available for the US mainland.", imagery_at=now)
                else:
                    _set(activity_id, step="Downloading the aerial photo")
                    try:
                        fetch_imagery(frame, img)
                        extra.pop("imagery_error", None)
                        _prune_images(settings)
                    except RecapError as e:
                        extra.update(imagery_error=str(e), imagery_at=now)
            if not extra.get("buildings") and due("buildings"):
                _set(activity_id, step="Finding buildings along the route")
                try:
                    extra["buildings"] = fetch_buildings(frame, track)
                    extra.pop("buildings_error", None)
                    extra["buildings_at"] = now
                except RecapError as e:
                    extra.update(buildings_error=str(e), buildings_at=now)
            db.recap_put(activity_id, "extras", extra)
        _set(activity_id, state="done", step="Ready")
    except RecapError as e:
        _set(activity_id, state="error", error=str(e))
    except Exception:
        log.exception("Preparing recap %s failed", activity_id)
        _set(activity_id, state="error", error="Something went wrong while preparing this run.")


def forget_jobs() -> None:
    with _jobs_lock:
        _jobs.clear()
