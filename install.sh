#!/bin/bash
# Garmin Dashboard: install or update on Unraid.
#
#   bash /mnt/user/appdata/garmin-dashboard/install.sh
#
# This builds the image from the source in this folder. (Most people should
# install from Unraid's Docker tab instead; see README.md.)
#
# Safe to re-run any time (after editing config.env, or to update):
# it rebuilds the image and recreates the container. Your data and
# secrets in ./data and your settings in config.env are kept.
set -euo pipefail

NAME="garmin-dashboard"
IMAGE="garmin-dashboard:latest"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$DIR/config.env"
DATA="$DIR/data"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "Run this as root (the Unraid web terminal already is)."
command -v docker >/dev/null 2>&1 || fail "Docker isn't installed. Enable it under Settings → Docker."
docker info >/dev/null 2>&1 || fail "Docker isn't running. Start it under Settings → Docker."
[ -f "$DIR/Dockerfile" ] && [ -f "$DIR/requirements.lock" ] && [ -d "$DIR/app" ] || fail "Run this from inside the garmin-dashboard folder."
if [ ! -f "$CONFIG" ]; then
  [ -f "$DIR/config.example.env" ] || fail "config.example.env is missing from $DIR."
  cp "$DIR/config.example.env" "$CONFIG"
  echo "Created config.env with default settings (edit it any time, then re-run this script)."
fi

# Tolerate a config.env edited on Windows (CRLF line endings).
sed -i 's/\r$//' "$CONFIG"

read_setting() {  # last value of KEY in config.env, without quotes/spaces
  local v
  v="$(grep -E "^[[:space:]]*$1=" "$CONFIG" | tail -n1 | cut -d= -f2- || true)"
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  printf '%s' "$v" | tr -d '[:space:]'
}

PORT="$(read_setting PORT)"; PORT="${PORT:-8099}"
BIND="$(read_setting BIND_ADDRESS)"; BIND="${BIND:-0.0.0.0}"
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || fail "PORT in config.env must be a number from 1 to 65535."
valid_ipv4() {  # four numbers 0-255, no leading zeros
  local octet='(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])'
  [[ "$1" =~ ^$octet\.$octet\.$octet\.$octet$ ]]
}
valid_ipv4 "$BIND" || fail "BIND_ADDRESS in config.env must be an IPv4 address like 0.0.0.0 or 192.168.1.50."

say "1/4  Preparing the data folder ($DATA)"
mkdir -p "$DATA"
chown -R 99:100 "$DATA"      # nobody:users, the unprivileged user the container runs as
chmod 700 "$DATA"
find "$DATA" -type d -exec chmod 700 {} +
find "$DATA" -type f -exec chmod 600 {} +

say "2/4  Building the image (first build takes a few minutes)"
docker build --pull -t "$IMAGE" "$DIR"

say "3/4  Starting the container"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d \
  --name "$NAME" \
  --restart unless-stopped \
  --user 99:100 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --pids-limit 256 \
  --memory 768m \
  -p "$BIND:$PORT:8099" \
  -v "$DATA:/data" \
  --env-file "$CONFIG" \
  --label "net.unraid.docker.webui=http://[IP]:[PORT:$PORT]/" \
  "$IMAGE" >/dev/null

# Remove the previous build of this image only (nothing else on your server).
docker image prune -f --filter "label=org.opencontainers.image.title=garmin-dashboard" >/dev/null 2>&1 || true

say "4/4  Waiting for it to come up"
state=""
for _ in $(seq 1 60); do
  state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$NAME" 2>/dev/null || echo missing)"
  [ "$state" = "healthy" ] && break
  if [ "$state" = "exited" ] || [ "$state" = "dead" ] || [ "$state" = "missing" ]; then break; fi
  sleep 2
done
if [ "$state" != "healthy" ]; then
  echo "The container didn't become healthy (state: $state). Last log lines:"
  docker logs --tail 40 "$NAME" 2>&1 || true
  fail "See the log above."
fi

# Unraid's LAN address normally lives on br0 (or bond0/eth0); fall back to the default route.
IP=""
for ifc in br0 bond0 eth0; do
  IP="$( { ip -4 -o addr show dev "$ifc" 2>/dev/null || true; } | awk '{split($4, a, "/"); print a[1]; exit}' || true)"
  [ -n "$IP" ] && break
done
if [ -z "$IP" ]; then
  IP="$( { ip -4 route get 1.1.1.1 2>/dev/null || true; } | awk '{for (i=1;i<NF;i++) if ($i=="src") {print $(i+1); exit}}' || true)"
fi
[ "$BIND" != "0.0.0.0" ] && IP="$BIND"
URL="http://${IP:-<your-server-ip>}:$PORT"

echo
STATUS="$(docker exec "$NAME" python -m app.setup status 2>&1 || true)"
printf '%s\n' "$STATUS"
if printf '%s' "$STATUS" | grep -q -e "✗ dashboard password" -e "✗ Garmin Personal API key"; then
  say "Almost done. Run the setup to add your password and Garmin key:"
  echo "    docker exec -it $NAME python -m app.setup"
  echo
  echo "Then open $URL"
else
  say "Running. Open $URL"
fi
