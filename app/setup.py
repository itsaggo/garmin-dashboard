"""Terminal fallback. Normal setup happens in the browser; use these only if you're locked out
or prefer the terminal:

    docker exec -it garmin-dashboard python -m app.setup status
    docker exec -it garmin-dashboard python -m app.setup reset-password   # forgot your password
    docker exec -it garmin-dashboard python -m app.setup logout-all       # sign out every browser
    docker exec -it garmin-dashboard python -m app.setup api-key          # save / replace the Personal API key
    docker exec -it garmin-dashboard python -m app.setup connect          # sign in to Garmin Connect
    docker exec -it garmin-dashboard python -m app.setup disconnect       # remove the Garmin Connect sign-in

Secrets are typed at hidden prompts and saved as 0600 files in the data folder.
Your Garmin password is used once to sign in and is never saved.
"""
from __future__ import annotations

import getpass
import sys

from . import auth, config, garmin_login
from .db import Database


def _ask_yes(question: str, default: bool = True) -> bool:
    answer = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not answer else answer.startswith("y")


def _request_sync(s: config.Settings) -> None:
    try:
        s.sync_request_file.write_text("1")
    except OSError:
        pass


def _new_password() -> str:
    while True:
        pw = getpass.getpass("New password: ")
        problem = auth.password_problem(pw)
        if problem:
            print(f"  {problem}")
            continue
        if pw != getpass.getpass("Repeat password: "):
            print("  Passwords didn't match, try again.")
            continue
        return pw


def cmd_reset_password(s: config.Settings) -> None:
    name = auth.current_username(s)
    if name is None:
        print("No account exists yet. Open the dashboard in a browser to create one.")
        return
    print(f"\nResetting the password for '{name}'.")
    new_name = input(f"Username [{name}]: ").strip() or name
    if auth.username_problem(new_name):
        print(f"  {auth.username_problem(new_name)}")
        return
    auth.set_password(s, _new_password(), username=new_name)
    removed = Database(s.db_path).session_delete_all()
    print("  ✓ Password saved." + (f" Signed out {removed} session(s)." if removed else ""))


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
    print("\nGarmin Connect sign-in (adds VO2 max, training status, training readiness, recovery time).")
    print("Uses the community 'garminconnect' library; your password is used once and never saved.")
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    db = Database(s.db_path)
    print("  Signing in…")
    try:
        pending = garmin_login.start(s, db, email, password)
        del password
        while pending:
            code = input("Two-factor code from Garmin (email/app): ").strip()
            try:
                garmin_login.finish(s, db, pending, code)
                pending = None
            except garmin_login.LoginError as e:
                print(f"  ✗ {e}")
                if "Start" in str(e):  # expired or out of tries
                    return
    except garmin_login.LoginError as e:
        print(f"  ✗ {e}")
        return
    print("  ✓ Connected. Training data will appear after the next sync (within a minute).")
    _request_sync(s)


def cmd_disconnect(s: config.Settings) -> None:
    if garmin_login.disconnect(s):
        print("  ✓ Garmin Connect sign-in removed. (Stored training history is kept.)")
    else:
        print("  Garmin Connect isn't connected.")


def cmd_logout_all(s: config.Settings) -> None:
    n = Database(s.db_path).session_delete_all()
    print(f"  ✓ Signed out {n} session(s).")


def cmd_status(s: config.Settings) -> None:
    mark = lambda ok: "✓" if ok else "✗"  # noqa: E731
    name = auth.current_username(s)
    print(f"  {mark(bool(name))} dashboard account" + (f" ({name})" if name else " (create it in the browser)"))
    print(f"  {mark(bool(s.api_key()))} Garmin Personal API key")
    print(f"  {mark(s.connect_configured())} Garmin Connect sign-in (optional: VO2 max, training status, recovery)")


COMMANDS = {
    "status": cmd_status, "reset-password": cmd_reset_password, "logout-all": cmd_logout_all,
    "api-key": cmd_api_key, "connect": cmd_connect, "disconnect": cmd_disconnect,
}
INTERACTIVE = {"reset-password", "api-key", "connect"}


def main() -> None:
    config.drop_root("app.setup")
    s = config.load()
    args = sys.argv[1:]
    if not args:
        print("Setup now happens in your browser: open the dashboard's WebUI.\n")
        cmd_status(s)
        print("\nTerminal commands (if you're locked out):")
        print("  " + "\n  ".join(f"python -m app.setup {c}" for c in COMMANDS))
        return
    if args[0] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    if args[0] in INTERACTIVE and not sys.stdin.isatty():
        sys.exit("This needs an interactive terminal: use  docker exec -it garmin-dashboard python -m app.setup " + args[0])
    try:
        COMMANDS[args[0]](s)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Nothing more was changed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
