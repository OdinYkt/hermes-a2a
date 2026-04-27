from __future__ import annotations

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
        handler = object.__new__(A2ARequestHandler)
        handler.server = server
        handler.client_address = ("172.18.0.1", 12345)

        assert handler._check_auth() is False
    finally:
        server.server_close()


def test_default_empty_auth_rejects_non_loopback_requests(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_A2A_HARNESS_MODE", raising=False)
    server = A2AServer("127.0.0.1", 0)
    try:
        handler = object.__new__(A2ARequestHandler)
        handler.server = server
        handler.client_address = ("172.18.0.1", 12345)

        assert handler._check_auth() is False
    finally:
        server.server_close()


def test_v1_sendmessage_returns_completed_task(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugin.server import _harness_task_result

    result = _harness_task_result("msg-1", "reply with: 17")

    assert result["status"]["state"] == "completed"
    assert result["artifacts"][0]["parts"][0]["text"] == "17"


def test_harness_memory_survives_server_instances(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugin.server import _harness_task_result

    _harness_task_result("task-remember", "remember the number 73")
    result = _harness_task_result("task-recall", "what number did I tell you earlier?")

    assert result["status"]["state"] == "completed"
    assert "73" in result["artifacts"][0]["parts"][0]["text"]


def test_task_to_v1_uses_protobuf_json_shape(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugin.server import _harness_task_result, _task_to_v1

    result = _harness_task_result(
        "msg-1",
        "remember 99. then ask peer 'beta' (url=http://peer.test) the question 'what is N+1'. tell me what beta replied.",
    )

    task = _task_to_v1(result)
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["artifacts"][0]["parts"][0] == {"text": "beta ready"}
