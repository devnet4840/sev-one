"""The judge contract, and the real OpenAI handler wiring with the network call stubbed out."""

import json
import types

import pytest
from launchdarkly_ai_server import create_handler

from app.agent import OperatorLog, TurnEngine, build_handlers
from app.mock_provider import create_mock_handlers
from app.personas import PERSONAS
from tests.conftest import Harness, make_settings

pytestmark = pytest.mark.asyncio
DANA, SAM = PERSONAS["qa-dana"], PERSONAS["oncall-sam"]
Q = "What should I do first?"


def judge_returning(score):
    async def call(config, user_input, tool_handlers, variables, history=None):
        return {"output": json.dumps({"score": score, "reasoning": "test judge"}), "usage": {"input_tokens": 1, "output_tokens": 1}}

    return create_handler(("*", "messages"), call)


@pytest.mark.parametrize("score", [7, 1.5, -0.2, float("inf"), "0.9 (high)", None])
async def test_judge_scores_outside_0_to_1_fail_closed(score):
    h = await Harness(make_settings()).start()
    try:
        h.runtime.local.apply_stage("qa-preview")
        agent_handler = create_mock_handlers(0)[0]  # ("*", "agent")
        engine = TurnEngine(h.settings, h.runtime, [agent_handler, judge_returning(score)], OperatorLog())
        r = await engine.run(DANA, Q)
        assert r["decision"]["reason"] == "judge_unavailable", r["decision"]
        assert r["answer"]["source"] == "standard"
    finally:
        await h.stop()


async def test_judge_score_inside_range_is_used():
    h = await Harness(make_settings()).start()
    try:
        h.runtime.local.apply_stage("qa-preview")
        engine = TurnEngine(h.settings, h.runtime, [create_mock_handlers(0)[0], judge_returning(0.8)], OperatorLog())
        r = await engine.run(DANA, Q)
        assert r["decision"]["reason"] == "released"
        assert r["judge"]["score"] == 0.8
    finally:
        await h.stop()


# ---------------------------------------------------------------------------
# Real OpenAI handler (launchdarkly-ai-openai-agents) with agents.Runner.run stubbed
# ---------------------------------------------------------------------------

def reply(explanation, rec="checkout-rollback", claims=None):
    claims = [{"action": "rollback_deploy", "allowed": True}, {"action": "database_failover", "allowed": False}] if claims is None else claims
    return json.dumps({"recommended_step_id": rec, "action_claims": claims, "explanation": explanation})


GOOD_TEXT = (
    "Dana, errors jumped to 18.2% at 2:03 AM, five minutes after v2.14.0 went out at 1:58 AM. "
    "Roll back with `kubectl rollout undo deploy/checkout-api -n staging`. A database failover needs the "
    "incident commander's approval."
)
GOOD = reply(GOOD_TEXT)
FAST_FIX = reply(
    "Sam, I've already restarted the search pods. Flush the query cache now with "
    "`cachectl flush query-cache --all` and you'll be back in minutes.",
    rec="search-restart",
    claims=[{"action": "restart_pods", "allowed": True}],
)


@pytest.fixture
def stub_runner(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    import agents

    calls = []
    answers = {"text": GOOD}

    async def fake_run(agent, prompt, hooks=None, **kwargs):
        instructions = agent.instructions or ""
        is_judge = "strict incident-response reviewer" in instructions
        calls.append({"judge": is_judge, "model": agent.model, "tools": [t.name for t in agent.tools], "instructions": instructions})
        if is_judge:
            verdict = {"score": 0.1 if "already restarted" in instructions else 0.95, "reasoning": "stub judge"}
            output = json.dumps(verdict)
        else:
            ref = "INC-4820" if "Dana" in instructions else "INC-4822"
            for tool in agent.tools:  # the Agents SDK calls tools with a JSON argument string
                if answers.get("only_incident") and tool.name != "get_incident":
                    continue
                await tool.on_invoke_tool(None, json.dumps({"incident_id": ref}))
            output = answers["text"]
        usage = types.SimpleNamespace(input_tokens=100, output_tokens=50, input_tokens_details=None, output_tokens_details=None, requests=1, total_tokens=150)
        return types.SimpleNamespace(final_output=output, context_wrapper=types.SimpleNamespace(usage=usage))

    monkeypatch.setattr(agents.Runner, "run", staticmethod(fake_run))
    return calls, answers


async def test_openai_handler_path_fills_variables_calls_tools_and_runs_the_judge(stub_runner):
    calls, _ = stub_runner
    s = make_settings(llm_mode="openai")
    h = await Harness(s).start()
    try:
        h.runtime.local.apply_stage("qa-preview")
        engine = TurnEngine(s, h.runtime, build_handlers(s), OperatorLog())
        r = await engine.run(DANA, Q)
    finally:
        await h.stop()
    assert r["decision"]["reason"] == "released", r["decision"]
    agent_call, judge_call = calls[0], calls[1]
    assert agent_call["model"] == s.local_model_small  # real model name, not a mock label
    assert sorted(agent_call["tools"]) == ["get_incident", "get_runbook"]
    assert "{{" not in agent_call["instructions"] and "INC-4820" in agent_call["instructions"]
    assert [t["tool"] for t in r["tool_trace"]] == ["get_incident", "get_runbook"]
    assert judge_call["judge"] and "Tool calls made by the assistant" in judge_call["instructions"]


async def test_openai_fast_fix_wording_is_blocked_even_with_the_judge_off(stub_runner):
    _, answers = stub_runner
    answers["text"] = FAST_FIX
    s = make_settings(llm_mode="openai", judge_config_key="")
    h = await Harness(s).start()
    try:
        h.runtime.local.apply_stage("fast-fix")
        r = await TurnEngine(s, h.runtime, build_handlers(s), OperatorLog()).run(SAM, Q)
    finally:
        await h.stop()
    assert r["decision"]["reason"] == "blocked_by_checks"
    failed = {c["name"] for c in r["checks"] if not c["passed"]}
    assert {"no_action_claims", "no_unapproved_actions"} <= failed
    assert r["answer"]["source"] == "standard"


async def test_an_answer_that_only_needed_the_incident_lookup_is_released(stub_runner):
    _, answers = stub_runner
    answers["only_incident"] = True
    answers["text"] = reply(
        "Dana, the errors started at 2:03 AM, five minutes after v2.14.0 went out at 1:58 AM, so the deploy is the likely cause.",
        rec=None,
        claims=[],
    )
    s = make_settings(llm_mode="openai")
    h = await Harness(s).start()
    try:
        h.runtime.local.apply_stage("qa-preview")
        r = await TurnEngine(s, h.runtime, build_handlers(s), OperatorLog()).run(DANA, "Was it the last deploy?")
    finally:
        await h.stop()
    assert [t["tool"] for t in r["tool_trace"]] == ["get_incident"]
    assert r["decision"]["reason"] == "released", r["decision"]


async def test_a_prose_reply_without_the_fields_is_blocked(stub_runner):
    _, answers = stub_runner
    answers["text"] = GOOD_TEXT  # correct facts, but not the JSON shape the instructions ask for
    s = make_settings(llm_mode="openai")
    h = await Harness(s).start()
    try:
        h.runtime.local.apply_stage("qa-preview")
        r = await TurnEngine(s, h.runtime, build_handlers(s), OperatorLog()).run(DANA, Q)
    finally:
        await h.stop()
    assert r["decision"]["reason"] == "blocked_by_checks"
    assert r["checks"][0]["name"] == "structured_reply" and not r["checks"][0]["passed"]


@pytest.mark.parametrize(
    "rec, claims, failing",
    [
        ("checkout-hotfix", None, "recommended_step"),  # not a step in this runbook
        ("checkout-db-failover", None, "recommended_step"),  # real step, but it needs approval
        ("checkout-rollback", [{"action": "database_failover", "allowed": True}], "action_claims"),
        ("checkout-rollback", [{"action": "reboot_datacenter", "allowed": True}], "action_claims"),
    ],
)
async def test_field_checks_catch_wrong_ids_and_claims(stub_runner, rec, claims, failing):
    _, answers = stub_runner
    answers["text"] = reply(GOOD_TEXT, rec=rec, claims=claims)
    s = make_settings(llm_mode="openai", judge_config_key="")
    h = await Harness(s).start()
    try:
        h.runtime.local.apply_stage("qa-preview")
        r = await TurnEngine(s, h.runtime, build_handlers(s), OperatorLog()).run(DANA, Q)
    finally:
        await h.stop()
    assert r["decision"]["reason"] == "blocked_by_checks"
    assert [c["name"] for c in r["checks"] if not c["passed"]] == [failing]


async def test_released_answer_shows_the_explanation_and_the_validated_step(stub_runner):
    s = make_settings(llm_mode="openai")
    h = await Harness(s).start()
    try:
        h.runtime.local.apply_stage("qa-preview")
        r = await TurnEngine(s, h.runtime, build_handlers(s), OperatorLog()).run(DANA, Q)
    finally:
        await h.stop()
    assert r["answer"]["text"] == GOOD_TEXT  # the engineer never sees raw JSON
    assert r["recommended_step_id"] == "checkout-rollback"
