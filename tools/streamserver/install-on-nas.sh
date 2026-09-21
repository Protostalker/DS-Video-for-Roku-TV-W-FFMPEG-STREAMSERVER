#!/bin/sh
# DS Video stream server: one-command install for Synology Container Manager.
#
#   sudo sh install-on-nas.sh              install or update (safe to run again)
#   sudo sh install-on-nas.sh --status     is it running, is it healthy?
#   sudo sh install-on-nas.sh --logs       show the last log lines
#   sudo sh install-on-nas.sh --uninstall  remove the container and image
#
# Options for the install:
#   --port N        port the Roku talks to (default 8899)
#   --dsm-url URL   where DSM listens on the NAS itself (default: auto-detected)
#   --no-vaapi      never use the Intel GPU for encoding
#   --debug         verbose server log (shows the exact ffmpeg command)
#
# Run it from the folder that contains Dockerfile and streamserver.py, over SSH
# (Control Panel > Terminal & SNMP > Enable SSH service). No GUI steps needed.

NAME="ds-video-stream"
IMAGE="ds-video-stream:latest"
PORT="8899"
DSM_URL=""
USE_VAAPI="auto"
LOG_LEVEL="INFO"
ACTION="install"

say()  { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --port)      [ $# -ge 2 ] || die "--port needs a number"; PORT="$2"; shift ;;
    --port=*)    PORT="${1#*=}" ;;
    --dsm-url)   [ $# -ge 2 ] || die "--dsm-url needs an address"; DSM_URL="$2"; shift ;;
    --dsm-url=*) DSM_URL="${1#*=}" ;;
    --no-vaapi)  USE_VAAPI="no" ;;
    --debug)     LOG_LEVEL="DEBUG" ;;
    --status)    ACTION="status" ;;
    --logs)      ACTION="logs" ;;
    --uninstall) ACTION="uninstall" ;;
    -h|--help)   sed -n '2,18p' "$0"; exit 0 ;;
    *)           die "Unknown option: $1 (try --help)" ;;
  esac
  shift
done

case "$PORT" in ''|*[!0-9]*) die "Port must be a number, got '$PORT'" ;; esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "Port must be between 1 and 65535"

# ---- need root ---------------------------------------------------------------
if [ "$(id -u)" != "0" ]; then
  if command -v sudo >/dev/null 2>&1; then
    say "Needs administrator rights, asking sudo (type your DSM password)..."
    exec sudo sh "$0" "$@"
  fi
  die "Please run as root:  sudo sh $0"
fi

# ---- find docker -------------------------------------------------------------
DOCKER=""
for c in docker /usr/local/bin/docker /var/packages/ContainerManager/target/usr/bin/docker /var/packages/Docker/target/usr/bin/docker; do
  if command -v "$c" >/dev/null 2>&1; then DOCKER="$c"; break; fi
done
[ -n "$DOCKER" ] || die "Docker was not found.
Open DSM > Package Center, install 'Container Manager' (called 'Docker' on DSM 7.1 and older), then run this again."
"$DOCKER" info >/dev/null 2>&1 || die "Docker is installed but not running.
Open DSM > Package Center > Container Manager and click Open/Run, then try again."

fetch() {
  if command -v curl >/dev/null 2>&1; then curl -sk -m 5 "$1" 2>/dev/null
  elif command -v wget >/dev/null 2>&1; then wget -qO- --no-check-certificate -T 5 "$1" 2>/dev/null
  fi
}
health_json() { fetch "http://127.0.0.1:$PORT/api/health"; }
nas_ip() {
  ip=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1)
  [ -n "$ip" ] || ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  printf '%s' "${ip:-YOUR-NAS-IP}"
}

# ---- small actions -------------------------------------------------------------
if [ "$ACTION" = "status" ]; then
  "$DOCKER" ps -a --filter "name=$NAME" --format 'Container: {{.Names}}  Status: {{.Status}}'
  body=$(health_json)
  if [ -n "$body" ]; then say "Health check on port $PORT: OK"; say "$body" | head -c 600; say ""
  else say "Health check on port $PORT: no answer (is it running? try --logs)"; fi
  exit 0
fi
if [ "$ACTION" = "logs" ]; then
  "$DOCKER" logs --tail 100 "$NAME" 2>&1
  exit 0
fi
if [ "$ACTION" = "uninstall" ]; then
  "$DOCKER" rm -f "$NAME" >/dev/null 2>&1 && say "Removed container $NAME" || say "No container named $NAME"
  "$DOCKER" rmi "$IMAGE" >/dev/null 2>&1 && say "Removed image $IMAGE" || true
  say "Done. You can delete this folder too if you like."
  exit 0
fi

# ---- install -----------------------------------------------------------------
SRC=$(cd "$(dirname "$0")" && pwd)
for f in Dockerfile streamserver.py planner.py; do
  [ -f "$SRC/$f" ] || die "$f is missing from $SRC.
Copy the whole 'streamserver' folder to the NAS (File Station works), then run this script from inside it."
done

say "== DS Video stream server installer =="
say "Folder: $SRC"

# Where does DSM listen on the NAS itself? Try the usual suspects.
probe_dsm() { fetch "$1/webapi/query.cgi?api=SYNO.API.Info&version=1&method=query&query=SYNO.API.Auth" | grep -q '"success" *: *true'; }
if [ -z "$DSM_URL" ]; then
  http_port=$(sed -n 's/^\(external_port_dsm_http\|dsm_http_port\)="\{0,1\}\([0-9]*\)"\{0,1\}$/\2/p' /etc/synoinfo.conf 2>/dev/null | head -n 1)
  https_port=$(sed -n 's/^\(external_port_dsm_https\|dsm_https_port\)="\{0,1\}\([0-9]*\)"\{0,1\}$/\2/p' /etc/synoinfo.conf 2>/dev/null | head -n 1)
  for cand in "http://127.0.0.1:5000" "http://127.0.0.1:${http_port:-5000}" "https://127.0.0.1:5001" "https://127.0.0.1:${https_port:-5001}"; do
    if probe_dsm "$cand"; then DSM_URL="$cand"; break; fi
  done
  if [ -z "$DSM_URL" ]; then
    DSM_URL="http://127.0.0.1:5000"
    warn "Could not find DSM on this NAS. Using $DSM_URL. If your DSM uses another port, run again with:
         --dsm-url http://127.0.0.1:YOURPORT"
  else
    say "DSM found at $DSM_URL"
  fi
elif ! probe_dsm "$DSM_URL"; then
  warn "DSM did not answer at $DSM_URL. Continuing anyway."
fi

# Intel GPU?
GPU_ARGS=""
if [ "$USE_VAAPI" != "no" ] && [ -e /dev/dri/renderD128 ]; then
  GPU_ARGS="--device /dev/dri:/dev/dri -e HWACCEL=vaapi"
  say "Intel GPU found: hardware video encoding enabled (falls back to software if it fails)."
fi

mkdir -p "$SRC/cache" || die "Cannot create $SRC/cache"

say ""
say "Building the image (first time takes a few minutes, needs internet)..."
"$DOCKER" build -t "$IMAGE" "$SRC" || die "The image build failed.
The NAS needs internet access to download ffmpeg (from dl-cdn.alpinelinux.org). Check the NAS network/DNS and try again."

"$DOCKER" rm -f "$NAME" >/dev/null 2>&1

if command -v netstat >/dev/null 2>&1 && netstat -tln 2>/dev/null | grep -q "[:.]$PORT "; then
  die "Port $PORT is already used by something else on this NAS.
Pick another one:  sudo sh $0 --port 8898
(then set the same address in the Roku app: Settings > Stream Server)"
fi

say "Starting the container..."
# shellcheck disable=SC2086
"$DOCKER" run -d --name "$NAME" --restart unless-stopped --network host \
  -e "DSM_URL=$DSM_URL" -e "PORT=$PORT" -e "LOG_LEVEL=$LOG_LEVEL" $GPU_ARGS \
  -v "$SRC/cache:/cache" "$IMAGE" >/dev/null || die "Could not start the container."

i=0
body=""
while [ $i -lt 40 ]; do
  body=$(health_json)
  [ -n "$body" ] && break
  i=$((i + 1))
  sleep 1
done
if [ -z "$body" ]; then
  say ""
  "$DOCKER" logs --tail 30 "$NAME" 2>&1
  die "The server did not come up. The log above should say why."
fi

say ""
say "Server is running."
case "$body" in
  *'"reachable": true'*) say "Server can talk to DSM: OK" ;;
  *) warn "The server cannot reach DSM at $DSM_URL. Playback of files will fail until this is fixed:
         sudo sh $0 --dsm-url http://127.0.0.1:YOURDSMPORT" ;;
esac
say ""
say "=========================================================="
say " Done. Stream server address:  http://$(nas_ip):$PORT"
if [ "$PORT" = "8899" ]; then
  say " The Roku app finds it by itself, nothing to set up."
else
  say " In the Roku app open Settings > Stream Server and enter:"
  say "   http://$(nas_ip):$PORT"
fi
say " If you use the DSM firewall, allow TCP port $PORT from your home network."
say " Check on it any time:   sudo sh $0 --status"
say "=========================================================="
