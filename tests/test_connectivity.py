"""The two outage cases are different and the app must handle both.

1. Connected, then lost the connection: the SDK keeps serving the last flag data it received.
   The AI explanation keeps working on that cached configuration.
2. Cold start with no configuration at all: flags return the app's defaults, so the AI
   guidance is OFF and every engineer gets the standard message.
"""

from threading import Event

import pytest
from ldclient.interfaces import UpdateProcessor

from app.personas import PERSONAS
from tests.conftest import Harness, make_settings

pytestmark = pytest.mark.asyncio
DANA = PERSONAS["qa-dana"]
Q = "What should I do first?"


async def test_lost_connection_after_startup_keeps_serving_cached_flags(harness):
    harness.runtime.local.apply_stage("qa-preview")
    harness.runtime.local.simulate_connection("interrupted")
    conn = harness.runtime.connection()
    assert conn["state"] == "interrupted"
    assert conn["initialized"] is True
    r = await harness.engine.run(DANA, Q)
    assert r["decision"]["reason"] == "released"
    assert r["config_used"]["variation_key"] == "candidate"


class _NeverDelivers(UpdateProcessor):
    """A data source that never receives any flag data (like a bad SDK key or no network)."""

    def __init__(self, ready: Event):
        self._ready = ready

    def start(self):
        self._ready.set()

    def stop(self):
        pass

    def is_alive(self):
        return False

    def initialized(self):
        return False


async def test_cold_start_without_configuration_falls_back_to_defaults():
    h = Harness(make_settings())
    h.runtime.local.update_processor_factory = lambda config, store, ready: _NeverDelivers(ready)
    await h.start()
    try:
        assert h.runtime.connection()["initialized"] is False
        r = await h.engine.run(DANA, Q)
        assert r["decision"]["reason"] == "feature_off"  # app default for the flag
        assert r["answer"]["source"] == "standard"
        available = await h.runtime.config_available(DANA.ld_context())
        assert available["enabled"] is False  # inspect_config cannot see a config either
    finally:
        await h.stop()
