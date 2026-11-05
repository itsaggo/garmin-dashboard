"""A tiny fake Garmin Personal API for testing the sync without a real key.

    python tests/mock_garmin.py 9911 &
    GARMIN_PERSONAL_API_BASE_URL=http://127.0.0.1:9911 GARMIN_PERSONAL_API_KEY=test python -m app.sync

It deliberately rejects step ranges over 14 days (to exercise chunk-splitting)
and answers the first stress request with HTTP 429 (to exercise backoff).
"""
import base64
import json
import random
import sys
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

rnd = random.Random(1)
state = {"stress_429": True}


def jwt() -> str:
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b({'alg': 'none'})}.{b({'exp': int(time.time()) + 86400})}.sig"


def days(a: str, b: str):
    d, end = date.fromisoformat(a), date.fromisoformat(b)
    while d <= end:
        yield d.isoformat()
        d += timedelta(days=1)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, text=False):
        data = body.encode() if text else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain" if text else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.endswith("/personal-api-key/token") and self.headers.get("Authorization") == "ApiKey test":
            return self.send(200, jwt(), text=True)
        self.send(401, {"error": "bad key"})

    def do_GET(self):
        if self.headers.get("Authorization", "").split(" ")[0] != "Bearer":
            return self.send(401, {})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path.split("/")
        if u.path.startswith("/usersummary-service/stats/steps/daily/"):
            a, b = p[-2], p[-1]
            if (date.fromisoformat(b) - date.fromisoformat(a)).days > 13:
                return self.send(400, {"message": "range too long"})
            return self.send(200, [{"calendarDate": d, "totalSteps": rnd.randint(4000, 15000), "stepGoal": 9000,
                                    "totalDistance": rnd.randint(3000, 11000)} for d in days(a, b)])
        if u.path.startswith("/usersummary-service/stats/stress/daily/"):
            if state["stress_429"]:
                state["stress_429"] = False
                return self.send(429, {})
            return self.send(200, [{"calendarDate": d, "values": {"overallStressLevel": rnd.randint(15, 45)}}
                                   for d in days(p[-2], p[-1])])
        if u.path == "/usersummary-service/v2/dailysummaries":
            return self.send(200, [{"calendarDate": d, "restingHeartRate": rnd.randint(50, 58),
                                    "bodyBatteryHighestValue": rnd.randint(60, 100), "bodyBatteryLowestValue": rnd.randint(5, 40),
                                    "averageStressLevel": -1, "totalKilocalories": 2500}
                                   for d in days(q["startDate"], q["endDate"])])
        if u.path == "/sleep-service/sleep/dailySleepSummariesByDate":
            return self.send(200, {"dailySleepSummaries": [
                {"dailySleepDTO": {"calendarDate": d, "sleepTimeSeconds": 26000, "deepSleepSeconds": 4000,
                                   "lightSleepSeconds": 16000, "remSleepSeconds": 6000, "awakeSleepSeconds": 900,
                                   "sleepScores": {"overall": {"value": rnd.randint(60, 90)}}}}
                for d in days(q["startDate"], q["endDate"])]})
        if u.path.startswith("/hrv-service/hrv/daily/"):
            return self.send(200, {"hrvSummaries": [{"calendarDate": d, "lastNightAvg": rnd.randint(45, 70),
                                                     "weeklyAvg": 58, "status": "BALANCED"} for d in days(p[-2], p[-1])]})
        if u.path == "/wellness-service/wellness/bodyBattery/reports/daily":
            return self.send(200, [{"date": d, "charged": 60, "drained": 55,
                                    "bodyBatteryValuesArray": [[0, 30], [1, 85], [2, 22]]}
                                   for d in days(q["startDate"], q["endDate"])])
        if u.path == "/activitylist-service/activities/search/activities":
            start, limit = int(q.get("start", 0)), int(q.get("limit", 20))
            acts = [{"activityId": 900 + i, "activityName": f"Run {i}", "startTimeLocal": f"{d} 06:30:00",
                     "activityType": {"typeKey": "running"}, "distance": 8046.7, "duration": 2880.0,
                     "averageHR": 150, "averageSpeed": 2.79, "elevationGain": 20}
                    for i, d in enumerate(list(days(q["startDate"], q["endDate"]))[::2])]
            return self.send(200, acts[start:start + limit])
        if u.path == "/userprofile-service/userprofile/settings":
            return self.send(200, {"measurementSystem": "statute_us"})
        self.send(404, {})


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1] if len(sys.argv) > 1 else 9911)), H).serve_forever()
