#!/bin/sh
# Start the dashboard as an unprivileged user (Unraid's nobody:users = 99:100 by default).
#
# - Started as non-root (install.sh uses --user 99:100): just run.
# - Started as root (Unraid's Docker tab): make sure the data folder belongs to
#   PUID:PGID, then drop to that user for good. Only the top folder can need
#   fixing: Unraid creates it as root on first install, and everything inside is
#   created by the app itself.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
DATA_DIR="${DATA_DIR:-/data}"

case "$PUID$PGID" in
  *[!0-9]*) echo "PUID and PGID must be numbers" >&2; exit 1 ;;
esac
if [ "$PUID" = "0" ]; then
  echo "Refusing to run as root: set PUID to a non-zero user id (Unraid default: 99)" >&2
  exit 1
fi

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA_DIR"
  if [ "$(stat -c %u:%g "$DATA_DIR")" != "$PUID:$PGID" ]; then
    chmod 700 "$DATA_DIR"
    chown "$PUID:$PGID" "$DATA_DIR" || {
      echo "Couldn't hand $DATA_DIR to $PUID:$PGID. Make sure the container has --cap-add CHOWN." >&2
      exit 1
    }
  fi
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --inh-caps=-all -- "$@"
fi

exec "$@"
