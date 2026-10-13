#!/usr/bin/env bash
# Deploy an immutable Forge Hub image from the GitHub-tested source tarball.
# Run as forge-deploy; never run user projects in the public hub.
set -Eeuo pipefail

SHA="${1:-}"
if [[ ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Expected an exact 40-character Git commit SHA" >&2
  exit 2
fi
DOMAIN="${FORGE_DOMAIN:-31-97-227-171.sslip.io}"
if [[ ! "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || [[ "$DOMAIN" == *..* ]]; then
  echo "FORGE_DOMAIN must be a DNS hostname" >&2
  exit 2
fi

ROOT="/opt/scrappy-forge"
ARCHIVE="${ROOT}/incoming/forge-${SHA}.tar.gz"
IMAGE="scrappy-forge:${SHA}"
LIVE="scrappy-forge-hub"
CANDIDATE="scrappy-forge-hub-candidate"
BACKUP="scrappy-forge-hub-rollback"

command -v docker >/dev/null || { echo "Docker unavailable: run bootstrap first" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl unavailable: run bootstrap first" >&2; exit 1; }
[[ -f "$ARCHIVE" ]] || { echo "Missing uploaded source bundle: $ARCHIVE" >&2; exit 1; }

WORK="$(mktemp -d "${ROOT}/build.XXXXXXXX")"
cleanup() {
  docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
  rm -rf -- "$WORK"
}
trap cleanup EXIT

tar -xzf "$ARCHIVE" --no-same-owner --no-same-permissions -C "$WORK"
[[ -f "$WORK/Dockerfile" && -f "$WORK/pyproject.toml" ]] || {
  echo "Source archive does not contain the expected build files" >&2
  exit 1
}
echo "Building immutable release ${SHA}"
docker build --tag "$IMAGE" "$WORK"

start_container() {
  local name="$1" port="$2"
  docker run -d \
    --name "$name" \
    --restart unless-stopped \
    --pull never \
    --read-only \
    --security-opt no-new-privileges \
    --cap-drop ALL \
    --pids-limit 128 \
    --memory 768m \
    --cpus 2 \
    -p "127.0.0.1:${port}:8080" \
    -e "FORGE_PUBLIC_URL=https://${DOMAIN}" \
    -e "FORGE_GIT_COMMIT_SHA=${SHA}" \
    -e "FORGE_SIGNUP_ENABLED=0" \
    "$IMAGE" >/dev/null
}

healthy() {
  local port="$1"
  for _ in $(seq 1 30); do
    if curl --noproxy '*' --silent --show-error --fail --max-time 3 \
      "http://127.0.0.1:${port}/readyz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# Validate the new image without touching the existing production listener.
docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
start_container "$CANDIDATE" 18081
if ! healthy 18081; then
  echo "Candidate readiness failed. Existing production container is unchanged." >&2
  docker logs --tail 60 "$CANDIDATE" >&2 || true
  exit 1
fi
docker rm -f "$CANDIDATE" >/dev/null

# Repair an interrupted previous promotion before touching the live service.
# The old container is retained under BACKUP until promotion is confirmed.
if docker container inspect "$BACKUP" >/dev/null 2>&1; then
  if ! docker container inspect "$LIVE" >/dev/null 2>&1; then
    docker rename "$BACKUP" "$LIVE"
    docker start "$LIVE" >/dev/null
  elif healthy 18080; then
    docker rm -f "$BACKUP" >/dev/null
  else
    docker rm -f "$LIVE" >/dev/null
    docker rename "$BACKUP" "$LIVE"
    docker start "$LIVE" >/dev/null
  fi
fi

# Retain the previous container so a failed promotion is reversible.
HAD_PREVIOUS=0
if docker container inspect "$LIVE" >/dev/null 2>&1; then
  HAD_PREVIOUS=1
  docker stop --time 10 "$LIVE" >/dev/null
  docker rename "$LIVE" "$BACKUP"
fi

if ! start_container "$LIVE" 18080 || ! healthy 18080; then
  echo "Production readiness failed. Restoring last healthy container." >&2
  docker logs --tail 60 "$LIVE" >&2 || true
  docker rm -f "$LIVE" >/dev/null 2>&1 || true
  if [[ "$HAD_PREVIOUS" == 1 ]]; then
    docker rename "$BACKUP" "$LIVE"
    docker start "$LIVE" >/dev/null
    healthy 18080 || echo "WARNING: rollback container health check failed" >&2
  fi
  exit 1
fi

docker rm -f "$BACKUP" >/dev/null 2>&1 || true
rm -f -- "$ARCHIVE"
echo "Deployed ${SHA} on localhost:18080; public TLS proxy: https://${DOMAIN}"
