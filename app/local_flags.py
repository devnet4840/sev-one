"""Local test mode: LaunchDarkly flags and configs without a LaunchDarkly account.

This stands in for the LaunchDarkly dashboard so the app (and its tests) can run with no network.
It is NOT how the demo runs for a reviewer with an account; in live mode every change below is
something you do in the LaunchDarkly UI and the SDK receives over its streaming connection.

How it plugs in: the SDK's own TestData builder creates the flag JSON, and a small update
processor pushes it through the SDK's data source update sink. That is the same path the
streaming connection uses, so flag change listeners and data source status listeners fire
exactly as they do in live mode. (Verified against launchdarkly-server-sdk 9.18.0: writing
TestData straight into the store does NOT fire listeners, so we route through the sink.)

The JSON shape of an AgentControl config variation (``_ldMeta`` plus model, provider,
instructions, tools) matches what launchdarkly-ai-server 0.2.4 parses in lifecycle.py and
types_validation.py.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path
from threading import Event

from ldclient.integrations.test_data import TestData
from ldclient.interfaces import (
    DataSourceErrorInfo,
    DataSourceErrorKind,
    DataSourceState,
    UpdateProcessor,
)
from ldclient.versioned_data_kind import FEATURES

from .settings import Settings
from .tools import load_tool_definitions

INSTRUCTIONS_DIR = Path(__file__).resolve().parent.parent / "launchdarkly" / "instructions"

# The release sequence from the demo plan. In live mode you do each step in the LaunchDarkly UI.
STAGES: dict[str, str] = {
    "baseline": "AI guidance off for everyone. Every engineer gets the standard runbook message.",
    "qa-preview": "AI guidance on for the QA engineer only (individual target). Config targets her to the candidate prompt.",
    "on-call": "AI guidance on for every on-call engineer. Config rule: service_tier = critical gets the larger model.",
    "fast-fix": "Prepared failure: the default rule now serves the 'fast-fix' prompt to standard services.",
}

QA_KEY = "qa-dana"

# Variation order inside the local AgentControl config flag.
BASELINE, CANDIDATE, PREMIUM, FAST_FIX, DISABLED = range(5)


def _instructions(name: str) -> str:
    return (INSTRUCTIONS_DIR / f"{name}.md").read_text().strip()


def _agent_variation(key: str, model: str, instructions_file: str, version: int) -> dict:
    return {
        "_ldMeta": {"enabled": True, "variationKey": key, "version": version, "mode": "agent"},
        "model": {"name": model},
        "provider": {"name": "OpenAI"},
        "instructions": _instructions(instructions_file),
        "tools": load_tool_definitions(),
    }


def _judge_variation(model: str) -> dict:
    return {
        "_ldMeta": {"enabled": True, "variationKey": "policy-judge", "version": 1, "mode": "judge"},
        "model": {"name": model},
        "provider": {"name": "OpenAI"},
        "messages": [{"role": "system", "content": _instructions("judge")}],
        "evaluationMetricKey": "incident-policy-score",
    }


class _SinkSource(UpdateProcessor):
    """Update processor that feeds TestData flags through the SDK's update sink."""

    def __init__(self, owner: "LocalFlags", config, ready: Event):
        self._owner = owner
        self._config = config
        self._ready = ready
        self._started = False

    def start(self):
        sink = self._config.data_source_update_sink
        self._owner._attach(sink)
        sink.init(self._owner.td._make_init_data())
        sink.update_status(DataSourceState.VALID, None)
        self._started = True
        self._ready.set()

    def stop(self):
        self._owner._attach(None)

    def is_alive(self):
        return self._started

    def initialized(self):
        return self._started


class LocalFlags:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.td = TestData.data_source()
        self._sink = None
        self.stage = "baseline"
        self.feature_targeting_on = True
        self.connection = "valid"
        # With the mock model the names are labels only. With LLM_MODE=openai they must be real.
        small, large = ("mock-small", "mock-large") if settings.llm_mode == "mock" else (settings.local_model_small, settings.local_model_large)
        self._variations = [
            _agent_variation("baseline", small, "baseline", 1),
            _agent_variation("candidate", small, "candidate", 1),
            _agent_variation("premium-model", large, "baseline", 1),
            _agent_variation("fast-fix", small, "fast-fix", 1),
            {"_ldMeta": {"enabled": False, "variationKey": "disabled", "version": 1, "mode": "agent"}},
        ]
        self._push_judge()
        self.apply_stage("baseline")

    # -- wiring -------------------------------------------------------------

    def update_processor_factory(self, config, store, ready):
        """Pass this as Config(update_processor_class=...)."""
        return _SinkSource(self, config, ready)

    def _attach(self, sink) -> None:
        self._sink = sink

    def _push(self, builder) -> None:
        self.td.update(builder)
        if self._sink is not None:
            # td._current_flags holds the flag JSON TestData just built (SDK-internal, pinned version).
            self._sink.upsert(FEATURES, self.td._current_flags[builder._key])

    # -- what you would do in the LaunchDarkly UI ----------------------------

    def apply_stage(self, stage: str) -> None:
        if stage not in STAGES:
            raise ValueError(f"Unknown stage '{stage}'. Use one of: {', '.join(STAGES)}")
        self.stage = stage
        s = self.settings

        # Boolean flag: is AI guidance allowed to run for this engineer?
        flag = (
            self.td.flag(s.flag_key)
            .boolean_flag()
            .clear_rules()
            .clear_targets()
            .on(self.feature_targeting_on)
        )
        if stage == "baseline":
            flag = flag.fallthrough_variation(False)
        elif stage == "qa-preview":
            flag = flag.fallthrough_variation(False).variation_for_user(QA_KEY, True)
        else:
            flag = flag.fallthrough_variation(True).variation_for_user(QA_KEY, True)
        self._push(flag)

        # AgentControl config: which model and prompt does this engineer get?
        config = (
            self.td.flag(s.agent_config_key)
            .variations(*copy.deepcopy(self._variations))
            .off_variation(DISABLED)
            .clear_rules()
            .clear_targets()
            .on(True)
            .fallthrough_variation(BASELINE)
        )
        if stage in ("qa-preview", "on-call", "fast-fix"):
            config = config.variation_for_user(QA_KEY, CANDIDATE)
        if stage in ("on-call", "fast-fix"):
            config = config.if_match("service_tier", "critical").then_return(PREMIUM)
        if stage == "fast-fix":
            config = config.fallthrough_variation(FAST_FIX)
        self._push(config)

    def set_feature_targeting(self, on: bool) -> None:
        """What a flag trigger does: turn targeting for the boolean flag on or off."""
        self.feature_targeting_on = on
        self._push(self.td.flag(self.settings.flag_key).on(on))

    def edit_instructions(self, variation_key: str, extra_sentence: str) -> int:
        """Simulates editing a variation's instructions in the UI. Returns the new version.

        Builds a new variation dict instead of mutating the old one: the SDK's in-memory store
        may hold a reference to the old dict, and an in-place edit would change served values
        without a change event.
        """
        for i, v in enumerate(self._variations):
            if v["_ldMeta"]["variationKey"] == variation_key:
                updated = copy.deepcopy(v)
                updated["instructions"] = updated["instructions"] + "\n" + extra_sentence
                updated["_ldMeta"]["version"] += 1
                self._variations[i] = updated
                self.apply_stage(self.stage)
                return updated["_ldMeta"]["version"]
        raise ValueError(f"Unknown variation '{variation_key}'")

    def simulate_connection(self, state: str) -> None:
        """'interrupted' simulates losing the streaming connection after startup; 'valid' restores it."""
        if self._sink is None:
            return
        self.connection = state
        if state == "interrupted":
            info = DataSourceErrorInfo(DataSourceErrorKind.NETWORK_ERROR, 0, time.time(), "Simulated network loss")
            self._sink.update_status(DataSourceState.INTERRUPTED, info)
        else:
            self._sink.update_status(DataSourceState.VALID, None)

    def _push_judge(self) -> None:
        if self.settings.judge_config_key:
            self._push(
                self.td.flag(self.settings.judge_config_key)
                .variations(_judge_variation("mock-judge" if self.settings.llm_mode == "mock" else self.settings.local_model_small))
                .variation_for_all(0)
            )

    def snapshot(self) -> dict:
        return {
            "stage": self.stage,
            "stage_description": STAGES[self.stage],
            "stages": STAGES,
            "feature_targeting_on": self.feature_targeting_on,
            "connection": self.connection,
        }
