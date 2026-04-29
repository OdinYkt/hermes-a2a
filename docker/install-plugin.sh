#!/usr/bin/env bash

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/opt/data}"
INSTALL_DIR="/opt/hermes"
STAGING_DIR="/opt/hermes-a2a"
PLUGIN_SOURCE="$STAGING_DIR/plugin"
PLUGIN_TARGET="$HERMES_HOME/plugins/a2a"
CONFIG_FILE="$HERMES_HOME/config.yaml"
ENV_FILE="$HERMES_HOME/.env"
LEGACY_HOME_LINK="$HOME/.hermes"

if [ -x "$INSTALL_DIR/.venv/bin/python" ]; then
  PYTHON_BIN="$INSTALL_DIR/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi

export CONFIG_FILE
export ENV_FILE
export WEBHOOK_PORT="${WEBHOOK_PORT:-8644}"
export A2A_WEBHOOK_SECRET="${A2A_WEBHOOK_SECRET:-hermes-a2a-internal-webhook}"

mkdir -p "$HERMES_HOME/plugins"

if [ ! -f "$ENV_FILE" ]; then
  cp "$INSTALL_DIR/.env.example" "$ENV_FILE"
fi

if [ ! -f "$CONFIG_FILE" ]; then
  cp "$INSTALL_DIR/cli-config.yaml.example" "$CONFIG_FILE"
fi

if [ ! -e "$LEGACY_HOME_LINK" ]; then
  ln -s "$HERMES_HOME" "$LEGACY_HOME_LINK"
elif [ -d "$LEGACY_HOME_LINK" ] && [ ! -L "$LEGACY_HOME_LINK" ] && [ -z "$(ls -A "$LEGACY_HOME_LINK")" ]; then
  rmdir "$LEGACY_HOME_LINK"
  ln -s "$HERMES_HOME" "$LEGACY_HOME_LINK"
fi

rm -rf "$PLUGIN_TARGET"
cp -R "$PLUGIN_SOURCE" "$PLUGIN_TARGET"

"$PYTHON_BIN" <<'PY'
import os
from pathlib import Path

import yaml

config_path = Path(os.environ["CONFIG_FILE"])
env_path = Path(os.environ["ENV_FILE"])
config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/")
openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
openai_model = os.getenv("OPENAI_MODEL", "").strip()

plugins = config.setdefault("plugins", {})
enabled = plugins.get("enabled")
if not isinstance(enabled, list):
    enabled = []

# Always-enabled plugins. ``observability/langfuse`` ships bundled with
# ``hermes-agent`` >= v0.11.0; on older base images the entry is harmless
# (Hermes logs a warning and skips it).
for plugin in ("a2a", "observability/langfuse"):
    if plugin not in enabled:
        enabled.append(plugin)
plugins["enabled"] = sorted(set(enabled))

disabled = plugins.get("disabled")
if isinstance(disabled, list):
    plugins["disabled"] = [
        name for name in disabled if name not in ("a2a", "observability/langfuse")
    ]

platforms = config.setdefault("platforms", {})
webhook = platforms.setdefault("webhook", {})
webhook["enabled"] = True

extra = webhook.setdefault("extra", {})
extra.setdefault("host", "127.0.0.1")
extra.setdefault("port", int(os.environ.get("WEBHOOK_PORT", "8644")))

routes = extra.setdefault("routes", {})
routes["a2a_trigger"] = {
    "events": ["a2a_inbound"],
    "secret": os.environ["A2A_WEBHOOK_SECRET"],
    "prompt": "[A2A wake] Process pending A2A queue.\ntask_id={task_id}\ntask_text={task_text}\nsender_name={sender_name}\nkind={kind}\ncorrelation_id={correlation_id}",
    "deliver": "log",
}

if openai_base_url:
    model = config.get("model")
    if not isinstance(model, dict):
        model = {"default": model} if model else {}
        config["model"] = model

    model["provider"] = "custom"
    model["base_url"] = openai_base_url
    if openai_model:
        model["default"] = openai_model
    if openai_api_key:
        model["api_key"] = openai_api_key
    model.pop("api_mode", None)

    custom_providers = config.get("custom_providers")
    if not isinstance(custom_providers, list):
        custom_providers = []

    provider_name = os.getenv("HERMES_A2A_CUSTOM_PROVIDER_NAME", "Docker OpenAI")
    provider_entry = None
    for entry in custom_providers:
        if isinstance(entry, dict) and str(entry.get("base_url", "")).rstrip("/") == openai_base_url:
            provider_entry = entry
            break

    if provider_entry is None:
        provider_entry = {"name": provider_name, "base_url": openai_base_url}
        custom_providers.append(provider_entry)

    provider_entry["name"] = provider_entry.get("name") or provider_name
    provider_entry["base_url"] = openai_base_url
    if openai_api_key:
        provider_entry["api_key"] = openai_api_key
    if openai_model:
        provider_entry["model"] = openai_model
    config["custom_providers"] = custom_providers

config_path.write_text(
    yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
    encoding="utf-8",
)

env_updates = {}
if openai_base_url:
    env_updates["OPENAI_BASE_URL"] = openai_base_url
if openai_api_key:
    env_updates["OPENAI_API_KEY"] = openai_api_key
if openai_model:
    env_updates["OPENAI_MODEL"] = openai_model
if openai_base_url:
    env_updates["HERMES_INFERENCE_PROVIDER"] = "custom"

if env_updates:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    seen = set()
    rewritten = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            rewritten.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in env_updates:
            rewritten.append(f"{key}={env_updates[key]}")
            seen.add(key)
        else:
            rewritten.append(line)
    for key, value in env_updates.items():
        if key not in seen:
            rewritten.append(f"{key}={value}")
    env_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
PY
