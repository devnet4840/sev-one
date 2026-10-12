"""Demo engineers and the LaunchDarkly contexts built from them.

Each persona becomes a LaunchDarkly context of kind "user". Targeting in LaunchDarkly uses:
- key           -> individual targeting (the QA engineer "qa-dana")
- service_tier  -> rule-based targeting (engineers on "critical" services get the larger model)
- role          -> available for rules too ("qa" or "oncall")
- team          -> available for rules too ("checkout" or "search")

In a real app the signed-in session decides who the engineer is. Here the persona switcher
stands in for sign-in, and the server (never the browser or the model) builds the context.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Persona:
    key: str  # LaunchDarkly context key. Used for individual targeting.
    first_name: str
    last_name: str
    service_tier: str  # "standard" or "critical". Used for rule-based targeting.
    role: str  # "qa" or "oncall"
    team: str
    incident_id: str  # the incident this engineer is paged for
    label: str
    blurb: str

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def ld_context(self) -> dict:
        """The context dict the LaunchDarkly AI SDK expects (kind + key + custom attributes)."""
        return {
            "kind": "user",
            "key": self.key,
            "name": self.name,
            "service_tier": self.service_tier,
            "role": self.role,
            "team": self.team,
        }

    def public(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "first_name": self.first_name,
            "service_tier": self.service_tier,
            "role": self.role,
            "team": self.team,
            "incident_id": self.incident_id,
            "label": self.label,
            "blurb": self.blurb,
        }


PERSONAS: dict[str, Persona] = {
    p.key: p
    for p in [
        Persona(
            key="qa-dana",
            first_name="Dana",
            last_name="Reyes",
            service_tier="standard",
            role="qa",
            team="checkout",
            incident_id="INC-4820",
            label="QA engineer",
            blurb="Replays incidents in staging. Individually targeted to preview new AI behavior first.",
        ),
        Persona(
            key="oncall-sam",
            first_name="Sam",
            last_name="Patel",
            service_tier="standard",
            role="oncall",
            team="search",
            incident_id="INC-4822",
            label="On call, search",
            blurb="Paged for slow search. Gets whatever the default rule serves.",
        ),
        Persona(
            key="oncall-priya",
            first_name="Priya",
            last_name="Shah",
            service_tier="critical",
            role="oncall",
            team="checkout",
            incident_id="INC-4821",
            label="On call, checkout",
            blurb="Paged for a SEV-1 on a critical service. A rule on service_tier = critical routes her to a larger model.",
        ),
    ]
}


def get_persona(key: str) -> Persona:
    persona = PERSONAS.get(key)
    if persona is None:
        raise KeyError(f"Unknown persona '{key}'")
    return persona
