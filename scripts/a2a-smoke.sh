#!/usr/bin/env bash

set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-hermes-a2a:dev}"
CONTAINER_NAME="${CONTAINER_NAME:-hermes-a2a-smoke}"
HOST_GATEWAY_PORT="${HOST_GATEWAY_PORT:-18642}"
HOST_A2A_PORT="${HOST_A2A_PORT:-18081}"
A2A_AUTH_TOKEN="${A2A_AUTH_TOKEN:-smoke-token}"
API_SERVER_ENABLED="${API_SERVER_ENABLED:-false}"
API_SERVER_KEY="${API_SERVER_KEY:-}"

TEMP_DATA_DIR="$(mktemp -d)"
chmod 777 "$TEMP_DATA_DIR"

docker_args=(
  -d --rm
  --name "$CONTAINER_NAME"
  --add-host=host.docker.internal:host-gateway
  -p "${HOST_GATEWAY_PORT}:8642"
  -p "${HOST_A2A_PORT}:8081"
  -v "$TEMP_DATA_DIR:/opt/data"
  -e A2A_ENABLED=true
  -e A2A_HOST=0.0.0.0
  -e A2A_PORT=8081
  -e A2A_AGENT_NAME=hermes-a2a-smoke
  -e API_SERVER_ENABLED="$API_SERVER_ENABLED"
  -e A2A_AUTH_TOKEN="$A2A_AUTH_TOKEN"
)

if [ -n "$API_SERVER_KEY" ]; then
  docker_args+=( -e API_SERVER_KEY="$API_SERVER_KEY" )
fi

if ! docker image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
  echo "Image $IMAGE_TAG not found. Build it first."
  exit 1
fi

cleanup() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  docker run --rm -u 0:0 -v "$TEMP_DATA_DIR:/cleanup" --entrypoint /bin/sh "$IMAGE_TAG" -lc 'rm -rf /cleanup/* /cleanup/.[!.]* /cleanup/..?* 2>/dev/null || true' >/dev/null 2>&1 || true
  rm -rf "$TEMP_DATA_DIR" >/dev/null 2>&1 || true
}

trap cleanup EXIT

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

docker run "${docker_args[@]}" "$IMAGE_TAG" >/dev/null

python3 - "$HOST_GATEWAY_PORT" "$HOST_A2A_PORT" "$API_SERVER_ENABLED" <<'PY'
import json
import sys
import time
import urllib.request

gateway_port = int(sys.argv[1])
a2a_port = int(sys.argv[2])
api_server_enabled = sys.argv[3].lower() in {"1", "true", "yes", "on"}


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_json(url: str, key: str, value: str, timeout: int = 90) -> dict:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            payload = fetch_json(url)
            if payload.get(key) == value:
                return payload
            last_error = f"unexpected payload from {url}: {payload!r}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(1)
    raise SystemExit(f"Timed out waiting for {url}: {last_error}")


if api_server_enabled:
    gateway = wait_json(f"http://127.0.0.1:{gateway_port}/health", "status", "ok")
    if gateway.get("status") != "ok":
        raise SystemExit(f"Gateway health invalid: {gateway!r}")

a2a = wait_json(f"http://127.0.0.1:{a2a_port}/health", "status", "ok")
if a2a.get("agent") != "hermes-a2a-smoke":
    raise SystemExit(f"A2A health invalid: {a2a!r}")

card = fetch_json(f"http://127.0.0.1:{a2a_port}/.well-known/agent.json")
if card.get("protocol") != "a2a":
    raise SystemExit(f"Agent card protocol mismatch: {card!r}")
if card.get("name") != "hermes-a2a-smoke":
    raise SystemExit(f"Agent card name mismatch: {card!r}")

print("smoke-ok")
PY
