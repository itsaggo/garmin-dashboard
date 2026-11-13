"""A tiny FIT file writer for tests: just enough of the format (header, definition and data messages, CRCs)
to make activity files the way a Garmin watch does."""
from __future__ import annotations

import struct
from datetime import datetime, timezone

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc).timestamp()
_CRC = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
        0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]
# base type id → struct code
_BASE = {0x00: "B", 0x02: "B", 0x84: "H", 0x85: "i", 0x86: "I", 0x83: "h"}
_INVALID = {0x00: 0xFF, 0x02: 0xFF, 0x84: 0xFFFF, 0x85: 0x7FFFFFFF, 0x86: 0xFFFFFFFF, 0x83: 0x7FFF}


def crc(data: bytes, c: int = 0) -> int:
    for b in data:
        t = _CRC[c & 0xF]; c = (c >> 4) & 0x0FFF; c = c ^ t ^ _CRC[b & 0xF]
        t = _CRC[c & 0xF]; c = (c >> 4) & 0x0FFF; c = c ^ t ^ _CRC[(b >> 4) & 0xF]
    return c


# global message number, then fields: name → (field number, base type, scale, offset)
MESSAGES = {
    "file_id": (0, {"type": (0, 0x00, 1, 0), "manufacturer": (1, 0x84, 1, 0), "product": (2, 0x84, 1, 0),
                    "time_created": (4, 0x86, 1, 0)}),
    "event": (21, {"timestamp": (253, 0x86, 1, 0), "event": (0, 0x00, 1, 0), "event_type": (1, 0x00, 1, 0)}),
    "record": (20, {"timestamp": (253, 0x86, 1, 0), "position_lat": (0, 0x85, 1, 0), "position_long": (1, 0x85, 1, 0),
                    "heart_rate": (3, 0x02, 1, 0), "cadence": (4, 0x02, 1, 0), "distance": (5, 0x86, 100, 0),
                    "enhanced_altitude": (78, 0x86, 5, 500), "fractional_cadence": (53, 0x02, 128, 0)}),
    "session": (18, {"timestamp": (253, 0x86, 1, 0), "sport": (5, 0x00, 1, 0), "total_elapsed_time": (7, 0x86, 1000, 0), "total_timer_time": (8, 0x86, 1000, 0),
                     "total_distance": (9, 0x86, 100, 0), "avg_heart_rate": (16, 0x02, 1, 0),
                     "max_heart_rate": (17, 0x02, 1, 0), "avg_cadence": (18, 0x02, 1, 0), "total_ascent": (22, 0x84, 1, 0)}),
}
EVENT_TIMER, START, STOP_ALL = 0, 0, 4


def fit_time(ts: float) -> int:
    return int(round(ts - FIT_EPOCH))


def semis(deg: float) -> int:
    return int(round(deg * 2 ** 31 / 180))


class FitWriter:
    def __init__(self):
        self.body = bytearray()
        self.local: dict[tuple, int] = {}

    def add(self, name: str, **values) -> None:
        gnum, fields = MESSAGES[name]
        keys = tuple(sorted(values))
        sig = (name, keys)
        if sig not in self.local:
            local = len(self.local) % 16
            self.local[sig] = local
            self.body += struct.pack("<BBBHB", 0x40 | local, 0, 0, gnum, len(keys))
            for k in keys:
                num, base, _, _ = fields[k]
                self.body += struct.pack("<BBB", num, struct.calcsize("<" + _BASE[base]), base)
        self.body += struct.pack("<B", self.local[sig])
        for k in keys:
            _, base, scale, offset = fields[k]
            v = values[k]
            raw = _INVALID[base] if v is None else int(round((v + offset) * scale))
            self.body += struct.pack("<" + _BASE[base], raw)

    def bytes(self) -> bytes:
        header = struct.pack("<BBHI4s", 14, 0x20, 2132, len(self.body), b".FIT")
        header += struct.pack("<H", crc(header))
        out = header + bytes(self.body)
        return out + struct.pack("<H", crc(out))


def run_file(start_ts: float, points: list[dict], pause: tuple[int, int] | None = None, session: dict | None = None) -> bytes:
    """points: dicts with t (seconds after start), lat, lon, d (m), hr, cad (strides/min), alt (m).
    pause: (stop_at_t, resume_at_t) adds timer stop/start events around a pause."""
    w = FitWriter()
    w.add("file_id", type=4, manufacturer=1, product=4257, time_created=fit_time(start_ts))
    w.add("event", timestamp=fit_time(start_ts), event=EVENT_TIMER, event_type=START)
    for p in points:
        if pause and p["t"] == pause[0]:
            w.add("event", timestamp=fit_time(start_ts + p["t"]), event=EVENT_TIMER, event_type=STOP_ALL)
        if pause and p["t"] == pause[1]:
            w.add("event", timestamp=fit_time(start_ts + p["t"]), event=EVENT_TIMER, event_type=START)
        w.add("record", timestamp=fit_time(start_ts + p["t"]),
              position_lat=None if p.get("lat") is None else semis(p["lat"]),
              position_long=None if p.get("lon") is None else semis(p["lon"]),
              heart_rate=p.get("hr"), cadence=p.get("cad"), distance=p.get("d"),
              enhanced_altitude=p.get("alt"), fractional_cadence=0)
    end = start_ts + points[-1]["t"]
    w.add("event", timestamp=fit_time(end), event=EVENT_TIMER, event_type=STOP_ALL)
    if session:
        w.add("session", timestamp=fit_time(end), sport=1, **session)
    return w.bytes()
