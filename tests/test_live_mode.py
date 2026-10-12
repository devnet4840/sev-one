"""Live mode without network access: LaunchDarkly is unreachable, so this checks the app's own
behavior around it (no local controls, missing trigger URLs, defaults) rather than LaunchDarkly."""

import pytest
from fastapi.testclient import TestClient

from app import ld
from app.main import create_app
from tests.conftest import make_settings

OP = {"X-Sev-One": "1", "X-Operator-Token": "t0ken"}


@pytest.fixture
def live_client(monkeypatch):
    monkeypatch.setattr(ld, "LIVE_START_WAIT_SECONDS", 0.2)
    settings = make_settings(ld_mode="live", ld_sdk_key="sdk-not-a-real-key", operator_token="t0ken")
    with TestClient(create_app(settings)) as client:
        yield client


def test_local_controls_do_not_exist_in_live_mode(live_client):
    r = live_client.post("/api/local/stage", json={"stage": "customers"}, headers=OP)
    assert r.status_code == 404
    assert "LaunchDarkly" in r.json()["detail"]


def test_rollback_without_a_trigger_url_explains_what_to_set(live_client):
    boot = live_client.get("/api/operator/bootstrap", headers=OP).json()
    assert boot["trigger"] == {"off_configured": False, "on_configured": False}
    outcome = live_client.post("/api/operator/rollback", headers=OP).json()
    assert outcome["ok"] is False
    assert "LD_TRIGGER_OFF_URL" in outcome["error"]


def test_a_malformed_trigger_url_is_reported_without_echoing_it(monkeypatch):
    monkeypatch.setattr(ld, "LIVE_START_WAIT_SECONDS", 0.2)
    secret = "http://exa mple.invalid/webhook/triggers/SECRET-PART"
    settings = make_settings(ld_mode="live", ld_sdk_key="sdk-not-a-real-key", operator_token="t0ken", trigger_off_url=secret)
    with TestClient(create_app(settings)) as client:
        outcome = client.post("/api/operator/rollback", headers=OP).json()
    assert outcome["ok"] is False
    assert "SECRET-PART" not in str(outcome)


def test_unreachable_launchdarkly_means_standard_answers(live_client):
    r = live_client.post("/api/chat", json={"persona": "qa-dana", "question": "What should I do first?"}, headers=OP).json()
    assert r["answer"]["source"] == "standard"  # the flag's app default is off
    assert len(r["steps"]) == 4


def test_a_successful_trigger_call_never_writes_the_url_to_the_log(monkeypatch, caplog):
    import http.server
    import logging
    import socket
    import threading

    class Ok(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 (http.server naming)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = http.server.HTTPServer(("127.0.0.1", port), Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    monkeypatch.setattr(ld, "LIVE_START_WAIT_SECONDS", 0.2)
    secret_url = f"http://127.0.0.1:{port}/webhook/triggers/SECRET-PART"
    settings = make_settings(ld_mode="live", ld_sdk_key="sdk-not-a-real-key", operator_token="t0ken", trigger_off_url=secret_url)
    # Capture INFO everywhere, which is where httpx writes request URLs. Without the app's
    # logging fix this test fails; with it, httpx stays at WARNING and writes nothing.
    caplog.set_level(logging.INFO)
    try:
        with TestClient(create_app(settings)) as client:
            outcome = client.post("/api/operator/rollback", headers=OP).json()
    finally:
        server.shutdown()
    assert outcome["ok"] is True and outcome["http_status"] == 200
    assert "SECRET-PART" not in caplog.text
