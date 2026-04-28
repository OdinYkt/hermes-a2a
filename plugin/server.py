"""A2A HTTP server — runs in a background thread, no asyncio.

Handles inbound A2A JSON-RPC requests. Messages are queued and picked up
by the pre_llm_call hook; responses are captured by post_llm_call and
returned to the caller.
"""
# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from threading import Event, Lock
from collections import OrderedDict
from typing import Any, Optional, cast
import urllib.request
import urllib.error

from .security import RateLimiter, audit, filter_outbound, sanitize_inbound

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8081
_TASK_CACHE_MAX = 1000
_MAX_PENDING = 10
_RESPONSE_TIMEOUT = float(os.getenv("A2A_RESPONSE_TIMEOUT", "120"))  # seconds to wait for agent response
_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("A2A_RATE_LIMIT_MAX_REQUESTS", "20"))
_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("A2A_RATE_LIMIT_WINDOW_SECONDS", "60"))

try:
    from hermes_cli import __version__ as HERMES_VERSION
except Exception:
    HERMES_VERSION = "0.0.0"


class _PendingTask:
    __slots__ = ("task_id", "text", "metadata", "response", "ready", "created_at")

    def __init__(self, task_id: str, text: str, metadata: dict):
        self.task_id = task_id
        self.text = text
        self.metadata = metadata
        self.response: Optional[str] = None
        self.ready = Event()
        self.created_at = time.time()


class TaskQueue:
    """Thread-safe queue for pending A2A tasks."""

    def __init__(self):
        self._pending: OrderedDict[str, _PendingTask] = OrderedDict()
        self._completed: OrderedDict[str, _PendingTask] = OrderedDict()
        self._lock = Lock()

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def enqueue(self, task_id: str, text: str, metadata: dict) -> _PendingTask:
        task = _PendingTask(task_id, text, metadata)
        with self._lock:
            self._pending[task_id] = task
            while len(self._pending) > _TASK_CACHE_MAX:
                _, old = self._pending.popitem(last=False)
                old.response = "(dropped — queue overflow)"
                old.ready.set()
        return task

    def drain_pending(self, exclude: set[str] | None = None) -> list[_PendingTask]:
        with self._lock:
            if exclude:
                return [t for t in self._pending.values() if t.task_id not in exclude]
            return list(self._pending.values())

    def get_pending(self, task_id: str) -> Optional["_PendingTask"]:
        with self._lock:
            return self._pending.get(task_id)

    def ensure_pending(self, task_id: str, text: str, metadata: dict) -> "_PendingTask":
        """Idempotent enqueue: returns existing pending entry if present,
        otherwise registers a fresh one. Used by pre_llm_call when the
        webhook payload references a task_id whose in-memory _PendingTask
        was lost (e.g. after a container restart) — without this fallback
        the wake event would observe an empty queue and never finalize."""
        with self._lock:
            existing = self._pending.get(task_id)
            if existing:
                return existing
            task = _PendingTask(task_id, text, metadata)
            self._pending[task_id] = task
            while len(self._pending) > _TASK_CACHE_MAX:
                _, old = self._pending.popitem(last=False)
                old.response = "(dropped — queue overflow)"
                old.ready.set()
            return task

    def complete(self, task_id: str, response: str) -> None:
        with self._lock:
            task = self._pending.pop(task_id, None)
            if task:
                task.response = response
                task.ready.set()
                self._completed[task_id] = task
                while len(self._completed) > _TASK_CACHE_MAX:
                    self._completed.popitem(last=False)

    def cancel(self, task_id: str) -> None:
        with self._lock:
            task = self._pending.pop(task_id, None)
            if task:
                task.response = "(canceled)"
                task.ready.set()
                self._completed[task_id] = task

    def get_status(self, task_id: str) -> dict:
        with self._lock:
            if task_id in self._pending:
                return {"state": "working"}
            task = self._completed.get(task_id)
            if task:
                if task.response == "(canceled)":
                    return {"state": "canceled"}
                return {
                    "state": "completed",
                    "response": filter_outbound(task.response or ""),
                }
        return {"state": "unknown"}


task_queue = TaskQueue()


_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


_STATIC_SESSION_CHAT_ID = "webhook:a2a_trigger:default"
def _derive_session_chat_id(metadata: dict | None) -> str:
    """Single-user single-session model: every inbound (TG msg, peer call,
    peer callback) folds into one persistent Hermes session for this agent.
    sender_id / sender_name are intentionally ignored here so callbacks
    from cc land in the same session as the original user request."""
    return _STATIC_SESSION_CHAT_ID


def _trigger_webhook(metadata: dict | None = None, task_text: str | None = None, task_id: str | None = None):
    """POST to the internal webhook to trigger an agent turn.

    Includes the task text + id in the body so the webhook prompt template
    ({__raw__}) substitutes them into the user message that gets written to
    the session transcript. Without this, only the wake stub lands in JSONL
    and the LLM has no record of past user requests after a restart."""
    secret = os.getenv("A2A_WEBHOOK_SECRET", "")
    if not secret:
        return

    port = int(os.getenv("WEBHOOK_PORT", "8644"))
    attempts = int(os.getenv("A2A_WEBHOOK_TRIGGER_ATTEMPTS", "30"))
    delay = float(os.getenv("A2A_WEBHOOK_TRIGGER_DELAY", "1"))
    session_chat_id = _derive_session_chat_id(metadata)
    body_payload: dict = {
        "event_type": "a2a_inbound",
        "session_chat_id": session_chat_id,
    }
    if task_id is not None:
        body_payload["task_id"] = task_id
    if task_text is not None:
        # Truncate to keep the wake prompt bounded — full text still lives
        # in the task queue for the plugin to inject as conversation context.
        body_payload["task_text"] = task_text[:4000]
    if metadata:
        sender = metadata.get("sender_name")
        if sender:
            body_payload["sender_name"] = sender
        kind = metadata.get("kind")
        if kind:
            body_payload["kind"] = kind
        corr = metadata.get("correlation_id")
        if corr:
            body_payload["correlation_id"] = corr
    body = json.dumps(body_payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    # X-Request-ID needs to be globally unique because Hermes' webhook
    # adapter idempotency-dedups on it. Concurrent triggers (multiple
    # peers calling at once) hit the same millisecond on time.time(), so
    # using a wallclock-only id collides and silently drops events. The
    # task_id is already unique per inbound A2A request — make it the
    # primary key, with a millisecond suffix to keep retried trigger
    # attempts from the same task_id coalescing into one delivery.
    req_id_base = task_id if task_id else session_chat_id
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/webhooks/a2a_trigger",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-Request-ID": f"{req_id_base}-{int(time.time()*1000)}",
        },
        method="POST",
    )
    last_error = None
    for attempt in range(1, attempts + 1):
        if attempt == 1:
            audit.log("webhook_trigger_started", {"task_id": task_id, "port": port})
        try:
            with _LOOPBACK_OPENER.open(req, timeout=5) as resp:
                audit.log("webhook_trigger_accepted", {"task_id": task_id, "status": resp.status})
                logger.info("[A2A] Webhook trigger accepted for task %s: %d", task_id, resp.status)
                return
        except Exception as e:
            last_error = e
            if attempt < attempts:
                time.sleep(delay)

    audit.log("webhook_trigger_failed", {"task_id": task_id, "error": str(last_error)})
    logger.warning("[A2A] Webhook trigger failed for task %s after %d attempts: %s", task_id, attempts, last_error)


class A2ARequestHandler(BaseHTTPRequestHandler):
    """Handles A2A HTTP requests."""

    server: Any

    def log_message(self, format, *args):
        logger.debug("A2A HTTP: %s", format % args)

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        token = self.server.auth_token
        if not token:
            remote = self.client_address[0]
            return remote in ("127.0.0.1", "::1")
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth_header[7:].strip(), token)

    def do_GET(self) -> None:
        if self.path in ("/.well-known/agent.json", "/.well-known/agent-card.json"):
            self._send_json(self.server.build_agent_card())
        elif self.path == "/health":
            self._send_json(
                {
                    "status": "ok",
                    "agent": self.server.agent_name,
                    "version": HERMES_VERSION,
                }
            )
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        if not self._check_auth():
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32000, "message": "Unauthorized"},
                    "id": None,
                },
                401,
            )
            return

        version = self.headers.get("A2A-Version", "").strip()
        if version and version != "1.0":
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32000,
                        "message": f"Unsupported A2A-Version: {version}",
                    },
                    "id": None,
                },
                406,
            )
            return

        if not self.server.limiter.allow(self.client_address[0]):
            audit.log("rate_limited", {"client": self.client_address[0]})
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32000, "message": "Rate limit exceeded"},
                    "id": None,
                },
                429,
            )
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32700, "message": "Parse error"},
                    "id": None,
                },
                400,
            )
            return

        method = body.get("method", "")
        params = body.get("params", {})
        rpc_id = body.get("id")

        audit.log("rpc_request", {"method": method, "client": self.client_address[0]})

        if method in ("SendMessage", "message/send"):
            result = self._handle_v1_message_send(params)
            self._send_json({"jsonrpc": "2.0", "result": {"task": _task_to_v1(result)}, "id": rpc_id})
            return
        if method == "tasks/send":
            result = self._handle_task_send(params)
        elif method in ("GetTask", "tasks/get"):
            tid = params.get("id", "")
            status = task_queue.get_status(tid)
            result = {"id": tid, "status": {"state": status["state"]}}
            if status.get("response"):
                result["artifacts"] = [
                    {
                        "parts": [{"type": "text", "text": status["response"]}],
                        "index": 0,
                    }
                ]
            if method == "GetTask":
                self._send_json({"jsonrpc": "2.0", "result": _task_to_v1(result), "id": rpc_id})
                return
        elif method == "tasks/cancel":
            tid = params.get("id", "")
            task_queue.cancel(tid)
            result = {"id": tid, "status": {"state": "canceled"}}
        else:
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                    "id": rpc_id,
                }
            )
            return

        self._send_json({"jsonrpc": "2.0", "result": result, "id": rpc_id})

    def _handle_v1_message_send(self, params: dict) -> dict:
        message = params.get("message", {}) if isinstance(params, dict) else {}
        task_id = message.get("taskId") or message.get("task_id") or message.get("messageId") or str(uuid.uuid4())
        parts = []
        for part in message.get("parts", []):
            if "text" in part:
                parts.append({"type": "text", "text": part.get("text", "")})
            elif part.get("kind") == "text":
                parts.append({"type": "text", "text": part.get("text", "")})
        legacy_params = {
            "id": task_id,
            "message": {
                "parts": parts,
                "metadata": message.get("metadata", {}),
            },
        }
        return self._handle_task_send(legacy_params)

    def _handle_task_send(self, params: dict) -> dict:
        task_id = params.get("id", str(uuid.uuid4()))
        message = params.get("message", {})

        text_parts = []
        for part in message.get("parts", []):
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
        user_text = "\n".join(text_parts)

        if not user_text.strip():
            return {
                "id": task_id,
                "status": {"state": "failed"},
                "artifacts": [
                    {"parts": [{"type": "text", "text": "Empty message"}], "index": 0}
                ],
            }

        user_text = sanitize_inbound(user_text)
        metadata = message.get("metadata", {})
        if "sender_name" not in metadata:
            metadata["sender_name"] = metadata.get(
                "agent_name", f"agent-{self.client_address[0]}"
            )
        raw_name = metadata.get("sender_name", "")
        metadata["sender_name"] = "".join(
            c for c in raw_name if c.isalnum() or c in "-_.@ "
        )[:64]

        audit.log("task_received", {"task_id": task_id, "length": len(user_text)})

        if task_queue.pending_count() >= _MAX_PENDING:
            return {
                "id": task_id,
                "status": {"state": "failed"},
                "artifacts": [
                    {
                        "parts": [
                            {
                                "type": "text",
                                "text": "Agent busy — too many pending tasks",
                            }
                        ],
                        "index": 0,
                    }
                ],
            }

        task = task_queue.enqueue(task_id, user_text, metadata)
        logger.info("[A2A] Enqueued inbound task %s", task_id)

        threading.Thread(
            target=_trigger_webhook,
            kwargs={"metadata": metadata, "task_text": user_text, "task_id": task_id},
            daemon=True,
        ).start()

        # Async submit: caller asked to fire-and-forget. Return task_id
        # immediately; caller polls with tasks/get or waits for callback.
        if metadata.get("async") in (True, "true", "1", 1):
            return {
                "id": task_id,
                "status": {"state": "submitted"},
                "artifacts": [
                    {"parts": [{"type": "text", "text": "Submitted; poll tasks/get or wait for callback."}], "index": 0}
                ],
            }

        task.ready.wait(timeout=_RESPONSE_TIMEOUT)

        if task.response is None:
            return {
                "id": task_id,
                "status": {"state": "working"},
                "artifacts": [
                    {
                        "parts": [
                            {
                                "type": "text",
                                "text": "(processing — poll with tasks/get)",
                            }
                        ],
                        "index": 0,
                    }
                ],
            }

        filtered = filter_outbound(task.response)
        audit.log(
            "task_completed", {"task_id": task_id, "response_length": len(filtered)}
        )

        return {
            "id": task_id,
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"type": "text", "text": filtered}], "index": 0}],
        }


class A2AServer(ThreadingHTTPServer):
    """Threaded HTTP server with A2A configuration.

    Each request runs in its own thread so tasks/send can block waiting
    for agent response without starving health checks and agent card requests.
    """

    daemon_threads = True

    def __init__(self, host: str, port: int):
        self.agent_name = os.getenv("A2A_AGENT_NAME", "hermes-agent")
        self.agent_description = os.getenv(
            "A2A_AGENT_DESCRIPTION", "A self-improving AI agent powered by Hermes"
        )
        self.auth_token = os.getenv("A2A_AUTH_TOKEN", "")
        self.limiter = RateLimiter(
            max_requests=_RATE_LIMIT_MAX_REQUESTS,
            window_seconds=_RATE_LIMIT_WINDOW_SECONDS,
        )
        super().__init__((host, port), A2ARequestHandler)

    def build_agent_card(self) -> dict:
        host, port = cast(tuple[str, int], self.server_address)
        public_url = os.getenv("A2A_PUBLIC_URL") or os.getenv("HERMES_A2A_PUBLIC_URL") or f"http://{host}:{port}"
        return {
            "name": self.agent_name,
            "description": self.agent_description,
            "url": public_url,
            "version": HERMES_VERSION,
            "protocol": "a2a",
            "protocolVersion": "1.0",
            "preferredTransport": "JSONRPC",
            "supportedInterfaces": [
                {
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                    "url": public_url,
                }
            ],
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "multiTurn": True,
                "structuredMetadata": True,
            },
            "skills": [
                {
                    "id": "general",
                    "name": "General Assistant",
                    "description": "General-purpose AI assistant with tool use, web search, and more",
                }
            ],
            "authentication": {
                "schemes": ["bearer"] if self.auth_token else [],
            },
        }


def _task_to_v1(task: dict) -> dict:
    converted = {
        "id": task.get("id", ""),
        "status": {"state": _v1_task_state(task.get("status", {}).get("state", "unknown"))},
    }
    artifacts = []
    for artifact in task.get("artifacts", []) or []:
        parts = []
        for part in artifact.get("parts", []) or []:
            if "text" in part:
                parts.append({"text": part.get("text", "")})
        artifacts.append({"artifactId": str(artifact.get("index", len(artifacts))), "parts": parts})
    if artifacts:
        converted["artifacts"] = artifacts
    return converted


def _v1_task_state(state: str) -> str:
    return f"TASK_STATE_{state.upper().replace('-', '_')}"
