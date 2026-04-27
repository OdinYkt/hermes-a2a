"""A2A tool schemas — what the LLM sees."""

A2A_DISCOVER = {
    "name": "a2a_discover",
    "description": (
        "Discover a remote A2A agent by fetching its Agent Card. "
        "Returns the agent's name, description, capabilities, and supported skills. "
        "Use this before calling an agent to understand what it can do. "
        "Provide either 'url' or 'name' (at least one is required)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Base URL of the remote agent (e.g. http://agent:8081)",
            },
            "name": {
                "type": "string",
                "description": "Name of a configured agent from ~/.hermes/config.yaml",
            },
        },
    },
}

A2A_CALL = {
    "name": "a2a_call",
    "description": (
        "Send a message/task to a remote A2A agent and get its response. "
        "Use a2a_discover first to learn what the agent can do."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Base URL of the remote agent",
            },
            "name": {
                "type": "string",
                "description": "Name of a configured agent (alternative to url)",
            },
            "message": {
                "type": "string",
                "description": "The message or task to send to the remote agent",
            },
            "task_id": {
                "type": "string",
                "description": "Optional task ID for continuing an existing conversation",
            },
            "reply_to_task_id": {
                "type": "string",
                "description": "Task ID this message is replying to (for multi-turn threading)",
            },
            "intent": {
                "type": "string",
                "enum": ["action_request", "review", "consultation", "notification", "instruction"],
                "description": "What kind of message this is",
            },
            "expected_action": {
                "type": "string",
                "enum": ["reply", "forward", "acknowledge"],
                "description": "What you expect the remote agent to do",
            },
        },
        "required": ["message"],
    },
}

A2A_LIST = {
    "name": "a2a_list",
    "description": (
        "List all configured remote A2A agents from ~/.hermes/config.yaml. "
        "Shows agent names, URLs, and descriptions."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

A2A_CALL_ASYNC = {
    "name": "a2a_call_async",
    "description": (
        "Submit a long-running task to a remote agent and return immediately "
        "with the task_id. The peer accepts the task, you continue with other "
        "work, and the peer delivers the final result via a2a_callback into "
        "your A2A inbox (which becomes the next [A2A inbound] wake event with "
        "metadata.kind='callback-result' and metadata.correlation_id=<task_id>). "
        "Use this for work that takes more than a few seconds (code edits, "
        "tests, multi-step delegations). For quick probes use a2a_call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Configured peer name"},
            "url": {"type": "string", "description": "Override URL (rare)"},
            "message": {"type": "string", "description": "Task description for the peer"},
            "task_id": {"type": "string", "description": "Optional caller-chosen task_id (default: auto-uuid)"},
            "intent": {"type": "string", "enum": ["action_request", "review", "instruction"], "description": "Intent hint for the peer"},
        },
        "required": ["message"],
    },
}

A2A_GET_TASK = {
    "name": "a2a_get_task",
    "description": (
        "Poll the current status of a previously-submitted async task. "
        "Returns state (submitted/working/completed/failed) and any partial/final "
        "response text. Use this to check progress when you cannot or do not "
        "want to wait for the callback."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Peer name"},
            "url": {"type": "string"},
            "task_id": {"type": "string", "description": "task_id returned by a2a_call_async"},
        },
        "required": ["task_id"],
    },
}

A2A_CALLBACK = {
    "name": "a2a_callback",
    "description": (
        "Deliver a result back to the agent that submitted an async task. Sets "
        "metadata.kind='callback-result' (or 'callback-error') and "
        "metadata.correlation_id to the original task_id so the receiver can "
        "thread it to the original conversation. Use when you finished work "
        "that arrived via an async submit."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Original requester peer name"},
            "url": {"type": "string"},
            "message": {"type": "string", "description": "The result text"},
            "correlation_id": {
                "type": "string",
                "description": "Original task_id you are responding to",
            },
            "kind": {
                "type": "string",
                "enum": ["callback-result", "callback-error"],
                "description": "Outcome kind (default callback-result)",
            },
        },
        "required": ["message", "correlation_id"],
    },
}
