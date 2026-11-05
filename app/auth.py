"""Dashboard login: scrypt password hash, server-side sessions, rate limiting."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

from .config import Settings, write_secret
from .db import Database

SESSION_COOKIE = "gd_session"
SESSION_DAYS = 30
MIN_PASSWORD_LENGTH = 12

# scrypt cost parameters (~64 MiB, ~0.1-0.3 s per check): slow for attackers, fine for one user.
_N, _R, _P, _MAXMEM = 2**16, 8, 1, 128 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.strip().split("$")
        if algo != "scrypt":
            return False
        check = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
            maxmem=_MAXMEM, dklen=len(bytes.fromhex(digest)),
        )
        return hmac.compare_digest(check, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


def set_password(settings: Settings, password: str) -> None:
    write_secret(settings.password_file, hash_password(password) + "\n")


def password_configured(settings: Settings) -> bool:
    try:
        return settings.password_file.read_text().strip().startswith("scrypt$")
    except FileNotFoundError:
        return False


def check_password(settings: Settings, password: str) -> bool:
    try:
        stored = settings.password_file.read_text()
    except FileNotFoundError:
        return False
    return verify_password(password, stored)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(db: Database) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    db.session_add(_token_hash(token), now, now + SESSION_DAYS * 86400)
    return token


def session_ok(db: Database, token: str | None) -> bool:
    if not token or len(token) > 200:
        return False
    return db.session_valid(_token_hash(token), int(time.time()))


def end_session(db: Database, token: str | None) -> None:
    if token:
        db.session_delete(_token_hash(token))


class LoginLimiter:
    """At most `per_ip` login attempts per IP and `total` overall in a sliding window.

    An attempt is counted *before* the password is checked (atomically, under a
    lock), so a burst of simultaneous requests can't slip past the limit while
    earlier attempts are still being hashed. A successful login removes its own
    attempt and clears that IP's history.
    """

    def __init__(self, per_ip: int = 5, total: int = 20, window_s: int = 900):
        self.per_ip, self.total, self.window = per_ip, total, window_s
        self._ip: dict[str, deque] = {}
        self._all: deque = deque()
        self._lock = threading.Lock()

    def _prune(self, q: deque, now: float) -> None:
        while q and q[0] <= now - self.window:
            q.popleft()

    def try_begin(self, ip: str) -> float | None:
        """Reserve an attempt. Returns a stamp, or None if the limit is reached."""
        now = time.monotonic()
        with self._lock:
            if len(self._ip) > 10_000:  # don't let a flood of IPs grow memory forever
                self._ip = {k: q for k, q in self._ip.items() if q and q[-1] > now - self.window}
            q = self._ip.setdefault(ip, deque())
            self._prune(q, now)
            self._prune(self._all, now)
            if len(q) >= self.per_ip or len(self._all) >= self.total:
                return None
            q.append(now)
            self._all.append(now)
            return now

    def release(self, stamp: float) -> None:
        """Give back an attempt that was reserved but never checked."""
        with self._lock:
            for q in (*self._ip.values(), self._all):
                try:
                    q.remove(stamp)
                except ValueError:
                    pass

    def success(self, ip: str, stamp: float) -> None:
        with self._lock:
            self._ip.pop(ip, None)
            try:
                self._all.remove(stamp)
            except ValueError:
                pass


# At most two password checks run at once (each scrypt check uses ~64 MiB).
_hash_slots = threading.BoundedSemaphore(2)


def check_password_limited(settings: Settings, password: str) -> bool | None:
    """Like check_password, but returns None (busy) instead of queueing more hashing."""
    if not _hash_slots.acquire(blocking=False):
        return None
    try:
        return check_password(settings, password)
    finally:
        _hash_slots.release()
