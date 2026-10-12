"""Runs the real web server and checks the no-reload path end to end:

operator stream open -> rollback -> SDK listener (on another thread) -> event loop ->
server-sent event with the new state -> the engineer's next answer is the standard message.
"""

import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.main import create_app
from tests.conftest import make_settings

OP = {"X-Sev-One": "1"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(settings):
    port = _free_port()
    app = create_app(settings)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=1))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            if httpx.get(f"{base}/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    return app, srv, thread, base


@pytest.fixture
def server():
    app, srv, thread, base = _serve(make_settings(mock_latency_ms=0))
    yield app, base
    srv.should_exit = True
    thread.join(timeout=10)


def _read_events(lines, until, timeout=5.0):
    """Collect SSE events from a line iterator until until(events) is true."""
    events, current = [], {}
    deadline = time.time() + timeout
    for line in lines:
        if time.time() > deadline:
            break
        if line.startswith("event: "):
            current["event"] = line[7:]
        elif line.startswith("data: "):
            current["data"] = json.loads(line[6:])
        elif line == "" and current:
            events.append(current)
            current = {}
            if until(events):
                break
    return events


def _ask(base, persona, question="What should I do first?"):
    r = httpx.post(f"{base}/api/chat", json={"persona": persona, "question": question}, headers=OP, timeout=20)
    r.raise_for_status()
    return r.json()


def test_rollback_fired_from_another_thread_reaches_the_open_console_without_a_reload(server):
    app, base = server
    httpx.post(f"{base}/api/local/stage", json={"stage": "qa-preview"}, headers=OP).raise_for_status()
    assert _ask(base, "qa-dana")["answer"]["source"] == "ai"

    with httpx.stream("GET", f"{base}/api/operator/events", params={"persona": "qa-dana"}, timeout=10) as stream:
        lines = stream.iter_lines()
        snapshot = _read_events(lines, until=lambda ev: any(e["event"] == "snapshot" for e in ev))
        assert snapshot[-1]["data"]["state"]["feature_on"] is True

        # Fire the change from a separate thread, the way the SDK's streaming connection does.
        local = app.state.runtime.local
        worker = threading.Thread(target=local.set_feature_targeting, args=(False,), name="fake-sdk-stream")
        worker.start()
        worker.join()
        events = _read_events(lines, until=lambda ev: any(e["event"] == "state" for e in ev))

    states = [e["data"] for e in events if e["event"] == "state"]
    assert states and states[-1]["feature_on"] is False
    assert any(e["event"] == "timeline" and e["data"]["kind"] == "flag_changed" for e in events)

    after = _ask(base, "qa-dana")
    assert after["answer"]["source"] == "standard"
    assert len(after["steps"]) == 4


def test_rollback_button_goes_through_the_trigger_path(server):
    _, base = server
    httpx.post(f"{base}/api/local/stage", json={"stage": "on-call"}, headers=OP).raise_for_status()
    with httpx.stream("GET", f"{base}/api/operator/events", params={"persona": "oncall-sam"}, timeout=10) as stream:
        lines = stream.iter_lines()
        _read_events(lines, until=lambda ev: any(e["event"] == "snapshot" for e in ev))
        outcome = httpx.post(f"{base}/api/operator/rollback", headers=OP).json()
        assert outcome["ok"] is True
        events = _read_events(lines, until=lambda ev: any(e["event"] == "state" for e in ev))
    kinds = [e["data"]["kind"] for e in events if e["event"] == "timeline"]
    assert "trigger_fired" in kinds and "flag_changed" in kinds
    assert _ask(base, "oncall-sam")["answer"]["source"] == "standard"


def test_engineer_gets_only_the_answer_and_the_console_gets_the_details(server):
    _, base = server
    httpx.post(f"{base}/api/local/stage", json={"stage": "fast-fix"}, headers=OP).raise_for_status()
    with httpx.stream("GET", f"{base}/api/operator/events", params={"persona": "qa-dana"}, timeout=10) as stream:
        lines = stream.iter_lines()
        _read_events(lines, until=lambda ev: any(e["event"] == "snapshot" for e in ev))
        engineer = _ask(base, "oncall-sam", "Can I flush the cache?")
        events = _read_events(lines, until=lambda ev: any(e["event"] == "operator" for e in ev))

    # The engineer response carries no blocked text, checks, judge, or tool calls.
    assert set(engineer) == {"turn_id", "persona_key", "answer", "incident", "steps", "recommended_step_id"}
    assert engineer["answer"]["source"] == "standard"
    assert "already restarted" not in json.dumps(engineer).lower()

    # The operator channel has the full story.
    turn = [e["data"] for e in events if e["event"] == "turn"][-1]
    assert turn["decision"]["reason"] == "blocked_by_checks"
    assert "already restarted" in turn["candidate_text"].lower()
    assert turn["judge"]["ran"] is True and turn["judge"]["score"] < 0.7  # LaunchDarkly gets a score too
    operator = [e["data"] for e in events if e["event"] == "operator"][-1]
    assert operator["blocked_count"] == 1
    assert operator["incidents"][0]["variation_key"] == "fast-fix"


def test_operator_changes_need_the_page_header(server):
    _, base = server
    # A form posted from another website cannot add a custom header.
    assert httpx.post(f"{base}/api/operator/rollback").status_code == 403
    assert httpx.post(f"{base}/api/local/stage", json={"stage": "baseline"}).status_code == 403
    assert httpx.post(f"{base}/api/local/stage", json={"stage": "nope"}, headers=OP).status_code == 400
    assert httpx.get(f"{base}/api/operator/events", params={"persona": "nobody"}).status_code == 404
    assert httpx.post(f"{base}/api/chat", json={"persona": "qa-dana", "question": ""}, headers=OP).status_code == 422


def test_operator_token_mode():
    app, srv, thread, base = _serve(make_settings(operator_token="s3cret", mock_latency_ms=0))
    try:
        assert httpx.get(f"{base}/api/operator/bootstrap").status_code == 401
        assert httpx.get(f"{base}/api/operator/bootstrap", headers={"X-Operator-Token": "wrong"}).status_code == 401
        assert httpx.get(f"{base}/api/operator/bootstrap", headers={"X-Operator-Token": "s3cret"}).status_code == 200
        with httpx.Client(base_url=base) as client:
            assert client.post("/api/operator/session", json={"token": "wrong"}, headers=OP).status_code == 401
            assert client.post("/api/operator/session", json={"token": "s3cret"}, headers=OP).status_code == 200
            assert client.get("/api/operator/bootstrap").status_code == 200  # cookie set by the session call
        # Engineer endpoints never need the token.
        assert httpx.get(f"{base}/api/incident", params={"persona": "oncall-sam"}).status_code == 200
    finally:
        srv.should_exit = True
        thread.join(timeout=10)


def test_operator_console_is_refused_from_another_machine_without_a_token():
    from fastapi.testclient import TestClient

    with TestClient(create_app(make_settings(mock_latency_ms=0))) as client:  # client host is "testclient"
        assert client.get("/api/operator/bootstrap").status_code == 403
        assert client.get("/api/bootstrap").status_code == 200


def test_operator_console_rejects_a_foreign_host_name_even_from_this_computer(server):
    # DNS rebinding: a website points its own name at 127.0.0.1. The request comes from this
    # computer, but the Host header names the other site, so the console must refuse it.
    _, base = server
    foreign = {"Host": "rebind.attacker.example", **OP}
    assert httpx.get(f"{base}/api/operator/bootstrap", headers=foreign).status_code == 403
    assert httpx.post(f"{base}/api/operator/restore", headers=foreign).status_code == 403
    assert httpx.get(f"{base}/api/operator/bootstrap", headers={"Host": "localhost"}).status_code == 200


def test_armed_fixture_is_blocked_labeled_and_used_once(server):
    _, base = server
    httpx.post(f"{base}/api/local/stage", json={"stage": "on-call"}, headers=OP).raise_for_status()
    assert httpx.post(f"{base}/api/operator/fixture").status_code == 403  # needs the page header
    with httpx.stream("GET", f"{base}/api/operator/events", params={"persona": "oncall-sam"}, timeout=10) as stream:
        lines = stream.iter_lines()
        _read_events(lines, until=lambda ev: any(e["event"] == "snapshot" for e in ev))
        assert httpx.post(f"{base}/api/operator/fixture", headers=OP).json() == {"armed": True}
        engineer = _ask(base, "oncall-sam")
        events = _read_events(lines, until=lambda ev: any(e["event"] == "turn" for e in ev))
    turn = [e["data"] for e in events if e["event"] == "turn"][-1]
    assert engineer["answer"]["source"] == "standard" and engineer["recommended_step_id"] is None
    assert turn["fixture"] is True and turn["config_used"]["variation_key"] == "test-fixture"
    assert turn["decision"]["reason"] == "blocked_by_checks"
    failed = {c["name"] for c in turn["checks"] if not c["passed"]}
    assert failed == {"no_action_claims"}  # the fields were right; the text check caught it
    assert turn["judge"]["ran"] is False and turn["tool_trace"] == []
    # One answer only: the next question goes to the model again and is released.
    assert _ask(base, "oncall-sam")["answer"]["source"] == "ai"
