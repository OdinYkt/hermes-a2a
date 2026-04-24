#!/usr/bin/env bash

set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-hermes-a2a:dev}"
ENV_FILE="${ENV_FILE:-}"
CONTAINER_NAME="${CONTAINER_NAME:-hermes-a2a-e2e}"
HOST_GATEWAY_PORT="${HOST_GATEWAY_PORT:-28642}"
HOST_A2A_PORT="${HOST_A2A_PORT:-28081}"
A2A_AUTH_TOKEN="${A2A_AUTH_TOKEN:-e2e-token}"
API_SERVER_ENABLED="${API_SERVER_ENABLED:-false}"
API_SERVER_KEY="${API_SERVER_KEY:-}"
OPENAI_MODEL="${OPENAI_MODEL:-}"

if ! docker image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
  echo "Image $IMAGE_TAG not found. Build it first."
  exit 1
fi

if [ -z "$ENV_FILE" ]; then
  echo "ENV_FILE is required. Point it at a .env file with Hermes model credentials."
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "ENV_FILE does not exist: $ENV_FILE"
  exit 1
fi

TEMP_DATA_DIR="$(mktemp -d)"
chmod 777 "$TEMP_DATA_DIR"

cleanup() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  docker run --rm -u 0:0 -v "$TEMP_DATA_DIR:/cleanup" --entrypoint /bin/sh "$IMAGE_TAG" -lc 'rm -rf /cleanup/* /cleanup/.[!.]* /cleanup/..?* 2>/dev/null || true' >/dev/null 2>&1 || true
  rm -rf "$TEMP_DATA_DIR" >/dev/null 2>&1 || true
}

trap cleanup EXIT

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

docker_args=(
  -d --rm
  --name "$CONTAINER_NAME"
  --add-host=host.docker.internal:host-gateway
  -p "${HOST_GATEWAY_PORT}:8642"
  -p "${HOST_A2A_PORT}:8081"
  -v "$TEMP_DATA_DIR:/opt/data"
  --env-file "$ENV_FILE"
  -e A2A_ENABLED=true
  -e A2A_HOST=0.0.0.0
  -e A2A_PORT=8081
  -e A2A_AGENT_NAME=hermes-a2a-e2e
  -e API_SERVER_ENABLED="$API_SERVER_ENABLED"
  -e A2A_AUTH_TOKEN="$A2A_AUTH_TOKEN"
)

if [ -n "$API_SERVER_KEY" ]; then
  docker_args+=( -e API_SERVER_KEY="$API_SERVER_KEY" )
fi

if [ -n "$OPENAI_MODEL" ]; then
  docker_args+=( -e OPENAI_MODEL="$OPENAI_MODEL" )
fi

docker run "${docker_args[@]}" "$IMAGE_TAG" >/dev/null

python3 - "$HOST_GATEWAY_PORT" "$HOST_A2A_PORT" "$A2A_AUTH_TOKEN" "$TEMP_DATA_DIR" "$API_SERVER_ENABLED" <<'PY'
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

gateway_port = int(sys.argv[1])
a2a_port = int(sys.argv[2])
token = sys.argv[3]
data_dir = pathlib.Path(sys.argv[4])
api_server_enabled = sys.argv[5].lower() in {"1", "true", "yes", "on"}


def request_json(
    url: str,
    method: str = "GET",
    payload: dict | None = None,
    headers: dict | None = None,
    timeout: int = 10,
) -> tuple[int, dict]:
    body = None
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_for_health(url: str, timeout: int = 120) -> dict:
    deadline = time.time() + timeout
    last_payload = None
    while time.time() < deadline:
        try:
            _, payload = request_json(url)
            last_payload = payload
            if payload.get("status") == "ok":
                return payload
        except Exception as exc:  # noqa: BLE001
            last_payload = {"error": str(exc)}
        time.sleep(1)
    raise SystemExit(f"Timed out waiting for health at {url}: {last_payload!r}")


if api_server_enabled:
    wait_for_health(f"http://127.0.0.1:{gateway_port}/health")
wait_for_health(f"http://127.0.0.1:{a2a_port}/health")

status, payload = request_json(
    f"http://127.0.0.1:{a2a_port}",
    method="POST",
    payload={
        "jsonrpc": "2.0",
        "id": "unauthorized-check",
        "method": "tasks/send",
        "params": {
            "id": "unauthorized-check",
            "message": {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
        },
    },
)
if status != 401:
    raise SystemExit(f"Expected unauthorized 401, got {status} with {payload!r}")

task_id = "task-e2e-ack"
status, payload = request_json(
    f"http://127.0.0.1:{a2a_port}",
    method="POST",
    headers={"Authorization": f"Bearer {token}"},
    timeout=150,
    payload={
        "jsonrpc": "2.0",
        "id": "authorized-send",
        "method": "tasks/send",
        "params": {
            "id": task_id,
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "Reply with exactly ACK-A2A and nothing else."}],
                "metadata": {"sender_name": "e2e-runner"},
            },
        },
    },
)
if status != 200:
    raise SystemExit(f"tasks/send failed: {status} {payload!r}")

result = payload.get("result", {})
state = result.get("status", {}).get("state")
if state not in {"completed", "working"}:
    raise SystemExit(f"Unexpected tasks/send state: {payload!r}")

if state == "working":
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(4)
        _, poll_payload = request_json(
            f"http://127.0.0.1:{a2a_port}",
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
            payload={
                "jsonrpc": "2.0",
                "id": "authorized-get",
                "method": "tasks/get",
                "params": {"id": task_id},
            },
        )
        result = poll_payload.get("result", {})
        state = result.get("status", {}).get("state")
        if state == "completed":
            break
    else:
        raise SystemExit(f"Timed out waiting for task completion. Last payload: {poll_payload!r}")

artifacts = result.get("artifacts", [])
texts = []
for artifact in artifacts:
    for part in artifact.get("parts", []):
        if part.get("type") == "text":
            texts.append(part.get("text", ""))

joined = "\n".join(texts)
if "ACK-A2A" not in joined:
    raise SystemExit(f"Expected ACK-A2A in response, got: {joined!r}")

audit_log = data_dir / "a2a_audit.jsonl"
conversation_dir = data_dir / "a2a_conversations"
if not audit_log.exists():
    raise SystemExit(f"Missing audit log: {audit_log}")
if not any(conversation_dir.rglob("*.md")):
    raise SystemExit(f"Missing persisted conversations under {conversation_dir}")

print("e2e-ok")
PY
