"""Garmin Connect sign-in shared by the web setup and the terminal command.

The password is handed to the garminconnect library once and dropped; only the
token Garmin issues is saved (0600, under DATA_DIR/secrets). Sign-in happens in a
staging folder and is swapped in only on success, so a failed attempt never
removes a working sign-in. Two-factor codes are supported: the half-finished
sign-in waits in memory for up to 5 minutes, never on disk.
"""
from __future__ import annotations

import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config
from .db import Database

PENDING_TTL_S = 300


class LoginError(Exception):
    pass


@dataclass
class _Pending:
    client: Any
    state: Any
    staging: Path
    created: float
    tries: int = 0


_pending: dict[str, _Pending] = {}
_pending_lock = threading.Lock()


def _first_line(e: BaseException) -> str:
    lines = str(e).strip().splitlines()
    return lines[0] if lines else type(e).__name__


def _staging(s: config.Settings) -> Path:
    path = s.secrets_dir / f"garminconnect.new-{secrets.token_hex(4)}"
    config.ensure_private_dir(path)
    return path


def _discard(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _prune_pending() -> None:
    now = time.monotonic()
    with _pending_lock:
        for key in [k for k, p in _pending.items() if now - p.created > PENDING_TTL_S]:
            _discard(_pending.pop(key).staging)


def install_tokens(s: config.Settings, staging: Path, db: Database) -> None:
    """Swap a freshly signed-in staging folder into place (holding the sync lock)."""
    from .sync import data_lock

    staged_file = staging / s.connect_token_file.name
    if not staged_file.is_file():
        _discard(staging)
        raise LoginError("Signed in, but Garmin's library didn't save a token. Nothing was changed.")
    with data_lock(s, blocking=True):
        old = s.secrets_dir / "garminconnect.old"
        _discard(old)
        if s.connect_token_dir.exists():
            s.connect_token_dir.rename(old)
        try:
            staging.rename(s.connect_token_dir)
        except OSError as e:
            if old.exists() and not s.connect_token_dir.exists():
                old.rename(s.connect_token_dir)
            _discard(staging)
            raise LoginError(f"Couldn't save the new sign-in ({_first_line(e)}). Your previous sign-in was kept.") from e
        _discard(old)
    db.delete_meta("connect_backfilled")  # pull training history on the next sync


def _translate(e: Exception) -> LoginError:
    from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

    if isinstance(e, GarminConnectTooManyRequestsError):
        return LoginError("Garmin is limiting sign-in attempts. Wait 15–30 minutes and try again.")
    if isinstance(e, GarminConnectAuthenticationError):
        return LoginError("Garmin didn't accept that email and password (or the code).")
    return LoginError(f"Sign-in failed: {_first_line(e)}")


def start(s: config.Settings, db: Database, email: str, password: str) -> str | None:
    """Begin a sign-in. Returns None when finished, or a pending id when Garmin wants a 2FA code."""
    from garminconnect import Garmin

    _prune_pending()
    staging = _staging(s)
    client = Garmin(email=email, password=password, return_on_mfa=True)
    try:
        status, state = client.login(tokenstore=str(staging))
    except Exception as e:
        _discard(staging)
        raise _translate(e) from e
    finally:
        client.password = None  # the password is not needed past this point
    if status == "needs_mfa":
        pending_id = secrets.token_urlsafe(24)
        with _pending_lock:
            if len(_pending) >= 5:  # nobody needs more than a couple of half-finished sign-ins
                oldest = min(_pending, key=lambda k: _pending[k].created)
                _discard(_pending.pop(oldest).staging)
            _pending[pending_id] = _Pending(client, state, staging, time.monotonic())
        return pending_id
    _save(s, db, client, staging)
    return None


def finish(s: config.Settings, db: Database, pending_id: str, code: str) -> None:
    from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError

    _prune_pending()
    with _pending_lock:
        pending = _pending.get(pending_id)
    if pending is None:
        raise LoginError("That sign-in expired. Start again.")
    try:
        pending.client.resume_login(pending.state, code.strip())
    except (GarminConnectAuthenticationError, GarminConnectConnectionError) as e:
        # A mistyped code leaves Garmin's pending sign-in usable: allow a few more tries.
        with _pending_lock:
            pending.tries += 1
            if pending.tries >= 3:
                _pending.pop(pending_id, None)
                _discard(pending.staging)
                raise LoginError("That code didn't work 3 times. Start the sign-in again.") from e
        raise LoginError("That code didn't work. Check it and try again.") from e
    except Exception as e:
        with _pending_lock:
            _pending.pop(pending_id, None)
        _discard(pending.staging)
        raise _translate(e) from e
    with _pending_lock:
        _pending.pop(pending_id, None)
    _save(s, db, pending.client, pending.staging)


def _save(s: config.Settings, db: Database, client: Any, staging: Path) -> None:
    try:
        client.client.dump(str(staging))  # 0600 file inside a 0700 folder (the library enforces it)
    except Exception as e:
        _discard(staging)
        raise LoginError(f"Signed in, but the token couldn't be saved: {_first_line(e)}") from e
    install_tokens(s, staging, db)


def disconnect(s: config.Settings) -> bool:
    from .sync import data_lock

    if not s.connect_token_dir.exists():
        return False
    with data_lock(s, blocking=True):
        shutil.rmtree(s.connect_token_dir, ignore_errors=True)
    return True


def cleanup_stale_staging(s: config.Settings) -> None:
    """Remove half-finished sign-in folders left by a restart."""
    for p in s.secrets_dir.glob("garminconnect.new*"):
        _discard(p)
