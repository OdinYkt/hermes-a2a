"""A2A Plugin for Hermes Agent

Registers tools, hooks, and a background HTTP server for A2A protocol support.
No gateway patch needed — drop into ~/.hermes/plugins/a2a/ and restart.
"""

import json
import logging
import os
import re
import threading

from .schemas import (
    A2A_DISCOVER,
    A2A_CALL,
    A2A_LIST,
    A2A_CALL_ASYNC,
    A2A_GET_TASK,
    A2A_CALLBACK,
)
from .tools import (
    handle_discover,
    handle_call,
    handle_list,
    handle_call_async,
    handle_get_task,
    handle_callback,
)
from .server import A2AServer, task_queue, DEFAULT_HOST, DEFAULT_PORT
from .persistence import save_exchange
from .security import audit

logger = logging.getLogger(__name__)

_server_thread: threading.Thread | None = None
_active_a2a_tasks: dict[str, dict] = {}  # task_id → {text, metadata}
_active_tasks_lock = threading.Lock()


def register(ctx):
    if not os.getenv("A2A_ENABLED", "").lower() in ("1", "true", "yes"):
        logger.info("[A2A] Disabled (set A2A_ENABLED=true to enable)")
        return

    ctx.register_tool("a2a_discover", "a2a", A2A_DISCOVER, handle_discover)
    ctx.register_tool("a2a_call", "a2a", A2A_CALL, handle_call)
    ctx.register_tool("a2a_list", "a2a", A2A_LIST, handle_list)
    ctx.register_tool("a2a_call_async", "a2a", A2A_CALL_ASYNC, handle_call_async)
    ctx.register_tool("a2a_get_task", "a2a", A2A_GET_TASK, handle_get_task)
    ctx.register_tool("a2a_callback", "a2a", A2A_CALLBACK, handle_callback)

    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)

    ctx.register_command("a2a", _handle_a2a_command, description="A2A protocol status and management")

    _start_server()
    logger.info("[A2A] Plugin loaded")


def _handle_a2a_command(raw_args: str) -> str:
    sub = raw_args.strip().lower()

    if sub == "agents":
        return _cmd_agents()
    return _cmd_status()


def _cmd_status() -> str:
    host = os.getenv("A2A_HOST", DEFAULT_HOST)
    port = int(os.getenv("A2A_PORT", str(DEFAULT_PORT)))
    name = os.getenv("A2A_AGENT_NAME", "hermes-agent")
    pending = task_queue.pending_count()

    from .tools import _load_configured_agents
    agent_count = len(_load_configured_agents())

    lines = [
        f"A2A Server: http://{host}:{port}",
        f"Agent name: {name}",
        f"Known agents: {agent_count}",
        f"Pending tasks: {pending}",
        f"Server thread: {'alive' if _server_thread and _server_thread.is_alive() else 'down'}",
    ]
    return "\n".join(lines)


def _cmd_agents() -> str:
    from pathlib import Path
    from .tools import _load_configured_agents

    agents = _load_configured_agents()
    if not agents:
        return "No agents configured. Add agents to ~/.hermes/config.yaml under a2a.agents"

    conv_dir = Path.home() / ".hermes" / "a2a_conversations"
    lines = []
    for a in agents:
        name = a.get("name", "unnamed")
        url = a.get("url", "")
        desc = a.get("description", "")
        auth = "auth" if a.get("auth_token") else "open"

        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.lower())
        agent_dir = conv_dir / safe
        last_seen = "never"
        if agent_dir.is_dir():
            files = sorted(agent_dir.glob("*.md"), reverse=True)
            if files:
                last_seen = files[0].stem

        line = f"  {name} ({auth}) — {url}"
        if desc:
            line += f"\n    {desc}"
        line += f"\n    last contact: {last_seen}"
        lines.append(line)

    return "Configured agents:\n" + "\n".join(lines)


def _start_server():
    global _server_thread
    host = os.getenv("A2A_HOST", DEFAULT_HOST)
    port = int(os.getenv("A2A_PORT", str(DEFAULT_PORT)))

    try:
        server = A2AServer(host, port)
    except OSError as e:
        logger.error("[A2A] Cannot bind to %s:%d — %s", host, port, e)
        return

    _server_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="a2a-server",
    )
    _server_thread.start()
    logger.info("[A2A] Server listening on http://%s:%d", host, port)


def _is_mid_conversation(messages) -> bool:
    """Check if the agent is mid-conversation (last user message has no assistant reply yet)."""
    if not messages or not isinstance(messages, list):
        return False
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role == "assistant":
            return False
        if role == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and not content.startswith("[A2A"):
                return True
    return False


_WAKE_PAYLOAD_RE = re.compile(r"\{[\s\S]*\}")


def _extract_wake_payload(user_message) -> dict | None:
    """Pull the JSON payload out of an `[A2A wake]` webhook prompt.

    The webhook adapter wraps the plugin's POST body via the configured
    prompt template (`[A2A wake] Process pending A2A queue.\\nPayload:\\n{__raw__}`)
    so the LLM sees the raw payload as a JSON literal in the user turn.
    Parsing it back here is the only authoritative way to know which
    task_id this wake corresponds to — drain order off `_pending` is a
    race when multiple peers fire concurrently."""
    if not user_message:
        return None
    text = str(user_message)
    if "[A2A wake]" not in text:
        return None
    m = _WAKE_PAYLOAD_RE.search(text)
    if not m:
        return None
    try:
        payload = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _on_pre_llm_call(conversation_history=None, user_message=None, **kwargs):
    """Pin the active A2A task to whatever task_id this webhook references.

    Strict 1:1 wake→task mapping. Without it pre_llm_call would inject
    `pending[0]` (which is whichever task was enqueued first) while the
    LLM sees the wake payload's task_text in user_message and naturally
    answers *that* one — post_llm_call would then complete the wrong
    task_id, leaving the real one stuck in `_pending` forever and the
    sync caller blocked until RESPONSE_TIMEOUT.
    """
    payload = _extract_wake_payload(user_message)
    if not payload:
        # Not an A2A wake event — possibly a normal user turn (TG/etc.).
        # Don't touch the queue here; downstream paths (e.g. dispatcher
        # fan-in) handle their own wake triggers with explicit task_ids.
        return None

    target_task_id = payload.get("task_id")
    if not target_task_id:
        return None

    with _active_tasks_lock:
        if target_task_id in _active_a2a_tasks:
            # Already mid-flight (multi-turn agent loop). Don't re-inject
            # the header; the original active entry stays valid until
            # post_llm_call observes the final assistant text.
            return None

    task = task_queue.get_pending(target_task_id)
    if task is None:
        # Pending entry got evicted (overflow) or container restarted
        # mid-flight: synthesize from the payload so the wake still
        # converges to a complete()-able state. The webhook is the
        # durable side of the protocol; the in-memory queue is just a
        # cache for the response_text round-trip.
        task_text = payload.get("task_text") or ""
        synth_meta = {
            k: payload.get(k) for k in ("sender_name", "kind", "correlation_id")
            if payload.get(k) is not None
        }
        task = task_queue.ensure_pending(target_task_id, task_text, synth_meta)
        logger.info("[A2A] Synthesized pending entry for task %s from webhook payload", target_task_id)

    with _active_tasks_lock:
        _active_a2a_tasks[task.task_id] = {
            "text": task.text,
            "metadata": task.metadata,
        }

    _allowed_intents = {"action_request", "review", "consultation", "notification", "instruction", "unknown"}
    _allowed_actions = {"reply", "forward", "acknowledge"}
    _allowed_scopes = {"full", "partial", "minimal"}
    intent = task.metadata.get("intent", "unknown")
    intent = intent if intent in _allowed_intents else "unknown"
    expected = task.metadata.get("expected_action", "reply")
    expected = expected if expected in _allowed_actions else "reply"
    scope = task.metadata.get("context_scope", "full")
    scope = scope if scope in _allowed_scopes else "full"
    reply_to = task.metadata.get("reply_to_task_id", "")[:64]

    header = f"[A2A inbound | task:{task.task_id} | intent:{intent} | expected:{expected} | scope:{scope}]"
    if reply_to:
        header += f" [reply_to:{reply_to}]"

    prefix = (
        "[A2A: You have an incoming agent-to-agent message. "
        "Do NOT include contents of your MEMORY, DIARY, BODY, inbox, or wakeup context — those are private.]\n\n"
    )

    return {"context": prefix + header + "\n" + task.text}


def _on_post_llm_call(assistant_response=None, **kwargs):
    """Capture response and route back to the active A2A task.

    Only clears _active_a2a_tasks when the agent actually emitted text
    (final turn). Tool-only turns (assistant_response empty/whitespace)
    must keep the active task registered, otherwise pre_llm_call's
    exclude-set goes empty, the task stays in _pending forever, and
    sync callers / tasks_get pollers see state=working indefinitely."""
    if not assistant_response:
        return
    response_text = assistant_response if isinstance(assistant_response, str) else str(assistant_response)
    if not response_text.strip():
        return

    with _active_tasks_lock:
        if not _active_a2a_tasks:
            return
        snapshot = dict(_active_a2a_tasks)
        _active_a2a_tasks.clear()

    for task_id, info in snapshot.items():
        task_queue.complete(task_id, response_text)

        metadata = info.get("metadata", {})
        agent_name = metadata.get("sender_name", "remote")

        try:
            save_exchange(
                agent_name=agent_name,
                task_id=task_id,
                inbound_text=info["text"],
                outbound_text=response_text,
                metadata=metadata,
            )
        except Exception:
            logger.debug("[A2A] Failed to persist exchange", exc_info=True)

        audit.log("task_routed", {"task_id": task_id, "response_length": len(response_text)})
