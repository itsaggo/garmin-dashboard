"""v1.4: race recap — run list, activity file reading, aerial photo and buildings, the prepare job."""
import io
import math
import time
import zipfile

import pytest

from app import main, recap
from tests.fitgen import run_file
from tests.helpers import JSON, PASSWORD, client as new_client, login, make_account, reset

LAT0, LON0 = 29.7604, -95.3698          # Houston
SPEED = 3.2                              # m/s, about 8:23 per mile
LOOP_M = 3840.0
R_LOOP = LOOP_M / (2 * math.pi)


def _loop_points(pause=(400, 460), seconds=1200, gps=True):
    """A loop run at a steady pace, one sample a second, standing still during the pause."""
    pts, d = [], 0.0
    for t in range(seconds + 1):
        if t and not (pause and pause[0] < t <= pause[1]):
            d += SPEED
        a = d / R_LOOP
        lat = LAT0 + R_LOOP * math.sin(a) / 111320
        lon = LON0 + R_LOOP * (math.cos(a) - 1) / (111320 * math.cos(math.radians(LAT0)))
        pts.append({"t": t, "lat": lat if gps else None, "lon": lon if gps else None, "d": round(d, 2),
                    "hr": 140 + t * 30 // seconds, "cad": 85, "alt": 15 + 3 * math.sin(a)})
    return pts


SESSION = {"total_timer_time": 1140, "total_elapsed_time": 1200, "total_distance": 3840, "avg_heart_rate": 155,
           "max_heart_rate": 171, "avg_cadence": 85, "total_ascent": 6}


def _fit(**kw):
    return run_file(1_762_000_000, _loop_points(**kw), pause=kw.get("pause", (400, 460)), session=SESSION)


def _zip(fit: bytes, name="123_ACTIVITY.fit") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, fit)
    return buf.getvalue()


# ------------------------------------------------------------------ reading the activity file
def test_track_from_fit_leaves_out_the_pause():
    tr = recap.build_track(recap.parse_fit(_fit()))
    tot = tr["totals"]
    assert tot["timer_s"] == 1140 and tot["distance_m"] == 3840
    assert tot["avg_hr"] == 155 and tot["max_hr"] == 171 and tot["gain_m"] == 6
    assert tot["avg_cadence"] == 170                       # strides per minute → steps per minute
    mi, km = tr["splits"]["mi"], tr["splits"]["km"]
    assert len(mi) == 2 and len(km) == 3
    assert mi[0] == pytest.approx(1609.344 / SPEED, abs=1.0)  # the 60 s pause isn't counted
    assert km[0] == pytest.approx(1000 / SPEED, abs=1.0)
    p = tr["points"]
    assert len(p["x"]) == len(p["y"]) == len(p["d"]) == len(p["t"]) == len(p["hr"]) == len(p["cad"])
    assert p["cad"][10] == 170 and p["t"][-1] == pytest.approx(1140, abs=0.5)
    # local metres around the middle of the route: a loop about 1.2 km across
    assert max(p["x"]) - min(p["x"]) == pytest.approx(2 * R_LOOP, rel=0.02)
    assert abs(max(p["y"]) + min(p["y"])) < 2 and abs(max(p["x"]) + min(p["x"])) < 2
    f = tr["frame"]
    assert f["side_m"] == pytest.approx(2 * R_LOOP + 2 * recap.MARGIN_M, rel=0.02)
    assert f["center"][0] == pytest.approx(LAT0 + R_LOOP / 111320 * 0, abs=0.01)


def test_long_runs_are_thinned_for_the_browser():
    tr = recap.build_track(recap.parse_fit(run_file(1_762_000_000, _loop_points(pause=None, seconds=9000),
                                                    session=None)))
    assert len(tr["points"]["x"]) <= recap.MAX_POINTS + 1
    assert tr["points"]["d"][-1] == pytest.approx(9000 * SPEED, rel=0.001)   # the last point is kept


def test_run_without_gps_is_refused():
    with pytest.raises(recap.RecapError, match="no GPS"):
        recap.build_track(recap.parse_fit(run_file(1_762_000_000, _loop_points(gps=False), session=SESSION)))


def test_damaged_file_is_refused():
    fit = bytearray(_fit())
    with pytest.raises(recap.RecapError):
        recap.build_track(recap.parse_fit(bytes(fit[:14]) + b"\x55" * 40))


def test_zip_handling_and_limits(monkeypatch):
    fit = _fit()
    assert recap.fit_from_zip(_zip(fit)) == fit
    assert recap.fit_from_zip(fit) == fit                            # some downloads aren't zipped
    with pytest.raises(recap.RecapError, match="isn't a FIT"):
        recap.fit_from_zip(_zip(b"<gpx/>", name="run.gpx"))
    with pytest.raises(recap.RecapError):
        recap.fit_from_zip(b"<html>error</html>")
    monkeypatch.setattr(recap, "MAX_FIT_BYTES", 1000)               # a member bigger than allowed
    with pytest.raises(recap.RecapError, match="large"):
        recap.fit_from_zip(_zip(b"\0" * 5000))
    monkeypatch.setattr(recap, "MAX_ZIP_BYTES", 10)
    with pytest.raises(recap.RecapError, match="large"):
        recap.fit_from_zip(_zip(fit))


def test_outdoor_runs_only():
    assert recap.is_outdoor_run("running") and recap.is_outdoor_run("trail_running") and recap.is_outdoor_run("track_running")
    for t in ("treadmill_running", "indoor_running", "virtual_run", "cycling", "", None):
        assert not recap.is_outdoor_run(t)


# ------------------------------------------------------------------ aerial photo
def test_photo_request_is_a_square_around_the_route_only():
    tr = recap.build_track(recap.parse_fit(_fit()))
    url = recap.naip_url(tr["frame"])
    from urllib.parse import parse_qs, urlsplit
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    x0, y0, x1, y1 = map(float, q["bbox"].split(","))
    assert x1 - x0 == pytest.approx(y1 - y0, abs=0.05)               # square (to the centimetre)
    assert (x1 - x0) * tr["frame"]["k"] == pytest.approx(tr["frame"]["side_m"], abs=0.1)
    assert q["size"] == "4000,4000" and q["bandIds"] == "0,1,2" and q["f"] == "image" and q["bboxSR"] == "3857"
    assert url.startswith(recap.NAIP_URL) and set(q) == {"bbox", "bboxSR", "imageSR", "size", "format",
                                                         "compressionQuality", "bandIds", "interpolation", "f"}
    assert recap.naip_covers(tr["frame"])
    london = dict(tr["frame"], merc=list(recap.merc(51.5, -0.12)))
    assert not recap.naip_covers(london)


def test_photo_must_be_a_jpeg(tmp_path, monkeypatch):
    tr = recap.build_track(recap.parse_fit(_fit()))

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(recap.net, "open_url", lambda req, timeout: Resp(b'{"error": {"code": 500}}'))
    with pytest.raises(recap.RecapError, match="didn't send a photo"):
        recap.fetch_imagery(tr["frame"], tmp_path / "a.jpg")
    assert not (tmp_path / "a.jpg").exists()
    monkeypatch.setattr(recap.net, "open_url", lambda req, timeout: Resp(b"\xff\xd8\xff\xe0" + b"x" * 100))
    recap.fetch_imagery(tr["frame"], tmp_path / "a.jpg")
    assert (tmp_path / "a.jpg").read_bytes().startswith(b"\xff\xd8\xff")
    assert (tmp_path / "a.jpg").stat().st_mode & 0o777 == 0o600


# ------------------------------------------------------------------ buildings
def test_building_heights():
    h = recap.building_height
    assert h({"height": "45"}) == 45 and h({"height": "100 ft"}) == pytest.approx(30.48)
    assert h({"building:levels": "10"}) == pytest.approx(33)
    assert h({"building": "house"}) == 6 and h({"building": "garage"}) == 3 and h({"height": "abc"}) == 6
    assert h({"height": "9000"}) == 6                                # nonsense falls back to a guess


def test_footprint_box_follows_the_longest_wall():
    a = math.radians(30)
    corners = [(0, 0), (40, 0), (40, 10), (0, 10)]
    rot = [(x * math.cos(a) - y * math.sin(a) + 100, x * math.sin(a) + y * math.cos(a) + 50) for x, y in corners]
    cx, cy, w, d, ang = recap.footprint_box(rot)
    assert (w, d) == (pytest.approx(40), pytest.approx(10)) and ang == pytest.approx(a, abs=1e-6)
    mid = (rot[0][0] + rot[2][0]) / 2, (rot[0][1] + rot[2][1]) / 2
    assert (cx, cy) == (pytest.approx(mid[0]), pytest.approx(mid[1]))
    assert recap.footprint_box([(0, 0), (1, 0), (1, 1)]) is None      # too small to draw


def _way(lat, lon, size_deg=0.0002, tags=None):
    nds = [(lat, lon), (lat, lon + size_deg), (lat + size_deg, lon + size_deg), (lat + size_deg, lon), (lat, lon)]
    t = "".join(f'<tag k="{k}" v="{v}"/>' for k, v in (tags or {"building": "yes"}).items())
    return "<way id='1'>" + "".join(f'<nd ref="1" lat="{a}" lon="{b}"/>' for a, b in nds) + t + "</way>"


def test_buildings_near_the_route_become_boxes():
    tr = recap.build_track(recap.parse_fit(_fit()))
    near = _way(LAT0 + 0.0005, LON0 - 0.0003, tags={"building": "office", "height": "120"})
    far = _way(LAT0 + 0.05, LON0 + 0.05)
    xml = f"<osm>{near}{far}<node id='5' lat='1' lon='2'/></osm>".encode()
    boxes = recap.parse_buildings(io.BytesIO(xml), tr["frame"], tr)
    assert len(boxes) == 1
    x, y, w, d, ang, h = boxes[0]
    assert h == 120 and 15 < w < 25 and 15 < d < 25
    q = recap.overpass_query(tr["frame"], tr)
    assert q.startswith("[out:xml]") and 'way["building"](' in q and "poly" not in q and "around" not in q


def test_download_size_limit_while_streaming():
    stream = io.BufferedReader(recap._Limited(io.BytesIO(b"<osm>" + b" " * 5000 + b"</osm>"), 100))
    with pytest.raises(recap.RecapError, match="large"):
        stream.read()


# ------------------------------------------------------------------ pages and the prepare job
RUN_ID, TREAD_ID = 9001, 9002


@pytest.fixture()
def c(monkeypatch):
    reset()
    recap.forget_jobs()
    recap.wipe(main.settings)
    main.db.upsert_activity(RUN_ID, {"start_local": "2026-11-08 07:00:00", "name": "Houston Running", "type": "running",
                                     "distance_m": 3840, "duration_s": 1200}, {})
    main.db.upsert_activity(TREAD_ID, {"start_local": "2026-11-09 07:00:00", "name": "Treadmill", "type": "treadmill_running",
                                       "distance_m": 5000, "duration_s": 1500}, {})
    calls = {"download": 0, "photo": 0, "osm": 0}

    def download(settings, db, aid):
        calls["download"] += 1
        return _zip(_fit())

    def photo(frame, path):
        calls["photo"] += 1
        path.write_bytes(b"\xff\xd8\xff\xe0JPEGDATA")

    def osm(frame, track):
        calls["osm"] += 1
        return [[10.0, 20.0, 15.0, 12.0, 0.5, 30.0]]

    monkeypatch.setattr(recap, "_download_fit", download)
    monkeypatch.setattr(recap, "fetch_imagery", photo)
    monkeypatch.setattr(recap, "fetch_buildings", osm)
    cl = new_client()
    make_account()
    login(cl)
    cl.calls = calls
    return cl


def _wait(c, aid=RUN_ID):
    for _ in range(200):
        st = c.get(f"/api/recap/{aid}/status").json()
        if st["state"] != "running":
            return st
        time.sleep(0.02)
    raise AssertionError("prepare never finished")


def test_recap_needs_sign_in_and_the_dashboard_header():
    reset()
    anon = new_client()
    make_account()
    for path in ("/api/recap/runs", f"/api/recap/{RUN_ID}", f"/api/recap/{RUN_ID}/status", f"/api/recap/{RUN_ID}/photo"):
        assert anon.get(path).status_code == 401
    assert anon.get("/recap").status_code == 303
    login(anon)
    r = anon.post(f"/api/recap/{RUN_ID}/prepare", json={}, headers={"Origin": "http://testserver"})
    assert r.status_code == 403                                      # no X-Requested-With


def test_run_list_shows_outdoor_runs_only(c):
    r = c.get("/api/recap/runs").json()
    assert [x["id"] for x in r["runs"]] == [RUN_ID] and r["runs"][0]["ready"] is False and r["units"] == "imperial"


def test_prepare_then_render_data(c):
    assert c.get(f"/api/recap/{RUN_ID}").json() == {"prepared": False, "job": {"state": "idle"}}
    r = c.post(f"/api/recap/{RUN_ID}/prepare", json={}, headers=JSON)
    assert r.status_code == 200 and r.json()["state"] in ("running", "done")
    assert _wait(c)["state"] == "done"
    body = c.get(f"/api/recap/{RUN_ID}").json()
    assert body["prepared"] is True
    d = body["recap"]
    assert d["name"] == "Houston Running" and d["totals"]["timer_s"] == 1140 and len(d["splits"]["mi"]) == 2
    assert d["imagery"] == {"available": True, "credit": recap.NAIP_CREDIT, "reason": None}
    assert d["buildings"] == [[10.0, 20.0, 15.0, 12.0, 0.5, 30.0]] and d["buildings_credit"] == recap.OSM_CREDIT
    assert "frame" not in d and "merc" not in str(d)                 # map coordinates stay on the server
    assert d["hr_max"] is None                                       # no heart rate in the synced summaries yet
    photo = c.get(f"/api/recap/{RUN_ID}/photo")
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"
    assert photo.headers["x-content-type-options"] == "nosniff"
    assert c.get("/api/recap/runs").json()["runs"][0]["ready"] is True
    # a second prepare reuses everything already downloaded
    c.post(f"/api/recap/{RUN_ID}/prepare", json={}, headers=JSON)
    _wait(c)
    assert c.calls == {"download": 1, "photo": 1, "osm": 1}


def test_goal_from_race_settings(c):
    r = c.post("/api/settings", json={"values": {"race_enabled": True, "race_date": "2026-11-14", "race_distance": "half",
                                                 "race_goal_time": "1:52:00"}}, headers=JSON)
    assert r.status_code == 200
    c.post(f"/api/recap/{RUN_ID}/prepare", json={}, headers=JSON)
    _wait(c)
    g = c.get(f"/api/recap/{RUN_ID}").json()["recap"]["goal"]
    assert g == {"name": "Goal race", "km": 21.0975, "goal_s": 6720}


def test_optional_downloads_can_fail(c, monkeypatch):
    def broken(*a):
        raise recap.RecapError("Couldn't reach the USGS imagery service.")

    monkeypatch.setattr(recap, "fetch_imagery", broken)
    monkeypatch.setattr(recap, "fetch_buildings", broken)
    c.post(f"/api/recap/{RUN_ID}/prepare", json={}, headers=JSON)
    assert _wait(c)["state"] == "done"                               # the run still works without them
    d = c.get(f"/api/recap/{RUN_ID}").json()["recap"]
    assert d["imagery"]["available"] is False and "USGS" in d["imagery"]["reason"]
    assert d["buildings"] == [] and d["buildings_reason"]
    assert c.get(f"/api/recap/{RUN_ID}/photo").status_code == 404
    # not retried on every visit…
    c.post(f"/api/recap/{RUN_ID}/prepare", json={}, headers=JSON)
    _wait(c)
    monkeypatch.setattr(recap, "fetch_imagery", lambda frame, path: path.write_bytes(b"\xff\xd8\xff\xe0J"))
    c.post(f"/api/recap/{RUN_ID}/prepare", json={}, headers=JSON)
    _wait(c)
    assert c.get(f"/api/recap/{RUN_ID}").json()["recap"]["imagery"]["available"] is False
    # …but on request
    c.post(f"/api/recap/{RUN_ID}/prepare", json={"retry": True}, headers=JSON)
    _wait(c)
    assert c.get(f"/api/recap/{RUN_ID}").json()["recap"]["imagery"]["available"] is True


def test_download_failure_is_reported(c, monkeypatch):
    def fail(settings, db, aid):
        raise recap.RecapError("A Garmin sync is running. Try again in a minute.")

    monkeypatch.setattr(recap, "_download_fit", fail)
    c.post(f"/api/recap/{RUN_ID}/prepare", json={}, headers=JSON)
    st = _wait(c)
    assert st["state"] == "error" and "sync is running" in st["error"]
    assert c.get(f"/api/recap/{RUN_ID}").json()["prepared"] is False


def test_only_your_outdoor_runs(c):
    assert c.post(f"/api/recap/{TREAD_ID}/prepare", json={}, headers=JSON).status_code == 400
    r = c.post("/api/recap/123456/prepare", json={}, headers=JSON)
    assert r.status_code == 400 and "isn't in your synced" in r.json()["detail"]
    assert c.get("/api/recap/0").status_code == 404
    assert c.get("/api/recap/-5/status").status_code == 404
    assert c.get("/api/recap/abc").status_code == 422
    assert c.get(f"/api/recap/{RUN_ID}/photo").status_code == 404


def test_deleting_data_removes_recaps(c):
    c.post(f"/api/recap/{RUN_ID}/prepare", json={}, headers=JSON)
    _wait(c)
    assert recap.image_path(main.settings, RUN_ID).exists()
    r = c.post("/api/data/delete", json={"password": PASSWORD, "scope": "garmin_data"}, headers=JSON)
    assert r.status_code == 200
    assert main.db.recap_get(RUN_ID, "track") is None and not recap.image_path(main.settings, RUN_ID).exists()


def test_max_heart_rate_from_the_past_year():
    reset()
    for i, (day, hr) in enumerate((("2025-06-01", 199), ("2026-03-01", 186), ("2026-10-01", 181), ("2026-12-01", 195))):
        main.db.upsert_activity(500 + i, {"start_local": f"{day} 07:00:00", "type": "running", "max_hr": hr}, {})
    assert recap.hr_max(main.db, "2026-11-08 07:00:00") == 186      # older than a year and later runs don't count
    main.db.upsert_activity(600, {"start_local": "2026-11-01 07:00:00", "type": "running", "max_hr": 250}, {})
    assert recap.hr_max(main.db, "2026-11-08 07:00:00") == 186      # a sensor glitch is ignored


def test_old_photos_are_pruned(monkeypatch):
    reset()
    recap.wipe(main.settings)
    monkeypatch.setattr(recap, "KEEP_IMAGES", 2)
    for i in range(4):
        p = recap.image_path(main.settings, 100 + i)
        p.write_bytes(b"x")
        t = time.time() - 100 + i
        import os
        os.utime(p, (t, t))
    recap._prune_images(main.settings)
    left = sorted(p.name for p in (main.settings.data_dir / "recap").glob("*.jpg"))
    assert left == ["102.jpg", "103.jpg"]
    assert (main.settings.data_dir / "recap").stat().st_mode & 0o777 == 0o700
