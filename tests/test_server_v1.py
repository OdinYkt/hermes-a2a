from __future__ import annotations

from plugin.server import A2AServer


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


def test_harness_mode_allows_non_loopback_unauthenticated_requests(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_A2A_HARNESS_MODE", "true")
    server = A2AServer("127.0.0.1", 0)
    try:
        handler = server.RequestHandlerClass.__new__(server.RequestHandlerClass)
        handler.server = server
        handler.client_address = ("172.18.0.1", 12345)

        assert handler._check_auth() is True
    finally:
        server.server_close()


def test_default_empty_auth_rejects_non_loopback_requests(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_A2A_HARNESS_MODE", raising=False)
    server = A2AServer("127.0.0.1", 0)
    try:
        handler = server.RequestHandlerClass.__new__(server.RequestHandlerClass)
        handler.server = server
        handler.client_address = ("172.18.0.1", 12345)

        assert handler._check_auth() is False
    finally:
        server.server_close()


def test_v1_sendmessage_returns_completed_task(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_A2A_HARNESS_MODE", "true")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    server = A2AServer("127.0.0.1", 0)
    try:
        handler = server.RequestHandlerClass.__new__(server.RequestHandlerClass)
        handler.server = server
        handler.client_address = ("127.0.0.1", 12345)

        result = handler._handle_v1_message_send(
            {
                "message": {
                    "messageId": "msg-1",
                    "parts": [{"text": "reply with: 17"}],
                }
            }
        )
    finally:
        server.server_close()

    assert result["status"]["state"] == "completed"
    assert result["artifacts"][0]["parts"][0]["text"] == "17"


def test_harness_memory_survives_server_instances(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_A2A_HARNESS_MODE", "true")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    first = A2AServer("127.0.0.1", 0)
    try:
        handler = first.RequestHandlerClass.__new__(first.RequestHandlerClass)
        handler.server = first
        handler.client_address = ("127.0.0.1", 12345)
        handler._handle_task_send(
            {
                "id": "task-remember",
                "message": {"parts": [{"type": "text", "text": "remember the number 73"}]},
            }
        )
    finally:
        first.server_close()

    second = A2AServer("127.0.0.1", 0)
    try:
        handler = second.RequestHandlerClass.__new__(second.RequestHandlerClass)
        handler.server = second
        handler.client_address = ("127.0.0.1", 12345)
        result = handler._handle_task_send(
            {
                "id": "task-recall",
                "message": {"parts": [{"type": "text", "text": "what number did I tell you earlier?"}]},
            }
        )
    finally:
        second.server_close()

    assert result["status"]["state"] == "completed"
    assert "73" in result["artifacts"][0]["parts"][0]["text"]


def test_task_to_v1_uses_protobuf_json_shape(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_A2A_HARNESS_MODE", "true")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    server = A2AServer("127.0.0.1", 0)
    try:
        handler = server.RequestHandlerClass.__new__(server.RequestHandlerClass)
        handler.server = server
        handler.client_address = ("127.0.0.1", 12345)
        result = handler._handle_v1_message_send(
            {
                "message": {
                    "messageId": "msg-1",
                    "parts": [{"text": "remember 99. then ask peer 'beta' (url=http://peer.test) the question 'what is N+1'. tell me what beta replied."}],
                }
            }
        )
    finally:
        server.server_close()

    from plugin.server import _task_to_v1

    task = _task_to_v1(result)
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["artifacts"][0]["parts"][0] == {"text": "beta ready"}
