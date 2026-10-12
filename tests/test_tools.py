import asyncio
import json

import pytest

from app import tools


@pytest.fixture(autouse=True)
def _no_faults():
    tools.clear_faults()
    yield
    tools.clear_faults()


def call(handlers, name, args):
    return json.loads(asyncio.run(handlers[name](args)))


def test_owner_can_read_their_incident_and_policy():
    trace = []
    handlers = tools.build_tool_handlers("oncall-priya", trace)
    result = call(handlers, "get_incident", {"incident_id": "inc-4821"})
    assert result["ok"] is True
    assert "owner_key" not in result  # internal field never reaches the model
    assert result["policy"]["items"]["database_failover"]["allowed"] is False
    assert trace[0]["tool"] == "get_incident" and trace[0]["ok"] is True


def test_runbook_marks_steps_that_need_approval():
    handlers = tools.build_tool_handlers("oncall-priya", [])
    steps = call(handlers, "get_runbook", {"incident_id": "INC-4821"})["steps"]
    gated = [s["step_id"] for s in steps if s["needs_approval"]]
    assert gated == ["checkout-db-failover"]


def test_staging_runbook_points_at_staging():
    handlers = tools.build_tool_handlers("qa-dana", [])
    steps = call(handlers, "get_runbook", {"incident_id": "INC-4820"})["steps"]
    assert all("-n prod" not in s["command"] for s in steps)
    assert any("-n staging" in s["command"] for s in steps)


def test_someone_elses_incident_looks_exactly_like_a_missing_one():
    handlers = tools.build_tool_handlers("oncall-sam", [])
    other = call(handlers, "get_incident", {"incident_id": "INC-4821"})  # Priya's
    missing = call(handlers, "get_incident", {"incident_id": "INC-0000"})
    assert other == missing == tools.NOT_FOUND
    assert call(handlers, "get_runbook", {"incident_id": "INC-4821"}) == tools.NOT_FOUND


def test_bad_arguments_return_a_structured_error():
    handlers = tools.build_tool_handlers("qa-dana", [])
    assert call(handlers, "get_incident", {})["error"] == "INVALID_ARGUMENTS"
    assert call(handlers, "get_incident", "not-a-dict")["error"] == "INVALID_ARGUMENTS"


def test_one_transient_failure_is_retried():
    trace = []
    tools.inject_transient_failures("get_runbook", 1)
    handlers = tools.build_tool_handlers("qa-dana", trace)
    assert call(handlers, "get_runbook", {"incident_id": "INC-4820"})["ok"] is True
    assert trace[0]["attempts"] == 2


def test_persistent_failure_gives_up_after_the_retry_budget():
    trace = []
    tools.inject_transient_failures("get_runbook", 5)
    handlers = tools.build_tool_handlers("qa-dana", trace)
    result = call(handlers, "get_runbook", {"incident_id": "INC-4820"})
    assert result["error"] == "TOOL_UNAVAILABLE"
    assert trace[0]["attempts"] == tools.MAX_ATTEMPTS


def test_tool_definitions_match_the_files_for_launchdarkly():
    defs = tools.load_tool_definitions()
    assert set(defs) == {"get_incident", "get_runbook"}
    for d in defs.values():
        assert d["type"] == "function"
        assert d["parameters"]["required"] == ["incident_id"]
