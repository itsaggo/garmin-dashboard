"""Dashboard login: scrypt password hash, server-side sessions, rate limiting."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import deque

from . import config
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


USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
MAX_PASSWORD_LENGTH = 256
# Spent on every failed lookup so a wrong username takes as long as a wrong password.
_DUMMY_HASH = "scrypt$65536$8$1$" + "00" * 16 + "$" + "00" * 32


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters (a short sentence works well)."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Use at most {MAX_PASSWORD_LENGTH} characters."
    return None


def username_problem(username: str) -> str | None:
    if not USERNAME_RE.match(username or ""):
        return "Use 3–32 letters, numbers, dots, dashes or underscores."
    return None


def load_account(settings: Settings) -> dict | None:
    """The single dashboard account: {"username", "hash"}. Migrates v1.0 password-only installs."""
    try:
        data = json.loads(settings.account_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and USERNAME_RE.match(str(data.get("username", ""))) \
                and str(data.get("hash", "")).startswith("scrypt$"):
            return data
        return None
    except FileNotFoundError:
        pass
    except (ValueError, OSError):
        return None
    try:  # v1.0 stored only a password hash; keep it working with username "admin"
        legacy = settings.password_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if legacy.startswith("scrypt$"):
        data = {"username": "admin", "hash": legacy}
        write_secret(settings.account_file, json.dumps(data) + "\n")
        settings.password_file.unlink(missing_ok=True)
        return data
    return None


def account_exists(settings: Settings) -> bool:
    return load_account(settings) is not None


def create_account(settings: Settings, username: str, password: str) -> bool:
    """Create the one account. Returns False if an account already exists (first visitor wins,
    atomically: two simultaneous sign-ups can't both succeed)."""
    if account_exists(settings):
        return False
    body = json.dumps({"username": username, "hash": hash_password(password)}) + "\n"
    config.ensure_private_dir(settings.account_file.parent)
    try:
        fd = os.open(settings.account_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    return True


def set_password(settings: Settings, password: str, username: str | None = None) -> None:
    """Replace the password (and optionally username), keeping the account."""
    acct = load_account(settings) or {}
    name = username or acct.get("username") or "admin"
    write_secret(settings.account_file, json.dumps({"username": name, "hash": hash_password(password)}) + "\n")


def password_configured(settings: Settings) -> bool:  # kept for older call sites
    return account_exists(settings)


def check_login(settings: Settings, username: str, password: str) -> bool:
    acct = load_account(settings)
    if acct is None:
        verify_password(password, _DUMMY_HASH)
        return False
    name_ok = hmac.compare_digest(username.strip().lower().encode(), acct["username"].lower().encode())
    pw_ok = verify_password(password, acct["hash"])  # always run, so timing doesn't reveal the username
    return name_ok and pw_ok


def check_password(settings: Settings, password: str) -> bool:
    """Password only (used to confirm sensitive actions by someone already signed in)."""
    acct = load_account(settings)
    if acct is None:
        return False
    return verify_password(password, acct["hash"])


def current_username(settings: Settings) -> str | None:
    acct = load_account(settings)
    return acct["username"] if acct else None


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


def end_other_sessions(db: Database, keep_token: str | None) -> int:
    return db.session_delete_all_except(_token_hash(keep_token) if keep_token else "")


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


def limited(fn, *args) -> bool | None:
    """Run a password check, or return None (busy) instead of queueing more hashing."""
    if not _hash_slots.acquire(blocking=False):
        return None
    try:
        return fn(*args)
    finally:
        _hash_slots.release()
