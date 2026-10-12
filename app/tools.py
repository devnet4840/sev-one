"""Read-only tools the agent can call, plus the tracing that feeds the operator panel.

Security rule: the signed-in engineer is fixed by the server when the tools are built.
The model can pass any incident ID it likes, but each tool checks ownership in code and returns
the same "not found" answer for an incident that does not exist and one assigned to someone
else. The model never decides who may see what. Both tools only read: none of them runs a
command, so the assistant cannot change production.

Handler contract (verified in launchdarkly-ai-openai-agents 0.2.4, handler.py): the handler
calls each tool as ``tool(args_dict)`` and sends ``str(result)`` back to the model, so these
tools take one dict and return a JSON string.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from . import data

MAX_ATTEMPTS = 2  # one retry on a transient backend error
TOOL_TIMEOUT_SECONDS = 5.0
RETRY_BACKOFF_SECONDS = 0.1

# Simulated backend latency so the timings in the tool trace look like real calls.
SIMULATED_BACKEND_MS = 60


class TransientToolError(Exception):
    """A backend error worth one retry (for example a timeout from the incident tracker)."""


# Fault injection for tests and prepared failure scenarios: {tool_name: remaining_failures}.
_FAULTS: dict[str, int] = {}


def inject_transient_failures(tool_name: str, count: int) -> None:
    _FAULTS[tool_name] = count


def clear_faults() -> None:
    _FAULTS.clear()


def _maybe_fail(tool_name: str) -> None:
    remaining = _FAULTS.get(tool_name, 0)
    if remaining > 0:
        _FAULTS[tool_name] = remaining - 1
        raise TransientToolError(f"{tool_name}: incident tracker timed out (injected)")


NOT_FOUND = {
    "ok": False,
    "error": "INCIDENT_NOT_FOUND",
    "message": "No incident with that ID is assigned to this engineer.",
}


def _normalize_ref(args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    ref = args.get("incident_id")
    if not isinstance(ref, str) or not ref.strip():
        return None
    return ref.strip().upper()


# ---------------------------------------------------------------------------
# Backend lookups. The app uses these directly to render the incident and runbook
# cards, so the cards never depend on what the model says. The agent reaches the
# same data through the tool wrappers below.
# ---------------------------------------------------------------------------


def lookup_incident_for(user_key: str, incident_id: str) -> dict | None:
    record = data.incident_record(incident_id)
    if record is None or record["owner_key"] != user_key:
        return None
    return record


def lookup_steps_for(user_key: str, incident_id: str) -> list[dict] | None:
    if lookup_incident_for(user_key, incident_id) is None:
        return None
    return data.runbook_steps(incident_id)


def _public_incident(record: dict) -> dict:
    """What the model is allowed to see: no internal owner key."""
    return {k: v for k, v in record.items() if k != "owner_key"}


async def _get_incident(user_key: str, args: Any) -> dict:
    await asyncio.sleep(SIMULATED_BACKEND_MS / 1000)
    _maybe_fail("get_incident")
    ref = _normalize_ref(args)
    if ref is None:
        return {"ok": False, "error": "INVALID_ARGUMENTS", "message": "incident_id is required."}
    record = lookup_incident_for(user_key, ref)
    if record is None:
        return dict(NOT_FOUND)
    return {"ok": True, **_public_incident(record)}


async def _get_runbook(user_key: str, args: Any) -> dict:
    await asyncio.sleep(SIMULATED_BACKEND_MS / 1000)
    _maybe_fail("get_runbook")
    ref = _normalize_ref(args)
    if ref is None:
        return {"ok": False, "error": "INVALID_ARGUMENTS", "message": "incident_id is required."}
    steps = lookup_steps_for(user_key, ref)
    if steps is None:
        return dict(NOT_FOUND)
    return {
        "ok": True,
        "incident_id": ref,
        "read_only": True,
        "note": "The assistant cannot run these commands. Steps with needs_approval true need the incident commander.",
        "steps": steps,
    }


_TOOL_IMPLS: dict[str, Callable[[str, Any], Awaitable[dict]]] = {
    "get_incident": _get_incident,
    "get_runbook": _get_runbook,
}

TOOL_NAMES = tuple(_TOOL_IMPLS)


def _summarize(name: str, result: dict) -> str:
    if not result.get("ok"):
        return f"error: {result.get('error', 'UNKNOWN')}"
    if name == "get_incident":
        m = result["metrics"]
        not_allowed = [k for k, v in result["policy"]["items"].items() if not v["allowed"]]
        return (
            f"{result['severity']} {result['service']} ({result['environment']}): {m['error_rate_pct']}% errors, "
            f"p95 {m['p95_latency_ms']} ms; last deploy {result['last_deploy']['version']}; "
            f"needs approval: {', '.join(not_allowed)}"
        )
    if name == "get_runbook":
        return f"{len(result['steps'])} steps: " + "; ".join(f"{s['step_id']}{' (approval)' if s['needs_approval'] else ''}" for s in result["steps"])
    return "ok"


def _sanitize_result(result: dict) -> dict:
    """Copy of the result for the operator panel."""
    return json.loads(json.dumps(result))


def build_tool_handlers(user_key: str, trace: list[dict]) -> dict[str, Callable[[Any], Awaitable[str]]]:
    """Tools bound to the signed-in engineer. Every call is recorded in ``trace``.

    Each wrapper adds a timeout and one retry on a transient error. If the backend still fails,
    the model gets a structured error instead of an exception, so it can tell the engineer
    something useful, and the failure shows in the trace.
    """
    turn_start = time.monotonic()

    def make(name: str, impl: Callable[[str, Any], Awaitable[dict]]) -> Callable[[Any], Awaitable[str]]:
        async def tool(args: Any = None) -> str:
            started = time.monotonic()
            entry: dict[str, Any] = {
                "tool": name,
                "arguments": {"incident_id": str((args or {}).get("incident_id"))[:20]}
                if isinstance(args, dict)
                else {"raw": "<not an object>"},
                "started_at_ms": int((started - turn_start) * 1000),
            }
            attempts = 0
            result: dict
            while True:
                attempts += 1
                try:
                    result = await asyncio.wait_for(impl(user_key, args), timeout=TOOL_TIMEOUT_SECONDS)
                    break
                except (TransientToolError, asyncio.TimeoutError) as exc:
                    if attempts >= MAX_ATTEMPTS:
                        result = {
                            "ok": False,
                            "error": "TOOL_UNAVAILABLE",
                            "message": "The incident tracker is not responding right now.",
                            "detail": str(exc) or "timeout",
                        }
                        break
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS)
            entry.update(
                {
                    "ok": bool(result.get("ok")),
                    "error": None if result.get("ok") else result.get("error"),
                    "attempts": attempts,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "result_summary": _summarize(name, result),
                    "result": _sanitize_result(result),
                }
            )
            trace.append(entry)
            return json.dumps(result)

        tool.__name__ = name
        return tool

    return {name: make(name, impl) for name, impl in _TOOL_IMPLS.items()}


def load_tool_definitions() -> dict[str, dict]:
    """Tool definitions in the shape an AgentControl variation delivers them.

    Used by local test mode. In live mode these come from the tools you attach to the
    variation in LaunchDarkly (create them from launchdarkly/tools/*.json).
    """
    from pathlib import Path

    folder = Path(__file__).resolve().parent.parent / "launchdarkly" / "tools"
    definitions = {}
    for name in TOOL_NAMES:
        spec = json.loads((folder / f"{name}.json").read_text())
        definitions[name] = {
            "name": spec["key"],
            "type": "function",
            "description": spec["description"],
            "parameters": spec["parameters"],
        }
    return definitions
