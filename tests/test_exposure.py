"""Hardening for running behind a reverse proxy or tunnel on the public internet."""
import dataclasses
import ipaddress
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from app import ai, auth, main, net
from tests.helpers import JSON, ORIGIN, PASSWORD, USER, client as new_client, login, make_account, reset

PROXY = ("172.18.0.5", 40000)       # the tunnel container, as the app sees it
NET = (ipaddress.ip_network("172.18.0.0/16"),)


def proxied(**kw):
    return TestClient(main.app, follow_redirects=False, client=PROXY, **kw)


def bad_login(c, ip=None, xff=None, user=USER):
    h = dict(ORIGIN)
    if ip:
        h["CF-Connecting-IP"], h["CF-Ray"] = ip, "8a1b2c3d4e5f-IAH"
    if xff:
        h["X-Forwarded-For"] = xff
    return c.post("/login", data={"username": user, "password": "not the password"}, headers=h).headers["location"]


@pytest.fixture()
def tunnel(monkeypatch):
    reset()
    make_account()
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, trusted_proxies=NET))


# ------------------------------------------------------------------ who is the visitor?
def test_each_visitor_behind_the_tunnel_is_limited_separately(tunnel):
    c = proxied()
    assert [bad_login(c, "198.51.100.1") for _ in range(6)] == ["/login?m=bad"] * 5 + ["/login?m=locked"]
    assert bad_login(c, "198.51.100.2") == "/login?m=bad"  # someone else isn't locked out by the first visitor
    r = c.post("/login", data={"username": USER, "password": PASSWORD}, headers={**ORIGIN, "CF-Connecting-IP": "198.51.100.3", "CF-Ray": "x-IAH"})
    assert r.headers["location"] == "/"


def test_forwarding_headers_from_untrusted_addresses_are_ignored():
    reset()
    make_account()
    c = TestClient(main.app, follow_redirects=False, client=("203.0.113.50", 40000))  # not a trusted proxy
    seen = [bad_login(c, f"198.51.100.{i}") for i in range(7)]
    assert seen == ["/login?m=bad"] * 5 + ["/login?m=locked"] * 2  # pretending to be 7 visitors didn't help


def test_only_the_proxys_own_forwarded_for_entry_counts(tunnel):
    c = proxied()
    # The visitor controls everything left of the last entry; the proxy appends the real address.
    seen = [bad_login(c, xff=f"10.9.9.{i}, 198.51.100.9") for i in range(6)]
    assert seen[-1] == "/login?m=locked"


def test_ipv6_visitors_are_counted_per_64(tunnel):
    c = proxied()
    seen = [bad_login(c, f"2001:db8:aa:bb::{i:x}") for i in range(1, 7)]
    assert seen[-1] == "/login?m=locked"
    assert bad_login(c, "2001:db8:aa:cc::1") == "/login?m=bad"


@pytest.mark.parametrize("ip,bucket", [("203.0.113.7", "203.0.113.7"), ("::ffff:203.0.113.7", "203.0.113.7"),
                                       ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"), ("nonsense", "unknown"), ("", "unknown")])
def test_ip_bucket(ip, bucket):
    assert auth.ip_bucket(ip) == bucket


# ------------------------------------------------------------------ strangers can't lock the owner out
def test_known_device_still_signs_in_while_strangers_are_locked_out(tunnel):
    owner = proxied()
    r = owner.post("/login", data={"username": USER, "password": PASSWORD}, headers={**ORIGIN, "CF-Connecting-IP": "192.0.2.10"})
    cookie = next(h for h in r.headers.get_list("set-cookie") if h.startswith(auth.DEVICE_COOKIE + "="))
    assert "HttpOnly" in cookie and "Path=/login" in cookie and "SameSite=strict" in cookie
    attacker = proxied()
    for i in range(30):  # many addresses, enough to hit the overall limit for strangers
        bad_login(attacker, f"198.51.{i}.1")
    newcomer = proxied()
    r = newcomer.post("/login", data={"username": USER, "password": PASSWORD}, headers={**ORIGIN, "CF-Connecting-IP": "192.0.2.77"})
    assert r.headers["location"] == "/login?m=locked"  # strangers are held off...
    owner.cookies.delete(auth.SESSION_COOKIE)
    r = owner.post("/login", data={"username": USER, "password": PASSWORD}, headers={**ORIGIN, "CF-Connecting-IP": "192.0.2.10"})
    assert r.headers["location"] == "/"                 # ...but the owner's own browser gets in


def test_known_device_has_its_own_limit_and_fake_ones_are_strangers(tunnel):
    owner = proxied()
    owner.post("/login", data={"username": USER, "password": PASSWORD}, headers=ORIGIN)
    assert [bad_login(owner) for _ in range(6)][-1] == "/login?m=locked"  # a stolen device mark is no free pass
    fake = proxied(cookies={auth.DEVICE_COOKIE: "x" * 43})
    assert auth.device_key(main.db, "x" * 43) is None
    main.limiter = auth.LoginLimiter(per_ip=5, total=20)
    assert [bad_login(fake, "198.51.100.200") for _ in range(6)][-1] == "/login?m=locked"


def test_logout_all_forgets_devices_but_password_change_keeps_this_one(tunnel):
    c = proxied()
    c.post("/login", data={"username": USER, "password": PASSWORD}, headers=ORIGIN)
    token = c.cookies.get(auth.DEVICE_COOKIE)
    assert auth.device_key(main.db, token)
    r = c.post("/api/account/password", json={"current": PASSWORD, "new": "a different long passphrase"}, headers=JSON)
    assert r.status_code == 200 and auth.device_key(main.db, token)  # this browser stays a known device
    main.db.session_delete_all()                                     # the terminal's "logout-all" clears them
    assert auth.device_key(main.db, token) is None


def test_device_list_is_capped(tunnel):
    tokens = [auth.new_device(main.db) for _ in range(auth._MAX_DEVICES + 5)]
    assert sum(auth.device_key(main.db, t) is not None for t in tokens) == auth._MAX_DEVICES
    assert auth.device_key(main.db, tokens[-1])


# ------------------------------------------------------------------ HTTPS at the proxy
def test_cookies_are_secure_and_hsts_is_sent_when_the_visitor_is_on_https(tunnel):
    c = proxied()
    r = c.post("/login", data={"username": USER, "password": PASSWORD},
               headers={"Origin": "https://testserver", "X-Forwarded-Proto": "https", "CF-Connecting-IP": "192.0.2.10", "CF-Ray": "x-IAH"})
    cookies = r.headers.get_list("set-cookie")
    assert len(cookies) == 2 and all("Secure" in h and "HttpOnly" in h for h in cookies)
    assert r.headers["strict-transport-security"].startswith("max-age=")
    plain = new_client().get("/login")
    assert "strict-transport-security" not in plain.headers


def test_https_origin_alone_marks_cookies_secure():
    reset()
    make_account()
    c = TestClient(main.app, follow_redirects=False, base_url="http://garmin.example.net")
    r = c.post("/login", data={"username": USER, "password": PASSWORD}, headers={"Origin": "https://garmin.example.net"})
    assert r.headers["location"] == "/" and all("Secure" in h for h in r.headers.get_list("set-cookie"))


def test_plain_lan_sign_in_still_works_without_secure():
    reset()
    make_account()
    c = new_client()
    login(c)
    assert c.get("/api/status").status_code == 200  # the cookie came back over plain http


# ------------------------------------------------------------------ request size
def test_oversized_requests_are_refused_before_being_read():
    reset()
    make_account()
    c = new_client()
    big = "p" * (main.MAX_BODY_BYTES + 10)
    r = c.post("/login", data={"username": USER, "password": big}, headers=ORIGIN)
    assert r.status_code == 413

    def chunks():  # no Content-Length: sent in pieces
        for _ in range(40):
            yield b"x" * 8192
    r = c.post("/login", content=chunks(), headers={**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code in (400, 413)  # reading stops at the limit; the form parser reports it as a bad request
    r = c.post("/login", content=b"x", headers={**ORIGIN, "Content-Length": "abc"})
    assert r.status_code in (400, 413)
    login(c)  # normal requests are untouched
    assert c.post("/api/settings", json={"values": {"units": "metric"}}, headers=JSON).status_code == 200


# ------------------------------------------------------------------ first-run account creation
def test_account_cannot_be_created_through_the_tunnel(monkeypatch):
    reset()
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, trusted_proxies=NET))
    outside = proxied()
    assert outside.get("/welcome", headers={"CF-Connecting-IP": "198.51.100.1"}).status_code == 403
    r = outside.post("/welcome", data={"username": "mallory", "password": "a long enough passphrase", "password2": "a long enough passphrase"},
                     headers={**ORIGIN, "CF-Connecting-IP": "198.51.100.1"})
    assert r.status_code == 403 and not auth.account_exists(main.settings)
    home = new_client()  # straight to the server's own address
    assert home.get("/welcome").status_code == 200
    r = home.post("/welcome", data={"username": USER, "password": PASSWORD, "password2": PASSWORD}, headers=ORIGIN)
    assert r.headers["location"] == "/setup" and auth.account_exists(main.settings)


@pytest.mark.parametrize("pw", ["aaaaaaaaaaaaaaaa", "password12345", "adam-adam-adam-99x", "123456789012345", "qwertyqwerty12"])
def test_guessable_passwords_are_refused(pw):
    reset()
    c = new_client()
    r = c.post("/welcome", data={"username": USER, "password": pw, "password2": pw}, headers=ORIGIN)
    assert r.headers["location"] == "/welcome?m=weak" and not auth.account_exists(main.settings)
    make_account()
    login(c)
    r = c.post("/api/account/password", json={"current": PASSWORD, "new": pw}, headers=JSON)
    assert r.status_code == 422


def test_good_passphrases_are_accepted():
    for pw in ("correct horse battery staple", "my long dashboard passphrase", "Tr0ub4dor&3xyz"):
        assert auth.weak_password_problem(pw, USER) is None


# ------------------------------------------------------------------ outbound requests
def test_outbound_requests_do_not_follow_redirects():
    hits = []

    class Target(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(b'{"models": [{"name": "secret-internal"}]}')

    target = HTTPServer(("127.0.0.1", 0), Target)

    class Bouncer(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302); self.send_header("Location", f"http://127.0.0.1:{target.server_port}/internal"); self.end_headers()

    bouncer = HTTPServer(("127.0.0.1", 0), Bouncer)
    for srv in (target, bouncer):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ai.AIError, match="redirect"):
            ai.list_models("ollama", None, f"http://127.0.0.1:{bouncer.server_port}")
        import urllib.error
        import urllib.request
        with pytest.raises(urllib.error.HTTPError):
            net.open_url(urllib.request.Request(f"http://127.0.0.1:{bouncer.server_port}/x"), 5, direct=True)
        assert hits == []  # the second server was never contacted
    finally:
        target.shutdown(); bouncer.shutdown()


# ------------------------------------------------------------------ second review
def test_account_creation_from_outside_is_refused_even_without_trusted_proxies():
    reset()  # TRUSTED_PROXIES not set at all, or set to the wrong address
    for hdr in ({"CF-Ray": "8a1b-IAH"}, {"X-Forwarded-For": "198.51.100.1"}, {"CF-Connecting-IP": "198.51.100.1"}, {"Forwarded": "for=1.2.3.4"}):
        c = TestClient(main.app, follow_redirects=False, client=("172.17.0.5", 40000))
        assert c.get("/welcome", headers=hdr).status_code == 403
        r = c.post("/welcome", data={"username": "mallory", "password": "a long enough passphrase", "password2": "a long enough passphrase"},
                   headers={**ORIGIN, **hdr})
        assert r.status_code == 403 and not auth.account_exists(main.settings)
    assert new_client().get("/welcome").status_code == 200  # a browser on the home network


def test_invented_cloudflare_header_is_ignored_behind_other_proxies(tunnel):
    c = proxied()
    seen = []
    for i in range(7):  # no CF-Ray: not Cloudflare, so only the proxy's own X-Forwarded-For entry counts
        h = {**ORIGIN, "CF-Connecting-IP": f"198.51.100.{i}", "X-Forwarded-For": "203.0.113.9"}
        seen.append(c.post("/login", data={"username": USER, "password": "nope nope nope"}, headers=h).headers["location"])
    assert seen[5:] == ["/login?m=locked"] * 2


def test_strangers_cannot_block_password_change_or_delete(tunnel):
    owner = proxied()
    owner.post("/login", data={"username": USER, "password": PASSWORD}, headers=ORIGIN)
    attacker = proxied()
    for i in range(30):
        bad_login(attacker, f"198.51.{i}.1")
    r = owner.post("/api/account/password", json={"current": PASSWORD, "new": "a different long passphrase"}, headers=JSON)
    assert r.status_code == 200
    r = owner.post("/api/data/delete", json={"password": "a different long passphrase", "scope": "garmin_data"}, headers=JSON)
    assert r.status_code == 200
    for _ in range(5):
        owner.post("/api/data/delete", json={"password": "wrong", "scope": "garmin_data"}, headers=JSON)
    assert owner.post("/api/data/delete", json={"password": "wrong", "scope": "garmin_data"}, headers=JSON).status_code == 429


def test_http_origin_is_not_same_site_when_served_over_https():
    reset()
    make_account()
    c = TestClient(main.app, follow_redirects=False, base_url="http://garmin.example.net")
    data = {"username": USER, "password": PASSWORD}
    r = c.post("/login", data=data, headers={"Origin": "http://garmin.example.net", "X-Forwarded-Proto": "https"})
    assert r.status_code == 403
    r = c.post("/login", data=data, headers={"Origin": "https://garmin.example.net", "X-Forwarded-Proto": "https"})
    assert r.headers["location"] == "/"


def test_ollama_address_rules_and_spend_cap():
    from app import prefs
    for bad in ("http://169.254.169.254", "http://192.168.1.5:11434/some/path", "http://[fe80::1]:11434"):
        with pytest.raises(prefs.PrefError):
            prefs.validate({"ollama_url": bad})
    assert prefs.validate({"ollama_url": "http://192.168.4.23:11434/"})["ollama_url"] == "http://192.168.4.23:11434"
    reset()
    errors = []

    def spend():
        try:
            ai._spend(main.db, "ask")
        except ai.AIError:
            errors.append(1)
    threads = [threading.Thread(target=spend) for _ in range(60)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len(errors) == 60 - ai.LIMITS["ask"]
