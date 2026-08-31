#!/usr/bin/env bash
#
# scripts/run-docker.sh
#
# Build the TokenScope image and (re)start the container with the log
# directories mounted read-only.
#
# Usage:
#   bash scripts/run-docker.sh                 # build + restart on :9898
#   bash scripts/run-docker.sh --port 9000
#   bash scripts/run-docker.sh --pull          # git pull the repo first
#   bash scripts/run-docker.sh --no-build      # reuse the existing image
#   bash scripts/run-docker.sh --logs          # follow the container's logs
#   bash scripts/run-docker.sh --stop          # stop + remove, then exit
#
# PORT / IMAGE / CONTAINER can also be set in the environment.
#
# The container runs on its own bridge network, so it cannot see other
# containers -- but it does have normal outbound access, which it needs: the
# sidebar's plan-limit panel calls api.anthropic.com (quota.py). An earlier
# version of this script disabled IP masquerade on that network, which broke
# the panel on Linux and did nothing at all on Docker Desktop (the VM NATs
# regardless), so the knob is gone rather than misleading.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-tokenscope}"
CONTAINER="${CONTAINER:-tokenscope}"
PORT="${PORT:-9898}"
NETWORK="tokenscope-net"
VOLUME="${CONTAINER}-data"

# Names this project used before it was renamed to TokenScope. An upgrade has to
# clean them up: the old container can still be holding port 9898, and the old
# volume holds the user's database and settings file.
LEGACY_CONTAINER="claude-usage"
LEGACY_NETWORK="claude-usage-net"
LEGACY_VOLUME="claude-usage-data"

DO_PULL=0
DO_BUILD=1
ACTION="run"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE_EOF'
Build the TokenScope image and (re)start the container.

Usage:
  bash scripts/run-docker.sh [options]

Options:
  --port N      host port to publish on (default: 9898, or $PORT)
  --pull        git pull the repo before building
  --no-build    reuse the existing image instead of rebuilding
  --logs        follow the running container's logs, then exit
  --stop        stop and remove the container, then exit
  -h, --help    this text
USAGE_EOF
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) [[ $# -ge 2 ]] || die "--port needs a value"; PORT="$2"; shift 2 ;;
    --pull) DO_PULL=1; shift ;;
    --no-build) DO_BUILD=0; shift ;;
    --logs) ACTION="logs"; shift ;;
    --stop) ACTION="stop"; shift ;;
    -h|--help) usage 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) ||
  die "invalid --port '$PORT'"

command -v docker >/dev/null 2>&1 ||
  die "docker not found on PATH. Install Docker Desktop or the docker CLI."
docker info >/dev/null 2>&1 ||
  die "cannot talk to the Docker daemon. Is Docker running?"

# Stop *and remove*: `--rm` reaps asynchronously, so `docker stop` alone can
# leave the name taken for a moment and make the next `docker run` fail. This
# also clears a container that exited but was never removed.
remove_container() {
  if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    printf '%s  Removing existing %s...\n' "⏹" "$CONTAINER"
    docker rm -f "$CONTAINER" >/dev/null
  fi
}

# The old script created its network with IP masquerade disabled. Reusing that
# network by name would keep the sidebar's plan-limit panel broken forever on
# Linux, so an existing network is checked and rebuilt when it is the bad one.
network_blocks_egress() {
  local value
  value="$(docker network inspect -f \
    '{{index .Options "com.docker.network.bridge.enable_ip_masquerade"}}' \
    "$1" 2>/dev/null || true)"
  [[ "$value" == "false" ]]
}

ensure_network() {
  if docker network inspect "$NETWORK" >/dev/null 2>&1; then
    if network_blocks_egress "$NETWORK"; then
      printf '%s  Rebuilding %s (it was created without outbound access).\n' "🔧" "$NETWORK"
      docker network rm "$NETWORK" >/dev/null
    else
      return 0
    fi
  fi
  docker network create "$NETWORK" >/dev/null
}

# Only for the default names -- someone running with CONTAINER=... set has their
# own scheme and their own migration to worry about.
uses_default_names() { [[ "$CONTAINER" == "tokenscope" ]]; }

# Free the port and drop the old network. Safe to call before the image exists.
remove_legacy_container() {
  uses_default_names || return 0
  if docker container inspect "$LEGACY_CONTAINER" >/dev/null 2>&1; then
    printf '%s  Removing the pre-rename container %s (it may hold the port).\n' \
      "🧹" "$LEGACY_CONTAINER"
    docker rm -f "$LEGACY_CONTAINER" >/dev/null
  fi
  if docker network inspect "$LEGACY_NETWORK" >/dev/null 2>&1; then
    docker network rm "$LEGACY_NETWORK" >/dev/null 2>&1 || true
  fi
}

# Copy the pre-rename data volume across. MUST run after the image is built:
# it does the copy inside that image, and on a genuine first upgrade the
# `tokenscope` image does not exist yet -- attempting it earlier makes docker
# try to pull it from Hub, fail, and start the dashboard on an empty volume with
# the migration then skipped forever (because the new volume now exists).
migrate_legacy_volume() {
  uses_default_names || return 0
  docker volume inspect "$LEGACY_VOLUME" >/dev/null 2>&1 || return 0
  ! docker volume inspect "$VOLUME" >/dev/null 2>&1 || return 0

  # Nothing to carry over is not a failure -- say nothing and move on.
  if docker run --rm -v "$LEGACY_VOLUME:/from:ro" --entrypoint sh "$IMAGE" \
       -c '[ -n "$(ls -A /from)" ]' >/dev/null 2>&1; then
    :
  else
    return 0
  fi

  printf '%s  Copying %s -> %s (database + settings)...\n' "📦" "$LEGACY_VOLUME" "$VOLUME"

  # `cp -a /from/.` copies dotfiles too. No `|| true`: a failed copy must be
  # reported as a failure. `ls -A` proves something actually landed, so an
  # empty destination is never announced as a success.
  local copy_log
  if copy_log="$(docker run --rm \
      -v "$LEGACY_VOLUME:/from:ro" \
      -v "$VOLUME:/to" \
      --entrypoint sh "$IMAGE" \
      -c 'cp -a /from/. /to/ && [ -n "$(ls -A /to)" ]' 2>&1)"; then
    printf '   Copied. The old volume is left in place; remove it with:\n'
    printf '     docker volume rm %s\n' "$LEGACY_VOLUME"
    return 0
  fi

  # Stop rather than start on an empty volume: continuing would create $VOLUME
  # via `docker run`, and the next invocation would then see the destination
  # already present and skip the migration for good -- quietly stranding the
  # user's database and settings.
  docker volume rm "$VOLUME" >/dev/null 2>&1 || true
  if [[ -n "$copy_log" ]]; then
    printf 'docker said: %s\n' "$copy_log" >&2
  fi
  die "could not copy $LEGACY_VOLUME into $VOLUME. Nothing was started and your old data is untouched. Copy it by hand with:
    docker run --rm -v $LEGACY_VOLUME:/from:ro -v $VOLUME:/to --entrypoint sh $IMAGE -c 'cp -a /from/. /to/'
  then re-run this script. To start fresh instead: docker volume create $VOLUME"
}

# True while the container is up.
container_running() {
  [[ "$(docker container inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)" == "true" ]]
}

# True when the dashboard answers on the published port. Falls back to "no
# probe available", in which case the caller only checks liveness.
http_probe() {
  local url="http://localhost:$PORT/"
  if command -v curl >/dev/null 2>&1; then
    # -f fail on HTTP errors, -s silent: a not-ready-yet probe must not print.
    curl -fs -o /dev/null --max-time 3 "$url"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$url" <<'PROBE_EOF' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen(sys.argv[1], timeout=3).read(1)
PROBE_EOF
    return
  fi
  return 2
}

case "$ACTION" in
  stop)
    remove_container
    # An old claude-usage container left running would keep the port.
    remove_legacy_container
    printf '%s  Stopped.\n' "✅"
    exit 0
    ;;
  logs)
    docker container inspect "$CONTAINER" >/dev/null 2>&1 ||
      die "container '$CONTAINER' does not exist. Start it first."
    exec docker logs -f "$CONTAINER"
    ;;
esac

printf '%s  Checking for a running container...\n' "▶"
remove_container
remove_legacy_container

printf '%s  Ensuring network %s...\n' "🔗" "$NETWORK"
ensure_network

if (( DO_PULL )); then
  printf '%s  Pulling latest...\n' "⬇"
  # Never abort the whole run because the tree is dirty or the network is down:
  # the point of this script is to get the dashboard up.
  git -C "$REPO_DIR" pull --ff-only ||
    printf '%s  git pull failed - building the working tree as-is.\n' "⚠"
fi

if (( DO_BUILD )); then
  printf '%s  Building image %s...\n' "🔨" "$IMAGE"
  docker build -t "$IMAGE" "$REPO_DIR"
else
  docker image inspect "$IMAGE" >/dev/null 2>&1 ||
    die "image '$IMAGE' does not exist; drop --no-build to build it."
fi

# The image exists from here on, which is what the volume copy runs inside.
migrate_legacy_volume

VOLUMES=(-v "$HOME/.claude:/root/.claude:ro")
if [[ -d "$HOME/.codex" ]]; then
  VOLUMES+=(-v "$HOME/.codex:/root/.codex:ro")
fi

printf '%s  Starting container...\n' "🚀"
# No --rm: keeping the exited container means `docker logs` still works after a
# crash. remove_container() clears it on the next start (and on --stop).
docker run -d \
  --name "$CONTAINER" \
  --network "$NETWORK" \
  -p "$PORT:8080" \
  "${VOLUMES[@]}" \
  -v "$VOLUME:/data" \
  -e HOST=0.0.0.0 \
  "$IMAGE" >/dev/null

# Report success only once the dashboard actually answers, and only if the
# container is still up a moment later -- "Running=true" one millisecond after
# `docker run` proves nothing about a process that crashes on its first request.
probe_supported=1
ready=0
for _ in $(seq 1 40); do            # up to ~20s
  container_running || break
  # `|| rc=$?` keeps errexit from firing on an expected failed probe.
  rc=0
  http_probe || rc=$?
  case "$rc" in
    0) ready=1; break ;;
    2) probe_supported=0; break ;;
  esac
  sleep 0.5
done

if (( ! probe_supported )); then
  printf '%s  No curl or python3 on PATH; skipping the HTTP check.\n' "ℹ"
  sleep 2
  ready=0
  container_running && ready=1
elif (( ready )); then
  # Settle, then confirm it did not fall over behind the first response.
  sleep 2
  if ! container_running || ! http_probe; then
    ready=0
  fi
fi

if (( ready )); then
  printf '%s  Running at http://localhost:%s\n' "✅" "$PORT"
  printf '   Logs:  bash scripts/run-docker.sh --logs\n'
  printf '   Stop:  bash scripts/run-docker.sh --stop\n'
  exit 0
fi

printf '%s  The dashboard did not come up. Last output:\n' "❌" >&2
docker logs "$CONTAINER" 2>&1 | tail -n 30 >&2 || true
printf '\n   Full logs:  docker logs %s\n' "$CONTAINER" >&2
exit 1
