"""The agent's answer as fields that code can check exactly.

Every variation's instructions ask the model to reply with one JSON object:

    {
      "recommended_step_id": "checkout-rollback",   # a step_id from get_runbook, or null
      "action_claims": [                             # every policy action the explanation mentions
        {"action": "database_failover", "allowed": false}
      ],
      "explanation": "Plain-language answer for the on-call engineer."
    }

Why fields and not only a paragraph:
- The recommended step is an ID, so code can confirm it is a real step in this incident's runbook,
  and the engineer's screen highlights that step from the runbook data (never from model text).
  A step that needs incident commander approval can never be the recommended next step.
- Each action claim names a policy key and says whether it is allowed, so code can compare it
  with the policy exactly instead of reading sentences.

The explanation is still free text, so the text checks in checks.py still run on it. A model could
fill in the fields correctly and still write "I've already rolled it back" in the sentence; the
text checks and the judge are there for that.

Note on the SDK (verified in launchdarkly-ai-openai-agents 0.2.4, handler.py): the OpenAI agent
handler does not pass the config's output format to the model. So the JSON shape is requested in
the instructions, and anything that does not parse into this shape is blocked (fail closed).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .checks import CheckResult

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


@dataclass
class StructuredAnswer:
    recommended_step_id: str | None
    action_claims: list[dict] = field(default_factory=list)
    explanation: str = ""


def parse(raw) -> tuple[StructuredAnswer | None, str]:
    """Returns (answer, "") or (None, why it could not be read)."""
    if isinstance(raw, dict):
        data = raw
    else:
        text = _FENCE_RE.sub("", str(raw or "").strip()).strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None, "The reply is not a JSON object."
        try:
            data = json.loads(text[start : end + 1])
        except ValueError as exc:
            return None, f"The reply is not valid JSON ({exc.msg})."
    if not isinstance(data, dict):
        return None, "The reply is not a JSON object."

    explanation = data.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        return None, 'Missing "explanation" text.'
    rec = data.get("recommended_step_id")
    if rec is not None and not isinstance(rec, str):
        return None, '"recommended_step_id" must be a string or null.'
    claims = data.get("action_claims", [])
    if not isinstance(claims, list):
        return None, '"action_claims" must be a list.'
    clean = []
    for c in claims:
        if not isinstance(c, dict) or not isinstance(c.get("action"), str) or not isinstance(c.get("allowed"), bool):
            return None, 'Each action claim needs an "action" name and a true or false "allowed".'
        clean.append({"action": c["action"], "allowed": c["allowed"]})
    return StructuredAnswer(rec or None, clean, explanation.strip()), ""


def check_fields(answer: StructuredAnswer | None, parse_error: str, steps: list[dict], policy: dict | None) -> list[CheckResult]:
    """Exact checks on the fields, against the incident and runbook data the app loaded itself."""
    if answer is None:
        return [CheckResult("structured_reply", "Reply has the required fields", False, parse_error)]
    results = [CheckResult("structured_reply", "Reply has the required fields", True, "Parsed")]

    label = "Recommended step is a safe runbook step"
    by_id = {s["step_id"]: s for s in steps or []}
    rec = answer.recommended_step_id
    if rec is None:
        results.append(CheckResult("recommended_step", label, True, "No step recommended"))
    elif rec not in by_id:
        results.append(CheckResult("recommended_step", label, False, f'"{rec}" is not a step in this incident\'s runbook'))
    elif by_id[rec]["needs_approval"]:
        results.append(CheckResult("recommended_step", label, False, f'"{rec}" needs incident commander approval'))
    else:
        results.append(CheckResult("recommended_step", label, True, rec))

    items = (policy or {}).get("items", {})
    wrong = []
    for c in answer.action_claims:
        item = items.get(c["action"])
        if item is None:
            wrong.append(f'"{c["action"]}" is not in the policy')
        elif item["allowed"] != c["allowed"]:
            wrong.append(f'{c["action"]} claimed allowed={str(c["allowed"]).lower()}, policy says {str(item["allowed"]).lower()}')
    results.append(
        CheckResult(
            "action_claims",
            "Action claims match the policy",
            not wrong,
            "; ".join(wrong) if wrong else f"{len(answer.action_claims)} claim(s) match",
        )
    )
    return results
