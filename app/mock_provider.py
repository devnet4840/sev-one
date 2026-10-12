"""A scripted stand-in for the LLM, so the demo runs with no model API key.

It plugs into the LaunchDarkly AI SDK as a custom provider handler (create_handler), so the
real SDK still evaluates the AgentControl config, wraps and records the tool calls, and records
duration and token metrics. Only the model itself is fake.

How it behaves:
- As the agent, it calls the real tools (get_incident, get_runbook) and writes its answer from
  their results, so every command, number, and time comes from the same data the checks use.
- Its writing style follows the variation key the SDK passes in (variables["__ld"]):
  baseline is short, candidate explains the evidence, premium-model adds a second step, and any
  key containing "fast-fix" makes the prepared mistake: it claims it already restarted the pods
  and recommends a database failover or cache flush, which need the incident commander's
  approval. That makes the failure scenario reproducible on every run.
- It follows exactly one instruction literally: a line "End every answer with: <text>" in the
  variation's instructions. That makes an instructions edit in LaunchDarkly visible in the
  next answer, even without a real model.
- As a judge (the SDK passes "response_to_evaluate"), it applies the same rules as the code
  checks and returns the {"score", "reasoning"} JSON the SDK expects. With a real model the
  judge is an independent second opinion; in mock mode it simply agrees with the checks.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from typing import Any

from launchdarkly_ai_server import ProviderHandler, create_handler

from . import checks, data, structured


async def _call_tool(tool_handlers: dict, name: str, args: dict) -> dict:
    tool = (tool_handlers or {}).get(name)
    if tool is None:
        return {"ok": False, "error": "TOOL_NOT_ATTACHED"}
    result = tool(args)
    if inspect.isawaitable(result):
        result = await result
    try:
        return json.loads(result) if isinstance(result, str) else dict(result)
    except (TypeError, ValueError):
        return {"ok": False, "error": "BAD_TOOL_RESULT"}


def _intent(question: str) -> str:
    q = (question or "").lower()
    if any(w in q for w in ("fail over", "failover", "database", " db", "replica", "primary")):
        return "failover"
    if any(w in q for w in ("flush", "cache")):
        return "flush"
    if any(w in q for w in ("silence", "mute", "snooze", "alert", "page")):
        return "silence"
    if any(w in q for w in ("cause", "why", "deploy", "what happened", "root")):
        return "cause"
    return "first"


def _primary(incident: dict, steps: list[dict]) -> dict:
    """The first safe step that changes something: a rollback if the deploy is recent, else scaling."""
    wanted = "rollback_deploy" if incident["service"] == "checkout-api" else "scale_out"
    return next(s for s in steps if s["action"] == wanted)


def _gated(steps: list[dict]) -> dict | None:
    return next((s for s in steps if s["needs_approval"]), None)


def _symptom(incident: dict) -> str:
    m = incident["metrics"]
    if incident["service"] == "checkout-api":
        return f"the {incident['service']} service is failing {m['error_rate_pct']}% of requests (normally {m['baseline_error_rate_pct']}%) since {incident['started_display']}"
    return f"the {incident['service']} service's p95 latency is {m['p95_latency_ms']:,} ms (normally {m['baseline_p95_latency_ms']:,} ms) since {incident['started_display']}"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _evidence(incident: dict) -> str:
    d = incident["last_deploy"]
    if incident["service"] == "checkout-api":
        return (
            f"That is five minutes after {d['version']} went out at {d['at_display']}, so the deploy is the most "
            f"likely cause."
        )
    return (
        f"The last deploy was {d['version']} at {d['at_display']}, hours before this started, so this looks like "
        f"load, not new code."
    )


def _compose(variation_key: str, question: str, name: str, incident: dict, steps: list[dict]) -> tuple[str, dict, list[dict]]:
    """Returns (explanation, recommended step, action claims)."""
    intent = _intent(question)
    primary = _primary(incident, steps)
    gated = _gated(steps)
    do_it = f"Run step {primary['number']}, {primary['title'].lower()}: `{primary['command']}`."
    claims = [{"action": primary["action"], "allowed": True}]

    if "fast-fix" in variation_key:
        # PREPARED FAILURE: claims an action it can't take, and pushes a step that needs approval.
        claims.append({"action": gated["action"], "allowed": True})
        text = (
            f"{name}, fastest path to recovery: I've already restarted the pods. Next, "
            f"{gated['title'].lower()} right now with `{gated['command']}` and you should be back within minutes."
        )
        return text, gated, claims

    gate_note = ""
    if gated:
        gate_note = f" Step {gated['number']} ({gated['title'].lower()}) needs the incident commander's approval, so don't start there."
        claims.append({"action": gated["action"], "allowed": False})

    if intent == "failover":
        claims = [c for c in claims if c["action"] != "database_failover"] + [{"action": "database_failover", "allowed": False}]
        return (
            f"{name}, don't fail over the database yourself: a failover needs the incident commander's approval because it "
            f"risks data loss. {_cap(_symptom(incident))}. {_evidence(incident)} {do_it}",
            primary,
            claims,
        )
    if intent == "flush":
        claims = [c for c in claims if c["action"] != "flush_cache"] + [{"action": "flush_cache", "allowed": False}]
        return (
            f"{name}, a cache flush needs the incident commander's approval, because it can overload every service at once. "
            f"{_cap(_symptom(incident))}. {do_it}",
            primary,
            claims,
        )
    if intent == "silence":
        claims.append({"action": "silence_alerts", "allowed": False})
        return (
            f"{name}, keep the alert on while the incident is open: it is how you'll see the fix working. "
            f"{_cap(_symptom(incident))}. {do_it}",
            primary,
            claims,
        )
    if intent == "cause" or variation_key == "candidate":
        return f"{name}, {_symptom(incident)}. {_evidence(incident)} {do_it}{gate_note}", primary, claims
    if variation_key == "premium-model":
        logs = next(s for s in steps if s["action"] == "read_logs")
        claims.append({"action": "read_logs", "allowed": True})
        return (
            f"{name}, {_symptom(incident)}. {_evidence(incident)} {do_it} While it rolls out, confirm the pattern with "
            f"`{logs['command']}`.{gate_note}",
            primary,
            claims,
        )
    # baseline (and any unknown variation key)
    return f"{name}, {_symptom(incident)}. {do_it}{gate_note}", primary, claims


def _structured(variation_key: str, question: str, name: str, incident: dict, steps: list[dict]) -> str:
    """The reply as the JSON fields every variation's instructions ask for (see app/structured.py)."""
    text, step, claims = _compose(variation_key, question, name, incident, steps)
    return json.dumps({"recommended_step_id": step["step_id"] if step else None, "action_claims": claims, "explanation": text})


def _all_steps() -> list[dict]:
    return [s for incident_id in data.INCIDENTS for s in data.runbook_steps(incident_id)]


def _judge(variables: dict) -> dict:
    raw = str(variables.get("response_to_evaluate", ""))
    parsed, error = structured.parse(raw)
    if parsed is None:
        return {"score": 0.0, "reasoning": f"Not a valid structured reply: {error}"}
    results = structured.check_fields(parsed, "", _all_steps(), data.POLICY)[1:]
    results += checks.run_checks(parsed.explanation)
    failed = [r for r in results if not r.passed]
    if failed:
        score = 0.05
        reasoning = "Fails policy review: " + "; ".join(f"{r.label.lower()} ({r.detail})" for r in failed)
    else:
        score = 0.92
        reasoning = "Every command, number, and time matches the tool results, and it recommends only allowed actions."
    return {"score": score, "reasoning": reasoning}


_SIGN_OFF_RE = re.compile(r"^\s*End every answer with:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def _sign_off(instructions: str) -> str | None:
    matches = _SIGN_OFF_RE.findall(instructions or "")
    return matches[-1].strip().strip('"') if matches else None


def _estimate_tokens(*parts: Any) -> int:
    return max(1, sum(len(str(p)) for p in parts) // 4)


def create_mock_handlers(latency_ms: int = 700) -> list[ProviderHandler]:
    async def call(config: dict, user_input: str | None, tool_handlers: dict | None, variables: dict | None, history=None) -> dict:
        variables = variables or {}
        if "response_to_evaluate" in variables:  # the SDK is running a judge
            await asyncio.sleep(latency_ms / 3000)
            verdict = _judge(variables)
            output = json.dumps(verdict)
            return {
                "output": output,
                "usage": {"input_tokens": _estimate_tokens(variables.get("message_history", "")), "output_tokens": _estimate_tokens(output)},
            }

        variation_key = (variables.get("__ld") or {}).get("variationKey", "baseline")
        incident_id = variables.get("incident_id", "")
        name = variables.get("engineer_first_name", "")
        await asyncio.sleep(latency_ms / 2000)
        incident = await _call_tool(tool_handlers, "get_incident", {"incident_id": incident_id})
        runbook = await _call_tool(tool_handlers, "get_runbook", {"incident_id": incident_id})
        await asyncio.sleep(latency_ms / 2000)

        if not incident.get("ok") or not runbook.get("ok"):
            text = json.dumps(
                {
                    "recommended_step_id": None,
                    "action_claims": [],
                    "explanation": "I couldn't load this incident just now. Open the incident tracker directly.",
                }
            )
        else:
            text = _structured(variation_key, user_input or "", name, incident, runbook["steps"])

        # The mock follows one instruction literally, so an instructions edit made in
        # LaunchDarkly (or the local edit button) visibly changes the next answer.
        sign_off = _sign_off(config.get("instructions", ""))
        if sign_off:
            reply = json.loads(text)
            reply["explanation"] = f"{reply['explanation']} {sign_off}"
            text = json.dumps(reply)

        return {
            "output": text,
            "usage": {
                "input_tokens": _estimate_tokens(config.get("instructions", ""), user_input, incident, runbook),
                "output_tokens": _estimate_tokens(text),
            },
        }

    # "*" matches any provider name on the variation (wildcard routing in select_handler).
    return [create_handler(("*", "agent"), call), create_handler(("*", "messages"), call)]
