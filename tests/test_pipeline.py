"""End-to-end turns through the real LaunchDarkly SDKs, using local test flags and the mock model."""

import asyncio

import pytest

from app import tools
from app.personas import PERSONAS
from tests.conftest import Harness, make_settings

DANA, SAM, PRIYA = PERSONAS["qa-dana"], PERSONAS["oncall-sam"], PERSONAS["oncall-priya"]
Q = "I just got paged. What should I do first?"

pytestmark = pytest.mark.asyncio


async def test_baseline_stage_everyone_gets_the_standard_message(harness):
    for persona in (DANA, SAM, PRIYA):
        r = await harness.engine.run(persona, Q)
        assert r["answer"]["source"] == "standard"
        assert r["decision"]["reason"] == "feature_off"
        assert len(r["steps"]) == 4  # runbook steps still render from the app's own lookup


async def test_qa_preview_releases_candidate_answer_to_qa_only(harness):
    harness.runtime.local.apply_stage("qa-preview")
    r = await harness.engine.run(DANA, Q)
    assert r["decision"]["reason"] == "released", r["decision"]
    assert r["answer"]["source"] == "ai"
    assert r["config_used"]["variation_key"] == "candidate"
    assert [t["tool"] for t in r["tool_trace"]] == ["get_incident", "get_runbook"]
    assert all(t["arguments"]["incident_id"] == "INC-4820" for t in r["tool_trace"])
    assert r["recommended_step_id"] == "checkout-rollback"
    assert all(c["passed"] for c in r["checks"])
    assert r["judge"]["passed"] is True and r["judge"]["score"] >= 0.7
    assert (await harness.engine.run(SAM, Q))["decision"]["reason"] == "feature_off"


async def test_on_call_stage_routes_critical_services_to_the_larger_model(harness):
    harness.runtime.local.apply_stage("on-call")
    priya = await harness.engine.run(PRIYA, Q)
    sam = await harness.engine.run(SAM, Q)
    dana = await harness.engine.run(DANA, Q)
    assert priya["config_used"]["variation_key"] == "premium-model"
    assert priya["config_used"]["model"] == "mock-large"
    assert sam["config_used"]["variation_key"] == "baseline"
    assert dana["config_used"]["variation_key"] == "candidate"  # individual target wins
    assert all(r["answer"]["source"] == "ai" for r in (priya, sam, dana))


async def test_fast_fix_prompt_is_blocked_and_engineer_still_gets_the_runbook(harness):
    harness.runtime.local.apply_stage("fast-fix")
    r = await harness.engine.run(SAM, Q)
    assert r["config_used"]["variation_key"] == "fast-fix"
    assert r["decision"]["reason"] == "blocked_by_checks"
    assert r["answer"]["source"] == "standard"
    failed = {c["name"] for c in r["checks"] if not c["passed"]}
    assert {"recommended_step", "action_claims", "no_unapproved_actions", "no_action_claims"} <= failed
    assert "flush" in r["candidate_text"].lower()  # operator can see what was blocked
    assert "already restarted" not in r["answer"]["text"].lower()  # the engineer never does
    assert r["recommended_step_id"] is None
    assert len(r["steps"]) == 4
    assert harness.oplog.blocked_count == 1
    # The judge still runs, so LaunchDarkly records a low quality score for this variation.
    assert r["judge"]["ran"] is True and r["judge"]["score"] < 0.7
    # Critical services are on a different variation and are unaffected.
    assert (await harness.engine.run(PRIYA, Q))["decision"]["reason"] == "released"


async def test_gated_action_questions_get_safe_answers(harness):
    harness.runtime.local.apply_stage("on-call")
    for q in ("Should I fail over the database?", "Can I flush the cache?", "Can I silence this alert?", "Was it the last deploy?"):
        for persona in (SAM, PRIYA):
            r = await harness.engine.run(persona, q)
            assert r["decision"]["reason"] == "released", (persona.key, q, r["decision"])


async def test_rollback_turns_ai_guidance_off_for_everyone(harness):
    harness.runtime.local.apply_stage("on-call")
    assert (await harness.engine.run(DANA, Q))["answer"]["source"] == "ai"
    harness.runtime.local.set_feature_targeting(False)  # what the trigger does
    for persona in (DANA, SAM, PRIYA):
        r = await harness.engine.run(persona, Q)
        assert r["decision"]["reason"] == "feature_off"
    harness.runtime.local.set_feature_targeting(True)
    assert (await harness.engine.run(DANA, Q))["answer"]["source"] == "ai"


async def test_rollback_while_an_answer_is_in_flight_is_honored():
    h = await Harness(make_settings(mock_latency_ms=600)).start()
    try:
        h.runtime.local.apply_stage("qa-preview")

        async def flip_soon():
            await asyncio.sleep(0.15)
            h.runtime.local.set_feature_targeting(False)

        r, _ = await asyncio.gather(h.engine.run(DANA, Q), flip_soon())
        assert r["decision"]["reason"] == "rolled_back_during_answer"
        assert r["answer"]["source"] == "standard"
    finally:
        await h.stop()


async def test_low_judge_score_blocks_even_when_checks_pass():
    h = await Harness(make_settings(judge_pass_threshold=0.99)).start()
    try:
        h.runtime.local.apply_stage("qa-preview")
        r = await h.engine.run(DANA, Q)
        assert all(c["passed"] for c in r["checks"])
        assert r["decision"]["reason"] == "blocked_by_judge"
        assert r["answer"]["source"] == "standard"
    finally:
        await h.stop()


async def test_missing_judge_fails_closed():
    h = await Harness(make_settings()).start()
    try:
        h.runtime.local.apply_stage("qa-preview")
        # The flags exist, but the app asks for a judge key that LaunchDarkly does not have.
        h.engine.settings = make_settings(judge_config_key="judge-that-does-not-exist")
        r = await h.engine.run(DANA, Q)
        assert r["decision"]["reason"] == "judge_unavailable"
        assert r["answer"]["source"] == "standard"
    finally:
        await h.stop()


async def test_missing_agent_config_fails_closed():
    h = await Harness(make_settings()).start()
    try:
        h.runtime.local.apply_stage("qa-preview")
        h.engine.settings = make_settings(agent_config_key="config-that-does-not-exist")
        r = await h.engine.run(DANA, Q)
        assert r["decision"]["reason"] == "ai_error"
        assert r["answer"]["source"] == "standard"
    finally:
        await h.stop()


async def test_tool_outage_never_reaches_the_engineer_as_an_ai_answer(harness):
    harness.runtime.local.apply_stage("qa-preview")
    tools.inject_transient_failures("get_runbook", 5)
    try:
        r = await harness.engine.run(DANA, Q)
    finally:
        tools.clear_faults()
    trace = {t["tool"]: t for t in r["tool_trace"]}
    assert trace["get_runbook"]["error"] == "TOOL_UNAVAILABLE"
    assert trace["get_runbook"]["attempts"] == tools.MAX_ATTEMPTS
    assert r["decision"]["reason"] == "tool_failure"
    assert r["answer"]["source"] == "standard"
    # The steps come from the app's own lookup, so the engineer still sees the runbook.
    assert len(r["steps"]) == 4


async def test_one_transient_tool_error_is_retried_and_the_answer_is_released(harness):
    harness.runtime.local.apply_stage("qa-preview")
    tools.inject_transient_failures("get_incident", 1)
    try:
        r = await harness.engine.run(DANA, Q)
    finally:
        tools.clear_faults()
    assert r["decision"]["reason"] == "released"
    assert {t["tool"]: t for t in r["tool_trace"]}["get_incident"]["attempts"] == 2


async def test_instruction_edit_changes_the_next_answer_and_bumps_the_version(harness):
    harness.runtime.local.apply_stage("qa-preview")
    before = await harness.engine.run(DANA, Q)
    version = harness.runtime.local.edit_instructions("candidate", "End every answer with: Post updates in #inc-checkout.")
    after = await harness.engine.run(DANA, Q)
    assert version == 2
    assert after["config_used"]["version"] == 2
    assert after["answer"]["text"].endswith("Post updates in #inc-checkout.")
    assert not before["answer"]["text"].endswith("Post updates in #inc-checkout.")


async def test_unknown_engineer_or_unavailable_incident_shows_no_steps():
    from app import data
    h = await Harness(make_settings()).start()
    saved = data.INCIDENTS.pop("INC-4822")
    try:
        h.runtime.local.apply_stage("on-call")
        r = await h.engine.run(SAM, Q)
        assert r["incident"] is None and r["steps"] == []
        assert r["answer"]["source"] == "standard"
        assert "couldn't load this incident" in r["answer"]["text"]
    finally:
        data.INCIDENTS["INC-4822"] = saved
        await h.stop()
