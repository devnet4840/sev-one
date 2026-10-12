"""One question from the on-call engineer, end to end.

Order of operations (this is the safety story, so it is spelled out):

1. Load the incident and its runbook straight from the backend. The incident card and runbook
   steps are built from this data, so the engineer always sees real steps, whatever the model does.
2. Read the boolean flag. If AI guidance is off for this engineer, send the standard message.
   Default is off, so a LaunchDarkly outage also lands here.
3. Invoke the AgentControl config through the LaunchDarkly AI SDK. The SDK picks the variation
   (model + instructions + tools) for this engineer, and records which one it used.
4. Read the reply's fields (recommended step, action claims, explanation) and check them exactly
   against the runbook and policy, then run the text checks on the explanation. Any failure
   blocks the answer.
5. Run the judge. A low score, a timeout, or no score at all blocks the answer (fail closed).
6. Re-check the flag right before sending. If an operator rolled back mid-answer, the engineer
   gets the standard message.
7. Send either the AI answer or the standard message. The engineer never sees a blocked answer;
   the operator panel does, labeled as blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
import uuid
from collections import deque
from typing import Any

from launchdarkly_ai_server import ProviderHandler, config, create_handler, run_judges

from . import checks, fallback, structured, tools
from .ld import LDRuntime
from .mock_provider import create_mock_handlers
from .personas import Persona
from .settings import Settings

log = logging.getLogger("sev_one.agent")


def build_handlers(settings: Settings) -> list[ProviderHandler]:
    if settings.llm_mode == "mock":
        return create_mock_handlers(settings.mock_latency_ms)

    # Real model: LaunchDarkly's OpenAI agent handler (launchdarkly-ai-openai-agents). It reads
    # OPENAI_API_KEY from the environment and registers for provider "OpenAI", mode "agent".
    from launchdarkly_ai_openai_agents import create_openai_agent_handler

    # LaunchDarkly is the monitoring layer in this demo, so the OpenAI Agents SDK's own trace
    # upload to OpenAI is off unless you opt back in with OPENAI_AGENTS_DISABLE_TRACING=false.
    if os.environ.get("OPENAI_AGENTS_DISABLE_TRACING") is None:
        from agents import set_tracing_disabled

        set_tracing_disabled(True)

    real = create_openai_agent_handler()
    handlers = [real]
    for name in settings.openai_provider_names:
        if real.provides_for and name != real.provides_for[0]:
            # Alias in case the variation's provider name differs only in casing.
            handlers.append(create_handler((name, "agent"), lambda *args, _h=real: _h(*args)))
    return handlers


def render_trajectory(trace: list[dict]) -> str:
    """The agent's tool calls and results, as text, for the judge to read as evidence."""
    if not trace:
        return "Tool calls: none."
    lines = ["Tool calls made by the assistant, in order:"]
    for i, entry in enumerate(trace, 1):
        lines.append(f"{i}. {entry['tool']}({json.dumps(entry['arguments'])})")
        lines.append(f"   Result: {json.dumps(entry.get('result'))}")
    return "\n".join(lines)


class OperatorLog:
    """Blocked answers and other incidents the operator should see. In memory, newest first."""

    def __init__(self, limit: int = 50):
        self.blocked_count = 0
        self.incidents: deque[dict] = deque(maxlen=limit)

    def record(self, incident: dict) -> None:
        self.blocked_count += 1
        self.incidents.appendleft(incident)

    def snapshot(self) -> dict:
        return {"blocked_count": self.blocked_count, "incidents": list(self.incidents)[:10]}


class TurnEngine:
    def __init__(self, settings: Settings, runtime: LDRuntime, handlers: list[ProviderHandler], oplog: OperatorLog):
        self.settings = settings
        self.runtime = runtime
        self.handlers = handlers
        self.oplog = oplog
        # Operator-armed test fixture: the next AI answer is a known bad reply instead of a model
        # call. Used to show the checks blocking a failure on cue, even with a real model.
        self.fixture_armed = False

    async def run(self, persona: Persona, question: str) -> dict:
        s = self.settings
        started = time.monotonic()
        turn_id = uuid.uuid4().hex[:8]
        ctx = persona.ld_context()

        # 1. Authoritative data for the cards (and the evidence for the checks).
        incident = tools.lookup_incident_for(persona.key, persona.incident_id)
        steps = tools.lookup_steps_for(persona.key, persona.incident_id) or []

        result: dict[str, Any] = {
            "turn_id": turn_id,
            "persona": persona.public(),
            "question": question,
            "incident": public_incident(incident),
            "steps": steps,
            "config_used": None,
            "checks": [],
            "judge": {"enabled": s.judge_enabled, "ran": False, "score": None, "passed": None, "reasoning": "", "error": None},
            "tool_trace": [],
            "usage": None,
            "candidate_text": None,  # the AI answer, shown to the operator even when blocked
            "recommended_step_id": None,  # set only when an AI answer is released
            "fixture": False,  # True when the answer was the labeled test fixture, not a model
            "timing": {},
        }

        def finish(source: str, reason: str, detail: str = "", text: str | None = None) -> dict:
            result["answer"] = {
                "source": source,  # "ai" or "standard"
                "text": text if text is not None else fallback.standard_message(incident, steps),
            }
            result["decision"] = {"released": source == "ai", "reason": reason, "detail": detail}
            result["timing"]["total_ms"] = int((time.monotonic() - started) * 1000)
            if reason in BLOCK_REASONS:
                self.oplog.record(
                    {
                        "turn_id": turn_id,
                        "at": time.time(),
                        "persona": persona.name,
                        "persona_key": persona.key,
                        "reason": reason,
                        "detail": detail,
                        "variation_key": (result["config_used"] or {}).get("variation_key"),
                        "excerpt": (result["candidate_text"] or "")[:240],
                    }
                )
            return result

        # 2. Is AI guidance allowed for this engineer right now?
        if not self.runtime.feature_enabled(ctx):
            return finish("standard", "feature_off", f"Flag '{s.flag_key}' serves false for this engineer.")

        # Test fixture (operator-armed, one answer only). No model call, no tool calls, and no judge
        # call, so nothing is sent to LaunchDarkly's metrics as if a model had produced it.
        if self.fixture_armed:
            self.fixture_armed = False
            result["fixture"] = True
            result["config_used"] = {
                "config_key": None,
                "variation_key": "test-fixture",
                "version": None,
                "model": "Test fixture (not a model answer)",
                "provider": None,
                "run_id": None,
            }
            result["judge"].update({"ran": False, "error": None, "reasoning": "Skipped: test fixture, not a model answer."})
            return self._evaluate(result, finish, ctx, fixture_reply(persona.first_name, incident, steps), incident, steps, trace=None)

        # 3. Ask the agent. The SDK evaluates the AgentControl config for this context.
        trace: list[dict] = []
        tool_handlers = tools.build_tool_handlers(persona.key, trace)
        agent_started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                config(
                    key=s.agent_config_key,
                    handler=self.handlers,
                    tool_handlers=tool_handlers,
                    # We run the judge ourselves below, the same way whether or not one is
                    # attached to the variation in LaunchDarkly, so skip the SDK's inline run.
                    skip_judges=True,
                ).invoke(
                    question,
                    ctx,
                    variables={
                        "incident_id": persona.incident_id,
                        "engineer_first_name": persona.first_name,
                    },
                ),
                timeout=s.agent_timeout_seconds,
            )
        except asyncio.TimeoutError:
            result["tool_trace"] = trace
            result["timing"]["agent_ms"] = int((time.monotonic() - agent_started) * 1000)
            return finish("standard", "ai_timeout", f"No answer within {s.agent_timeout_seconds:.0f}s.")
        except Exception as exc:  # disabled config, unreachable LaunchDarkly, provider error
            result["tool_trace"] = trace
            result["timing"]["agent_ms"] = int((time.monotonic() - agent_started) * 1000)
            log.warning("Agent invocation failed: %s", exc)
            return finish("standard", "ai_error", str(exc))

        result["timing"]["agent_ms"] = int((time.monotonic() - agent_started) * 1000)
        result["tool_trace"] = trace
        track = response.track_data or {}
        result["config_used"] = {
            "config_key": track.get("configKey"),
            "variation_key": track.get("variationKey"),
            "version": track.get("version"),
            "model": track.get("modelName"),
            "provider": track.get("providerName"),
            "run_id": track.get("runId"),
        }
        usage = response.usage
        result["usage"] = {"input": usage.input, "output": usage.output, "total": usage.total} if usage else None
        raw = response.response if isinstance(response.response, str) else json.dumps(response.response)
        verdict = None
        if s.judge_enabled:
            # The judge runs on every answer, even ones the checks will block, so LaunchDarkly
            # records a quality score for everything a variation produces, including the bad ones.
            judge_started = time.monotonic()
            verdict = await self._run_judge(ctx, question, raw, track, trace)
            result["judge"].update(verdict)
            result["timing"]["judge_ms"] = int((time.monotonic() - judge_started) * 1000)
        return self._evaluate(result, finish, ctx, raw, incident, steps, trace=trace, verdict=verdict)

    def _evaluate(self, result, finish, ctx, raw: str, incident, steps, trace, verdict=None) -> dict:
        """Steps 4 to 7: checks, decision, last flag check, release."""
        s = self.settings
        result["candidate_text"] = raw

        # 4. Field checks (exact, against the runbook and policy), then text checks on the explanation.
        checks_started = time.monotonic()
        policy = (incident or {}).get("policy")
        answer, parse_error = structured.parse(raw)
        check_results = structured.check_fields(answer, parse_error, steps, policy)
        if answer is not None:
            check_results += checks.run_checks(answer.explanation, policy)
        result["checks"] = [c.to_dict() for c in check_results]
        result["timing"]["checks_ms"] = int((time.monotonic() - checks_started) * 1000)

        # Decide, most specific reason first. Every path below sends the standard message.
        if trace is not None:
            tool_problem = _tool_problem(trace)
            if tool_problem:
                # The answer is not grounded in a successful lookup, so it can't be trusted.
                return finish("standard", "tool_failure", tool_problem)
        if not checks.all_passed(check_results):
            failed = "; ".join(c.detail for c in check_results if not c.passed)
            return finish("standard", "blocked_by_checks", failed)
        if verdict is not None:
            if verdict["error"]:
                return finish("standard", "judge_unavailable", verdict["error"])
            if not verdict["passed"]:
                return finish(
                    "standard",
                    "blocked_by_judge",
                    f"Score {verdict['score']:.2f} is below {s.judge_pass_threshold:.2f}. {verdict['reasoning']}",
                )

        # 6. Last look at the flag: an operator may have rolled back while we were working.
        if not self.runtime.feature_enabled(ctx):
            return finish("standard", "rolled_back_during_answer", "The flag turned off before the answer was sent.")

        # 7. Release. The engineer gets the explanation; the highlighted step comes from the
        # validated step ID, and the step itself is still built from runbook data.
        result["recommended_step_id"] = answer.recommended_step_id
        return finish("ai", "released", "Passed all checks.", text=answer.explanation)

    async def _run_judge(self, ctx: dict, question: str, answer: str, track: dict, trace: list[dict]) -> dict:
        s = self.settings
        verdict = {"enabled": True, "ran": True, "score": None, "passed": None, "reasoning": "", "error": None}
        try:
            results = await asyncio.wait_for(
                run_judges(
                    # Ask the SDK to run exactly our judge config, every time (sampling rate 1).
                    config={"judgeConfiguration": {"judges": [{"key": s.judge_config_key, "samplingRate": 1}]}},
                    user_context=ctx,
                    handler=self.handlers[0],
                    handlers=self.handlers,
                    user_input=question,
                    llm_response=answer,
                    base_track_data=track,
                    trajectory=render_trajectory(trace),
                ),
                timeout=s.judge_timeout_seconds,
            )
        except asyncio.TimeoutError:
            verdict["error"] = f"Judge did not answer within {s.judge_timeout_seconds:.0f}s."
            return verdict
        except Exception as exc:
            verdict["error"] = f"Judge failed: {exc}"
            return verdict

        judge = (results or {}).get(s.judge_config_key)
        if judge is None:
            # run_judges logs and swallows judge errors (launchdarkly-ai-server judges.py),
            # so a missing result means the judge could not run. Treat it as a failure and
            # show the SDK's own error message.
            reason = _JUDGE_ERRORS.pop(s.judge_config_key, None)
            verdict["error"] = (
                f"No score from judge '{s.judge_config_key}': {reason}"
                if reason
                else f"No score from judge '{s.judge_config_key}'. Check that the judge config exists, is on, and uses the OpenAI provider."
            )
            return verdict
        score = judge.score
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
            # Fail closed on anything outside the 0 to 1 contract, such as a 7 from a judge prompt
            # edited to use a 1 to 10 scale. A score of exactly 1 can't be told apart from a
            # perfect score, so keep the judge prompt's 0 to 1 scale fixed.
            verdict["error"] = f"Judge returned {score!r}, not a score from 0 to 1."
            return verdict
        verdict["score"] = float(score)
        verdict["reasoning"] = judge.response
        verdict["passed"] = float(score) >= s.judge_pass_threshold
        return verdict


BLOCK_REASONS = {"blocked_by_checks", "blocked_by_judge", "judge_unavailable", "ai_error", "ai_timeout", "tool_failure"}


def fixture_reply(name: str, incident: dict | None, steps: list[dict]) -> str:
    """A known bad reply, used only when an operator arms the test fixture.

    Its fields are correct (a real, safe runbook step, and database failover correctly marked not
    allowed), but its explanation claims the assistant already rolled back the deploy, which its
    read-only tools cannot do. That is the case the field checks alone would miss, so the text
    checks have to catch it.
    """
    first_safe = next((s for s in steps or [] if not s["needs_approval"] and s["action"] != "read_logs"), None)
    version = ((incident or {}).get("last_deploy") or {}).get("version", "the last deploy")
    return json.dumps(
        {
            "recommended_step_id": first_safe["step_id"] if first_safe else None,
            "action_claims": [{"action": "database_failover", "allowed": False}],
            "explanation": (
                f"[TEST FIXTURE] {name + ', ' if name else ''}I've already rolled back {version}, "
                "so errors should drop within a minute. No need to page anyone."
            ),
        }
    )


def _tool_problem(trace: list[dict]) -> str | None:
    """Returns why the answer can't be trusted, or None.

    - get_incident must have succeeded: it carries the metrics and the policy every answer depends on.
    - Any tool the agent did call must have succeeded at least once. An answer that never needed
      get_runbook is fine; the command check still requires any command it names to be a runbook command.
    """
    problems = []
    for name in tools.TOOL_NAMES:
        calls = [t for t in trace if t["tool"] == name]
        if not calls:
            if name == "get_incident":
                problems.append("get_incident was never called, so the answer isn't based on the incident data")
        elif not any(t["ok"] for t in calls):
            problems.append(f"{name} failed ({calls[-1].get('error')})")
    return "; ".join(problems) or None


class _JudgeErrorCapture(logging.Handler):
    """Keeps the last error the AI SDK logged for each judge key (judges.py logs instead of raising)."""

    _PATTERN = re.compile(r"Judge '(?P<key>[^']+)' failed: (?P<msg>.*)", re.DOTALL)

    def emit(self, record: logging.LogRecord) -> None:
        m = self._PATTERN.match(record.getMessage())
        if m:
            _JUDGE_ERRORS[m.group("key")] = m.group("msg")[:300]


_JUDGE_ERRORS: dict[str, str] = {}
logging.getLogger("launchdarkly_ai_server.judges").addHandler(_JudgeErrorCapture(level=logging.ERROR))


def public_incident(incident: dict | None) -> dict | None:
    """The incident fields the engineer's screen shows. No owner key, and no policy text."""
    if not incident:
        return None
    keep = ("incident_id", "service", "environment", "severity", "title", "status", "alert", "metrics", "last_deploy", "started_display")
    return {k: incident[k] for k in keep}
