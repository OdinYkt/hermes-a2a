#!/usr/bin/env bash

set -euo pipefail

export HERMES_HOME="${HERMES_HOME:-/opt/data}"
export A2A_ENABLED="${A2A_ENABLED:-true}"
export A2A_HOST="${A2A_HOST:-0.0.0.0}"
export A2A_PORT="${A2A_PORT:-8081}"
export WEBHOOK_ENABLED="${WEBHOOK_ENABLED:-true}"
export WEBHOOK_PORT="${WEBHOOK_PORT:-8644}"
export A2A_WEBHOOK_SECRET="${A2A_WEBHOOK_SECRET:-hermes-a2a-internal-webhook}"

rewrite_localhost_url() {
  local url="$1"
  if [ "${HERMES_A2A_REWRITE_LOCALHOST_BASE_URL:-true}" = "false" ]; then
    printf '%s' "$url"
    return
  fi

  case "$url" in
    http://localhost*) printf 'http://host.docker.internal%s' "${url#http://localhost}" ;;
    http://127.0.0.1*) printf 'http://host.docker.internal%s' "${url#http://127.0.0.1}" ;;
    https://localhost*) printf 'https://host.docker.internal%s' "${url#https://localhost}" ;;
    https://127.0.0.1*) printf 'https://host.docker.internal%s' "${url#https://127.0.0.1}" ;;
    *) printf '%s' "$url" ;;
  esac
}

if [ -z "${OPENAI_API_KEY:-}" ] && [ -n "${OPEN_AI_API_KEY:-}" ]; then
  export OPENAI_API_KEY="$OPEN_AI_API_KEY"
fi

if [ -z "${OPENAI_BASE_URL:-}" ] && [ -n "${OPEN_AI_URL:-}" ]; then
  export OPENAI_BASE_URL="$OPEN_AI_URL"
fi

if [ -n "${OPENAI_BASE_URL:-}" ]; then
  export OPENAI_BASE_URL="$(rewrite_localhost_url "$OPENAI_BASE_URL")"
fi

if [ -z "${OPENAI_MODEL:-}" ]; then
  export OPENAI_MODEL="${OPEN_AI_MODEL:-${OPENCODE_MODEL:-}}"
fi

append_no_proxy() {
  local current="$1"
  local token="$2"
  if [ -z "$current" ]; then
    printf '%s' "$token"
    return
  fi

  case ",${current}," in
    *",${token},"*) printf '%s' "$current" ;;
    *) printf '%s,%s' "$current" "$token" ;;
  esac
}

NO_PROXY="${NO_PROXY:-${no_proxy:-}}"
for token in 127.0.0.1 localhost ::1 host.docker.internal; do
  NO_PROXY="$(append_no_proxy "$NO_PROXY" "$token")"
done
export NO_PROXY
export no_proxy="$NO_PROXY"

mkdir -p "$HERMES_HOME"

# Patch upstream Hermes webhook.py so payload['session_chat_id'] becomes the
# session key (instead of always per-delivery_id). Lets the a2a plugin pin all
# messages from one TG sender / peer to the same Hermes session. Idempotent.
/opt/hermes/.venv/bin/python - <<'PY'
import pathlib
p = pathlib.Path("/opt/hermes/gateway/platforms/webhook.py")
src = p.read_text()
old = 'session_chat_id = f"webhook:{route_name}:{delivery_id}"'
new = 'session_chat_id = payload.get("session_chat_id") or f"webhook:{route_name}:{delivery_id}"'
if new in src:
    pass
elif old in src:
    p.write_text(src.replace(old, new, 1))
    print("[entrypoint] patched webhook.py session_chat_id derivation")
else:
    print("[entrypoint] WARN: webhook.py session_chat_id line not found, leaving unpatched")
PY

/opt/hermes-a2a/docker/install-plugin.sh

exec /opt/hermes/docker/entrypoint.sh "$@"
