"""Runtime settings (from environment variables) and secret-file locations.

Secrets are never taken from environment variables: they live as files under
DATA_DIR/secrets (directory 0700, files 0600) and are written by
`python -m app.setup`. Environment variables are visible to anyone who can run
`docker inspect`; files inside the data volume are not.
"""
from __future__ import annotations

import ipaddress
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Everything this process creates (database, secrets, tokens) is private to its user.
os.umask(0o077)


def _env(name: str, default: str = "") -> str:
    """Read an env var, tolerating Windows line endings and stray quotes."""
    value = os.environ.get(name, default).strip().replace("\r", "")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def _int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(_env(name, str(default)))
    except ValueError:
        value = default
    return max(low, min(high, value))


def _bool(name: str, default: bool = False) -> bool:
    return _env(name, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    sync_interval_hours: int
    backfill_days: int
    refresh_days: int
    connect_backfill_days: int
    units: str
    race_name: str | None
    race_date: str | None
    cookie_secure: bool
    request_delay: float
    allowed_hosts: frozenset
    allowed_origins: frozenset
    trusted_proxies: tuple = ()

    # ---- paths ---------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "garmin.db"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    @property
    def api_key_file(self) -> Path:
        return self.secrets_dir / "personal-api-key"

    @property
    def password_file(self) -> Path:
        return self.secrets_dir / "dashboard-password"

    @property
    def account_file(self) -> Path:
        return self.secrets_dir / "dashboard-account.json"

    @property
    def token_cache(self) -> Path:
        return self.secrets_dir / "personal-api-bearer-token.json"

    @property
    def connect_token_dir(self) -> Path:
        return self.secrets_dir / "garminconnect"

    @property
    def connect_token_file(self) -> Path:
        return self.connect_token_dir / "garmin_tokens.json"

    @property
    def sync_request_file(self) -> Path:
        return self.data_dir / ".sync-requested"

    # ---- secrets (read fresh each time so `app.setup` changes apply live) --
    def api_key(self) -> str | None:
        try:
            key = self.api_key_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return key or None

    def connect_configured(self) -> bool:
        return self.connect_token_file.is_file()


def drop_root(module: str) -> None:
    """When a command is run as root (e.g. `docker exec` on a container started from
    Unraid's Docker tab), re-run it as the dashboard's own user so every file it
    creates stays readable by the dashboard and by nobody else."""
    if os.geteuid() != 0:
        return
    puid, pgid = _env("PUID", "99"), _env("PGID", "100")
    if not (puid.isdigit() and pgid.isdigit()) or puid == "0":
        sys.exit("PUID/PGID must be non-zero numbers (Unraid default: 99 and 100).")
    try:
        os.execvp("setpriv", [
            "setpriv", f"--reuid={puid}", f"--regid={pgid}", "--clear-groups", "--inh-caps=-all", "--",
            sys.executable, "-m", module, *sys.argv[1:],
        ])
    except OSError as e:
        sys.exit(f"Couldn't switch from root to user {puid}: {e}")


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def write_secret(path: Path, content: str) -> None:
    """Atomically write a 0600 file."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load() -> Settings:
    data_dir = Path(_env("DATA_DIR", "/data")).expanduser()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        sys.exit(
            f"ERROR: the data folder {data_dir} is not writable by user id {os.getuid()} ({e}).\n"
            "On Unraid run install.sh again, or: chown -R 99:100 <your data folder>"
        )
    settings = Settings(
        data_dir=data_dir,
        sync_interval_hours=_int("SYNC_INTERVAL_HOURS", 4, 1, 168),
        backfill_days=_int("BACKFILL_DAYS", 365, 7, 3650),
        refresh_days=_int("REFRESH_DAYS", 7, 1, 60),
        connect_backfill_days=_int("CONNECT_BACKFILL_DAYS", 90, 7, 730),
        units="metric" if _env("UNITS", "imperial").lower().startswith("m") else "imperial",
        race_name=_env("RACE_NAME") or None,
        race_date=_valid_date(_env("RACE_DATE")),
        cookie_secure=_bool("COOKIE_SECURE"),
        request_delay=0.5,
        allowed_hosts=frozenset(
            h.strip().lower().rstrip(".") for h in _env("ALLOWED_HOSTS").split(",") if h.strip()
        ),
        # Full origins (scheme://host[:port]) of an HTTPS reverse proxy, if you use one.
        allowed_origins=frozenset(
            o.strip().lower().rstrip("/") for o in _env("ALLOWED_ORIGINS").split(",") if o.strip()
        ),
        trusted_proxies=_networks(_env("TRUSTED_PROXIES")),
    )
    ensure_private_dir(settings.secrets_dir)
    return settings


def _networks(value: str) -> tuple:
    """TRUSTED_PROXIES: addresses or ranges (10.0.0.5, 172.18.0.0/16) of the reverse proxy or tunnel
    in front of the dashboard. Only requests arriving from these may say who the real visitor is."""
    out = []
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            print(f"WARNING: ignoring TRUSTED_PROXIES entry that isn't an address or range: {part[:60]!r}", file=sys.stderr)
    return tuple(out)


def _valid_date(value: str) -> str | None:
    from datetime import date

    try:
        return date.fromisoformat(value).isoformat() if value else None
    except ValueError:
        return None
