# Hermes A2A Container Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a `hermes-a2a` container image that starts a full Hermes runtime with the `a2a` plugin enabled, reads model credentials from a Docker `--env-file`, and is verifiable through real A2A HTTP requests.

**Architecture:** Build on top of `nousresearch/hermes-agent` and keep Hermes upstream bootstrap intact. Add a thin wrapper entrypoint that installs the local `plugin/` payload into `$HERMES_HOME/plugins/a2a`, guarantees `a2a` is present in `plugins.enabled`, bridges legacy `~/.hermes/...` paths to the container's `$HERMES_HOME`, then hands off to `/opt/hermes/docker/entrypoint.sh` with `gateway run` as the default command.

**Tech Stack:** Docker, bash, Python 3 stdlib HTTP clients, Hermes Agent official image, YAML config patching via PyYAML already present in the base image.

---

### Task 1: Add failing container smoke harness

**Files:**
- Create: `scripts/a2a-smoke.sh`
- Create: `scripts/a2a-e2e.sh`

- [ ] **Step 1: Write the failing smoke script**

Create `scripts/a2a-smoke.sh` with checks for:
- image exists locally
- gateway `/health` responds with JSON
- A2A `/health` responds with `status=ok`
- `/.well-known/agent.json` responds with `protocol=a2a`

- [ ] **Step 2: Run the smoke script before image exists**

Run: `bash scripts/a2a-smoke.sh`
Expected: FAIL with `Image hermes-a2a:dev not found. Build it first.`

- [ ] **Step 3: Write the failing end-to-end script**

Create `scripts/a2a-e2e.sh` with checks for:
- unauthorized request returns HTTP 401 when `A2A_AUTH_TOKEN` is set
- `tasks/send` returns `completed` or `working`
- `tasks/get` polling reaches `completed`
- response artifact contains `ACK-A2A`
- audit and conversation persistence files exist under container state volume

- [ ] **Step 4: Run the end-to-end script before image exists**

Run: `ENV_FILE=/home/odinykt/projects/Threads/.env bash scripts/a2a-e2e.sh`
Expected: FAIL with `Image hermes-a2a:dev not found. Build it first.`

### Task 2: Add container runtime wrapper

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `docker/entrypoint.sh`
- Create: `docker/install-plugin.sh`
- Create: `docker/healthcheck.sh`

- [ ] **Step 1: Build the image contract around official Hermes image**

Use `FROM nousresearch/hermes-agent`, expose `8642` and `8081`, install wrapper scripts under `/opt/hermes-a2a/`, set wrapper as entrypoint, keep default command `gateway run`.

- [ ] **Step 2: Implement plugin installation before upstream bootstrap**

`docker/install-plugin.sh` must:
- copy `plugin/` to `$HERMES_HOME/plugins/a2a`
- create `$HOME/.hermes -> $HERMES_HOME` compatibility link when absent
- ensure `$HERMES_HOME/config.yaml` exists
- ensure `plugins.enabled` contains `a2a`
- remove `a2a` from `plugins.disabled` if present

- [ ] **Step 3: Implement wrapper entrypoint**

`docker/entrypoint.sh` must:
- set `A2A_HOST=0.0.0.0` by default
- set `A2A_PORT=8081` by default
- set `A2A_ENABLED=true` by default
- invoke `docker/install-plugin.sh`
- `exec /opt/hermes/docker/entrypoint.sh "$@"`

- [ ] **Step 4: Implement container healthcheck**

`docker/healthcheck.sh` must verify both:
- `http://127.0.0.1:8642/health`
- `http://127.0.0.1:8081/health`

- [ ] **Step 5: Build image and verify smoke turns green**

Run:
`docker build -t hermes-a2a:dev .`

Then:
`bash scripts/a2a-smoke.sh`

Expected: PASS.

### Task 3: Add operator-facing Docker docs and examples

**Files:**
- Create: `examples/docker.env.example`
- Create: `examples/docker.config.yaml.example`
- Create: `docs/docker.md`
- Modify: `README.md`
- Modify: `install.sh`

- [ ] **Step 1: Add example env file**

Document required runtime env keys, including provider credential placeholders plus A2A settings such as `A2A_ENABLED`, `A2A_HOST`, `A2A_PORT`, `A2A_AUTH_TOKEN`, and `A2A_AGENT_NAME`.

- [ ] **Step 2: Add example config file**

Show a minimal Hermes config with a model section and an optional `a2a.agents` list for outbound calls.

- [ ] **Step 3: Write Docker operator guide**

Document build, run, env-file usage, persisted state path `/opt/data`, health endpoints, agent card URL, and limitation that one state volume must not be shared by multiple running Hermes containers.

- [ ] **Step 4: Update README quickstart**

Add a short Docker section that links to `docs/docker.md`.

- [ ] **Step 5: Update local installer output**

Keep local install flow intact, but mention Docker flow in `install.sh` output.

### Task 4: Run full end-to-end verification against real container

**Files:**
- Reuse: `scripts/a2a-e2e.sh`

- [ ] **Step 1: Run end-to-end script with real env file**

Run:
`ENV_FILE=/home/odinykt/projects/Threads/.env bash scripts/a2a-e2e.sh`

Expected:
- container starts
- unauthorized request returns 401
- authorized `tasks/send` returns `completed` or `working`
- polling reaches `completed`
- reply contains `ACK-A2A`

- [ ] **Step 2: Verify persistence artifacts exist**

Checks must confirm container state contains:
- `a2a_audit.jsonl`
- `a2a_conversations/<agent>/<date>.md`

- [ ] **Step 3: Re-run smoke after end-to-end verification**

Run: `bash scripts/a2a-smoke.sh`
Expected: PASS again.
