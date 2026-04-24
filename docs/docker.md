# Docker Runtime for hermes-a2a

This image starts a full Hermes runtime and the `a2a` plugin in one container.

## What the image does

- builds on `nousresearch/hermes-agent`
- copies this repo's `plugin/` payload into `$HERMES_HOME/plugins/a2a`
- ensures `a2a` is enabled in `plugins.enabled`
- enables Hermes webhook wake-up on `WEBHOOK_PORT` so inbound A2A requests trigger an agent turn immediately
- defaults `A2A_HOST=0.0.0.0` so the A2A server is reachable outside the container
- leaves Hermes API server on `8642` disabled by default; enable it explicitly if you need OpenAI-compatible API access
- keeps persistent state under `/opt/data`

Inside the container, Hermes still uses `HERMES_HOME=/opt/data`. For plugin compatibility, `~/.hermes` is linked back to that same state directory, so files like `a2a_audit.jsonl` and `a2a_conversations/` also persist under `/opt/data`.

## Build

```bash
docker build -t hermes-a2a:dev .
```

## Prepare runtime files

1. Copy `examples/docker.env.example` to a local env file.
2. Fill in model credentials for your provider.
3. Optional: copy `examples/docker.config.yaml.example` into your persistent state volume as `config.yaml` before first boot.

## Run

```bash
docker run -d --rm \
  --name hermes-a2a \
  --add-host=host.docker.internal:host-gateway \
  -p 8081:8081 \
  --env-file /path/to/runtime.env \
  -v /path/to/hermes-data:/opt/data \
  hermes-a2a:dev
```

If you also want Hermes API server access on `8642`, add:

```bash
-p 8642:8642 \
-e API_SERVER_ENABLED=true \
-e API_SERVER_HOST=0.0.0.0 \
-e API_SERVER_PORT=8642 \
-e API_SERVER_KEY=change-me-api-server
```

For OpenAI-compatible providers running on the Docker host, set either:

```bash
OPENAI_BASE_URL=http://host.docker.internal:8100/v1
OPENAI_API_KEY=...
OPENAI_MODEL=cx/gpt-5.4
```

or the compatibility names used by some local tooling:

```bash
OPEN_AI_URL=http://localhost:8100/v1
OPEN_AI_API_KEY=...
OPEN_AI_MODEL=cx/gpt-5.4
```

The container maps `OPEN_AI_*` to Hermes' `OPENAI_*` names and rewrites `localhost`/`127.0.0.1` base URLs to `host.docker.internal` by default. Disable that rewrite with `HERMES_A2A_REWRITE_LOCALHOST_BASE_URL=false` if you run the model backend inside the same container network namespace.

For the e2e script, you can override only the model while reusing the same env credentials:

```bash
OPENAI_MODEL=openrouter/auto ENV_FILE=/path/to/runtime.env bash scripts/a2a-e2e.sh
```

## Verify runtime

A2A health:

```bash
curl -fsS http://127.0.0.1:8081/health
```

Agent card:

```bash
curl -fsS http://127.0.0.1:8081/.well-known/agent.json
```

Optional Hermes API server health:

```bash
curl -fsS -H "Authorization: Bearer $API_SERVER_KEY" http://127.0.0.1:8642/health
```

## End-to-end request

```bash
curl -fsS -X POST http://127.0.0.1:8081 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $A2A_AUTH_TOKEN" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "tasks/send",
    "params": {
      "id": "task-001",
      "message": {
        "role": "user",
        "parts": [{"type": "text", "text": "Reply with exactly ACK-A2A and nothing else."}],
        "metadata": {"sender_name": "docker-check"}
      }
    }
  }'
```

If the result state is `working`, poll with `tasks/get` until the state becomes `completed`.

## Persisted files

The container keeps all state under `/opt/data`.

Relevant A2A files after traffic:

- `/opt/data/plugins/a2a/`
- `/opt/data/config.yaml`
- `/opt/data/.env`
- `/opt/data/a2a_audit.jsonl`
- `/opt/data/a2a_conversations/<agent>/<date>.md`

## Optional API server

Hermes API server on port `8642` is upstream Hermes functionality, not required for A2A delivery. If you expose it beyond loopback, Hermes requires `API_SERVER_KEY`. Without that key, upstream Hermes refuses to bind `0.0.0.0`.

## Important limitation

Do not run multiple Hermes containers against the same `/opt/data` volume at the same time.
