"""The standard (non-AI) message.

Built only from incident data with a fixed template, so it cannot invent anything. The engineer
gets this whenever the AI guidance is off, blocked, timed out, or unavailable, and the runbook
steps still render from the same data. On-call keeps working without the AI.
"""

from __future__ import annotations


def standard_message(incident: dict | None, steps: list[dict] | None) -> str:
    if not incident:
        return (
            "We couldn't load this incident right now. Open the incident tracker directly, "
            "and page the incident commander if customers are affected."
        )
    m = incident["metrics"]
    deploy = incident["last_deploy"]
    lines = [
        f"{incident['incident_id']} ({incident['severity']}): {incident['service']} in {incident['environment']}. "
        f"Error rate {m['error_rate_pct']}% (normal {m['baseline_error_rate_pct']}%), "
        f"p95 latency {m['p95_latency_ms']:,} ms (normal {m['baseline_p95_latency_ms']:,} ms), "
        f"since {incident['started_display']}. Last deploy: {deploy['version']} at {deploy['at_display']}.",
    ]
    if steps:
        lines.append("Work through the runbook steps below in order.")
        lines.append('Steps marked "Needs approval" require the incident commander.')
    else:
        lines.append("Open the service runbook in the wiki, and page the incident commander if customers are affected.")
    return " ".join(lines)
