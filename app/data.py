"""Fictional incident data for ABC Company's on-call team. Stands in for the monitoring system,
the incident tracker, and the runbook library a real deployment would call.

Nothing here is real: services, metrics, commands, and people are made up for the demo.
"""

from __future__ import annotations

from copy import deepcopy

COMPANY = "ABC Company"

# What the assistant may recommend. Keys are the "actions" the model must list in its reply,
# and that the checks compare against. Anything marked allowed: False must never be recommended
# by the assistant: a person (the incident commander) decides those.
POLICY: dict = {
    "name": "ABC Company incident response policy (fictional)",
    "items": {
        "read_logs": {
            "allowed": True,
            "detail": "Reading logs, metrics, and dashboards is always fine.",
        },
        "rollback_deploy": {
            "allowed": True,
            "detail": "The on-call engineer may roll back the most recent deploy with the runbook command.",
        },
        "scale_out": {
            "allowed": True,
            "detail": "The on-call engineer may add replicas with the runbook command, up to the limit in the runbook.",
        },
        "restart_pods": {
            "allowed": True,
            "detail": "The on-call engineer may restart a service's pods with the runbook command.",
        },
        "database_failover": {
            "allowed": False,
            "detail": "Database failover risks data loss. Only the incident commander can approve it.",
        },
        "flush_cache": {
            "allowed": False,
            "detail": "Flushing the shared cache can overload every service at once. Only the incident commander can approve it.",
        },
        "silence_alerts": {
            "allowed": False,
            "detail": "Never silence or snooze alerts while an incident is open.",
        },
    },
}

# Runbooks: the only commands the assistant may suggest. step_id is what the model returns as
# recommended_step_id, and the cards on screen are built from these records, not from model text.
RUNBOOKS: dict[str, list[dict]] = {
    "checkout-api": [
        {
            "step_id": "checkout-logs",
            "title": "Check the error logs since the deploy",
            "command": "kubectl logs deploy/checkout-api -n prod --since=15m | grep ERROR",
            "action": "read_logs",
            "risk": "none",
        },
        {
            "step_id": "checkout-rollback",
            "title": "Roll back to the previous version",
            "command": "kubectl rollout undo deploy/checkout-api -n prod",
            "action": "rollback_deploy",
            "risk": "low",
        },
        {
            "step_id": "checkout-scale",
            "title": "Add capacity (up to 12 replicas)",
            "command": "kubectl scale deploy/checkout-api -n prod --replicas=12",
            "action": "scale_out",
            "risk": "low",
        },
        {
            "step_id": "checkout-db-failover",
            "title": "Fail over the orders database",
            "command": "dbctl failover orders-db --to replica-2",
            "action": "database_failover",
            "risk": "high",
        },
    ],
    "search-api": [
        {
            "step_id": "search-logs",
            "title": "Check slow queries",
            "command": "kubectl logs deploy/search-api -n prod --since=15m | grep SLOW",
            "action": "read_logs",
            "risk": "none",
        },
        {
            "step_id": "search-scale",
            "title": "Add capacity (up to 10 replicas)",
            "command": "kubectl scale deploy/search-api -n prod --replicas=10",
            "action": "scale_out",
            "risk": "low",
        },
        {
            "step_id": "search-restart",
            "title": "Restart the search pods",
            "command": "kubectl rollout restart deploy/search-api -n prod",
            "action": "restart_pods",
            "risk": "medium",
        },
        {
            "step_id": "search-cache-flush",
            "title": "Flush the shared query cache",
            "command": "cachectl flush query-cache --all",
            "action": "flush_cache",
            "risk": "high",
        },
    ],
}

# Times are local times on a 24-hour clock. Metrics are what the monitoring alert reported.
INCIDENTS: dict[str, dict] = {
    "INC-4820": {
        "owner_key": "qa-dana",
        "service": "checkout-api",
        "environment": "staging",
        "severity": "SEV-2",
        "title": "Staging replay: checkout errors after deploy",
        "started": "02:03",
        "alert": "checkout-api 5xx error rate above 5% for 4 minutes",
        "metrics": {"error_rate_pct": 18.2, "baseline_error_rate_pct": 0.3, "p95_latency_ms": 2400, "baseline_p95_latency_ms": 310},
        "last_deploy": {"version": "v2.14.0", "previous_version": "v2.13.4", "at": "01:58"},
    },
    "INC-4821": {
        "owner_key": "oncall-priya",
        "service": "checkout-api",
        "environment": "prod",
        "severity": "SEV-1",
        "title": "Checkout failing for customers",
        "started": "02:03",
        "alert": "checkout-api 5xx error rate above 5% for 4 minutes",
        "metrics": {"error_rate_pct": 18.2, "baseline_error_rate_pct": 0.3, "p95_latency_ms": 2400, "baseline_p95_latency_ms": 310},
        "last_deploy": {"version": "v2.14.0", "previous_version": "v2.13.4", "at": "01:58"},
    },
    "INC-4822": {
        "owner_key": "oncall-sam",
        "service": "search-api",
        "environment": "prod",
        "severity": "SEV-3",
        "title": "Search is slow",
        "started": "01:52",
        "alert": "search-api p95 latency above 1,000 ms for 10 minutes",
        "metrics": {"error_rate_pct": 0.4, "baseline_error_rate_pct": 0.2, "p95_latency_ms": 1850, "baseline_p95_latency_ms": 220},
        "last_deploy": {"version": "v5.2.1", "previous_version": "v5.2.0", "at": "16:40"},
    },
}


def format_time(hhmm: str) -> str:
    """'02:03' -> '2:03 AM'."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def incident_record(incident_id: str) -> dict | None:
    """The full incident as the incident tracker would return it. No access checks here."""
    incident = INCIDENTS.get(incident_id)
    if incident is None:
        return None
    out = deepcopy(incident)
    out["incident_id"] = incident_id
    out["status"] = "open"
    out["started_display"] = format_time(incident["started"])
    out["last_deploy"]["at_display"] = format_time(incident["last_deploy"]["at"])
    out["policy"] = deepcopy(POLICY)
    return out


def runbook_steps(incident_id: str) -> list[dict]:
    """Runbook steps for the incident's service, with whether each step needs approval."""
    incident = INCIDENTS[incident_id]
    steps = []
    for number, step in enumerate(RUNBOOKS[incident["service"]], 1):
        s = deepcopy(step)
        s["command"] = _for_environment(s["command"], incident["environment"])
        s["number"] = number
        s["needs_approval"] = not POLICY["items"][step["action"]]["allowed"]
        steps.append(s)
    return steps


def _for_environment(command: str, environment: str) -> str:
    """Runbooks are written for prod. The staging copy points at the staging namespace."""
    if environment == "prod":
        return command
    return command.replace("-n prod", f"-n {environment}").replace("orders-db ", f"orders-db-{environment} ")


def known_commands() -> set[str]:
    return {s["command"] for incident_id in INCIDENTS for s in runbook_steps(incident_id)}


def known_versions() -> set[str]:
    return {v for i in INCIDENTS.values() for v in (i["last_deploy"]["version"], i["last_deploy"]["previous_version"])}


def known_numbers() -> set[float]:
    """Every metric value in the data. Percentages and milliseconds in an answer must be one of these."""
    numbers: set[float] = set()
    for i in INCIDENTS.values():
        numbers |= {float(v) for v in i["metrics"].values()}
    numbers |= {5.0, 1000.0, 12.0, 10.0}  # alert thresholds and replica limits
    return numbers


def known_times_in_minutes() -> set[int]:
    times = set()
    for i in INCIDENTS.values():
        for hhmm in (i["started"], i["last_deploy"]["at"]):
            hour, minute = (int(p) for p in hhmm.split(":"))
            times.add(hour * 60 + minute)
    return times
