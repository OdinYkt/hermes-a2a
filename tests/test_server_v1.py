from __future__ import annotations

import json
from io import BytesIO
from http.client import HTTPMessage
from typing import cast

from plugin.server import A2ARequestHandler, A2AServer


def test_agent_card_declares_canonical_a2a_v1_interface() -> None:
    server = A2AServer("127.0.0.1", 0)
    try:
        card = server.build_agent_card()
    finally:
        server.server_close()

    assert card["protocolVersion"] == "1.0"
    assert {
        "protocolBinding": "JSONRPC",
        "protocolVersion": "1.0",
        "url": f"http://127.0.0.1:{server.server_address[1]}",
    } in card["supportedInterfaces"]


def test_legacy_harness_env_does_not_bypass_non_loopback_auth(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_A2A_HARNESS_MODE", "true")
    server = A2AServer("127.0.0.1", 0)
    try:
        handler = cast(A2ARequestHandler, object.__new__(A2ARequestHandler))
        handler.server = server
        handler.client_address = ("172.18.0.1", 12345)

        assert handler._check_auth() is False
    finally:
        server.server_close()


def test_default_empty_auth_rejects_non_loopback_requests(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_A2A_HARNESS_MODE", raising=False)
    server = A2AServer("127.0.0.1", 0)
    try:
        handler = cast(A2ARequestHandler, object.__new__(A2ARequestHandler))
        handler.server = server
        handler.client_address = ("172.18.0.1", 12345)

        assert handler._check_auth() is False
    finally:
        server.server_close()


def test_task_to_v1_uses_protobuf_json_shape(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugin.server import _task_to_v1

    result = {
        "id": "msg-1",
        "status": {"state": "completed"},
        "artifacts": [{"parts": [{"type": "text", "text": "beta ready"}], "index": 0}],
    }

    task = _task_to_v1(result)
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["artifacts"][0]["parts"][0] == {"text": "beta ready"}


def test_server_module_has_no_harness_shortcut_helpers() -> None:
    import plugin.server as server_module

    assert not hasattr(server_module, "_harness_task_result")
    assert not hasattr(server_module, "_harness_response_text")
    assert not hasattr(server_module, "_harness_memory_path")


def test_unsupported_a2a_version_rejected_before_task_enqueue(monkeypatch) -> None:
    monkeypatch.setenv("A2A_AUTH_TOKEN", "token")
    server = A2AServer("127.0.0.1", 0)
    try:
        handler = cast(A2ARequestHandler, object.__new__(A2ARequestHandler))
        handler.server = server
        handler.client_address = ("172.18.0.1", 12345)
        headers = HTTPMessage()
        headers["Authorization"] = "Bearer token"
        headers["A2A-Version"] = "9.9"
        headers["Content-Length"] = "2"
        handler.headers = headers
        handler.rfile = BytesIO(b"{}")
        sent: dict[str, object] = {}

        def capture(data: dict, status: int = 200) -> None:
            sent["data"] = data
            sent["status"] = status

        handler._send_json = capture

        handler.do_POST()

        assert sent["status"] in {400, 406}
        assert "version" in json.dumps(sent["data"]).lower()
    finally:
        server.server_close()


def test_webhook_wake_payload_parser_accepts_template_fields() -> None:
    from plugin import _extract_wake_payload

    payload = _extract_wake_payload(
        "[A2A wake] Process pending A2A queue.\n"
        "task_id=task-123\n"
        "task_text=reply with: 17\n"
        "sender_name=mock-peer\n"
        "kind=request\n"
    )

    assert payload == {
        "task_id": "task-123",
        "task_text": "reply with: 17",
        "sender_name": "mock-peer",
        "kind": "request",
    }
