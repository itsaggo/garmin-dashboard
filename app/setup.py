"""Interactive setup. Run inside the container:

    docker exec -it garmin-dashboard python -m app.setup            # guided: does whatever isn't done yet
    docker exec -it garmin-dashboard python -m app.setup password   # change the dashboard password
    docker exec -it garmin-dashboard python -m app.setup api-key    # save / replace the Personal API key
    docker exec -it garmin-dashboard python -m app.setup connect    # sign in to Garmin Connect (VO2 max etc.)
    docker exec -it garmin-dashboard python -m app.setup disconnect # remove the Garmin Connect sign-in
    docker exec -it garmin-dashboard python -m app.setup logout-all # sign out every browser
    docker exec -it garmin-dashboard python -m app.setup status

Secrets are typed at hidden prompts and saved as 0600 files in the data folder.
Your Garmin password is used once to sign in and is never saved.
"""
from __future__ import annotations

import getpass
import shutil
import sys
from contextlib import contextmanager

from . import auth, config
from .db import Database


def _ask_yes(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{question} [{hint}] ").strip().lower()
    return default if not answer else answer.startswith("y")


def _first_line(e: BaseException) -> str:
    lines = str(e).strip().splitlines()
    return lines[0] if lines else type(e).__name__


@contextmanager
def _sync_paused(s: config.Settings):
    """Hold the sync lock while changing Garmin Connect tokens, so a running sync
    can't read or re-save them halfway through."""
    from .sync import data_lock

    with data_lock(s) as held:
        if held:
            yield
            return
    print("  Waiting for the current sync to finish…")
    with data_lock(s, blocking=True):
        yield


def _request_sync(s: config.Settings) -> None:
    try:
        s.sync_request_file.write_text("1")
    except OSError:
        pass


def cmd_password(s: config.Settings) -> None:
    print(f"\nChoose a dashboard password (at least {auth.MIN_PASSWORD_LENGTH} characters; a passphrase is ideal).")
    while True:
        pw = getpass.getpass("New password: ")
        if len(pw) < auth.MIN_PASSWORD_LENGTH:
            print(f"  Too short, use at least {auth.MIN_PASSWORD_LENGTH} characters.")
            continue
        if pw != getpass.getpass("Repeat password: "):
            print("  Passwords didn't match, try again.")
            continue
        break
    auth.set_password(s, pw)
    removed = Database(s.db_path).session_delete_all()
    print("  ✓ Password saved." + (f" Signed out {removed} existing session(s)." if removed else ""))


def cmd_api_key(s: config.Settings) -> None:
    from garmin_personal_api import HttpError, PersonalApiAuthenticator

    print("\nCreate a key at https://connect.garmin.com/app/settings/personalApiToken")
    print("(Garmin shows it only once). Paste it below; it won't be displayed.")
    while True:
        key = getpass.getpass("Personal API key: ").strip()
        if not key:
            print("  Nothing entered.")
            continue
        print("  Checking the key with Garmin…")
        try:
            with PersonalApiAuthenticator(bearer_token_cache_file=s.token_cache) as a:
                a.obtain_token(key, force_refresh=True)
        except HttpError as e:
            print(f"  ✗ Garmin rejected that key (HTTP {e.status_code}). Check it and try again.")
            if not _ask_yes("  Try another key?"):
                return
            continue
        except Exception as e:
            print(f"  ✗ Couldn't reach Garmin to check the key: {type(e).__name__}. Is the server online?")
            if not _ask_yes("  Save it anyway?", default=False):
                return
        break
    config.write_secret(s.api_key_file, key + "\n")
    print("  ✓ Key saved. The dashboard will start syncing within a minute.")
    _request_sync(s)


def cmd_connect(s: config.Settings) -> None:
    from garminconnect import Garmin, GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

    print("\nGarmin Connect sign-in (adds VO2 max, training status, training readiness, recovery time).")
    print("This uses the community 'garminconnect' library, not Garmin's official API:")
    print("  • it only reads data, and your password is used once here and never saved")
    print("  • a sign-in token is saved instead (good for months); Garmin may change things and break it")
    if not _ask_yes("Continue?"):
        return
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")

    def prompt_mfa() -> str:
        return input("Two-factor code from Garmin (email/app): ").strip()

    # Sign in into a staging folder so a failed attempt never removes a working sign-in.
    staging = s.secrets_dir / "garminconnect.new"
    if staging.exists():
        shutil.rmtree(staging)
    config.ensure_private_dir(staging)
    client = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
    del password
    print("  Signing in…")
    try:
        client.login(tokenstore=str(staging))
    except GarminConnectTooManyRequestsError:
        print("  ✗ Garmin is limiting sign-in attempts. Wait 15–30 minutes and try again.")
        shutil.rmtree(staging, ignore_errors=True)
        return
    except GarminConnectAuthenticationError as e:
        print(f"  ✗ Sign-in failed: {_first_line(e)}")
        shutil.rmtree(staging, ignore_errors=True)
        return
    except Exception as e:
        print(f"  ✗ Sign-in failed: {type(e).__name__}: {_first_line(e)}")
        shutil.rmtree(staging, ignore_errors=True)
        return
    finally:
        client.password = None
    staged_file = staging / s.connect_token_file.name
    if not staged_file.is_file():
        shutil.rmtree(staging, ignore_errors=True)
        print("  ✗ Signed in, but no token file was written. Nothing was changed.")
        return
    with _sync_paused(s):
        # Swap in the new sign-in; the old one is only deleted once the new one is in place.
        old = s.secrets_dir / "garminconnect.old"
        if old.exists():
            shutil.rmtree(old)
        if s.connect_token_dir.exists():
            s.connect_token_dir.rename(old)
        try:
            staging.rename(s.connect_token_dir)
        except OSError as e:
            if old.exists() and not s.connect_token_dir.exists():
                old.rename(s.connect_token_dir)  # put the previous sign-in back
            print(f"  ✗ Couldn't save the new sign-in ({_first_line(e)}). Your previous sign-in was kept.")
            shutil.rmtree(staging, ignore_errors=True)
            return
        shutil.rmtree(old, ignore_errors=True)
    db = Database(s.db_path)
    db.delete_meta("connect_backfilled")  # pull history on the next sync
    print("  ✓ Connected. VO2 max and training data will appear after the next sync (within a minute).")
    _request_sync(s)


def cmd_disconnect(s: config.Settings) -> None:
    if s.connect_token_dir.exists():
        with _sync_paused(s):
            shutil.rmtree(s.connect_token_dir)
        print("  ✓ Garmin Connect sign-in removed. (Stored training history is kept.)")
    else:
        print("  Garmin Connect isn't connected.")


def cmd_logout_all(s: config.Settings) -> None:
    n = Database(s.db_path).session_delete_all()
    print(f"  ✓ Signed out {n} session(s).")


def cmd_status(s: config.Settings) -> None:
    mark = lambda ok: "✓" if ok else "✗"  # noqa: E731
    print(f"  {mark(auth.password_configured(s))} dashboard password")
    print(f"  {mark(bool(s.api_key()))} Garmin Personal API key")
    print(f"  {mark(s.connect_configured())} Garmin Connect sign-in (optional: VO2 max, training status, recovery)")


def guided(s: config.Settings) -> None:
    print("Garmin Dashboard setup")
    cmd_status(s)
    if not auth.password_configured(s):
        cmd_password(s)
    if not s.api_key():
        cmd_api_key(s)
    if not s.connect_configured() and _ask_yes(
        "\nAlso connect Garmin Connect for VO2 max, training status and recovery time?", default=True
    ):
        cmd_connect(s)
    print("\nDone. Current setup:")
    cmd_status(s)
    print("Open the dashboard in your browser and sign in with your dashboard password.")


COMMANDS = {
    "password": cmd_password, "api-key": cmd_api_key, "connect": cmd_connect,
    "disconnect": cmd_disconnect, "logout-all": cmd_logout_all, "status": cmd_status,
}


def main() -> None:
    config.drop_root("app.setup")
    s = config.load()
    args = sys.argv[1:]
    if args and args[0] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    if not sys.stdin.isatty() and (not args or args[0] in ("password", "api-key", "connect")):
        sys.exit("This needs an interactive terminal: use  docker exec -it garmin-dashboard python -m app.setup")
    try:
        COMMANDS[args[0]](s) if args else guided(s)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Nothing more was changed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
