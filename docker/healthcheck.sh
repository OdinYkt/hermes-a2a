#!/usr/bin/env bash

set -euo pipefail

python3 <<'PY'
import json
import os
import urllib.request
import urllib.error


def fetch(url: str) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


api_server_enabled = os.getenv("API_SERVER_ENABLED", "false").lower() in {"1", "true", "yes", "on"}

if api_server_enabled:
    gateway = fetch("http://127.0.0.1:8642/health")
    if gateway.get("status") != "ok":
        raise SystemExit(f"Gateway unhealthy: {gateway!r}")

a2a = fetch("http://127.0.0.1:8081/health")
if a2a.get("status") != "ok":
    raise SystemExit(f"A2A unhealthy: {a2a!r}")
PY
