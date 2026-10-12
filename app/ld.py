"""LaunchDarkly client setup, shared by feature flags and the AI SDK.

One LDClient serves both jobs:
- Feature flag evaluation and change listeners (launchdarkly-server-sdk).
- AgentControl config evaluation and AI metrics (launchdarkly-ai-server), which we hand the same
  client through init_client(client=...) ("bring your own client" in the AI SDK docs).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from launchdarkly_ai_server import init_client, inspect_config
from launchdarkly_ai_server import shutdown as ai_shutdown
from ldclient import Config, Context, LDClient

from .local_flags import LocalFlags
from .settings import Settings

log = logging.getLogger("sev_one.ld")

LIVE_START_WAIT_SECONDS = 10


class LDRuntime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.local: LocalFlags | None = None if settings.live else LocalFlags(settings)
        self.client: LDClient | None = None

    async def start(self) -> None:
        s = self.settings
        if s.live:
            # SDK KEY: comes from LD_SDK_KEY in .env (LaunchDarkly > Settings > SDK keys,
            # the server-side SDK key for the environment you are demoing).
            config = Config(s.ld_sdk_key)
            start_wait = LIVE_START_WAIT_SECONDS
        else:
            # Local test mode: no network. The key is a placeholder that is never sent anywhere.
            config = Config(
                "local-test-mode",
                update_processor_class=self.local.update_processor_factory,
                send_events=False,
            )
            start_wait = 2
        # LDClient() blocks until connected or start_wait passes, so keep it off the event loop.
        self.client = await asyncio.to_thread(LDClient, config, start_wait)
        if not self.client.is_initialized():
            log.warning(
                "LaunchDarkly client did not initialize. Flags will use app defaults "
                "(AI explanation OFF) until it connects."
            )
        await init_client(client=self.client)

    async def stop(self) -> None:
        # The AI SDK's shutdown flushes events and closes the client we gave it.
        await ai_shutdown()
        self.client = None

    # -- evaluation -----------------------------------------------------------

    def feature_enabled(self, context: dict) -> bool:
        """Is AI guidance allowed for this engineer?

        App default is False: if LaunchDarkly cannot evaluate the flag (never connected,
        flag deleted, bad context), the feature stays off. Fail safe.
        """
        if self.client is None:
            return False
        try:
            return bool(self.client.variation(self.settings.flag_key, Context.from_dict(context), False))
        except Exception:  # never let a flag read take down a request
            log.exception("Flag evaluation failed; using default False")
            return False

    async def config_available(self, context: dict) -> dict:
        """What the AgentControl config would serve this engineer right now (no model call).

        inspect_config never raises (launchdarkly-ai-server lifecycle.py) and returns
        enabled=False with config=None when LaunchDarkly cannot be reached.
        """
        result = await inspect_config(self.settings.agent_config_key, context)
        return summarize_config(result.get("enabled", False), result.get("config"), result.get("meta"))

    def connection(self) -> dict:
        if self.client is None:
            return {"state": "off", "initialized": False, "error": None, "mode": self.settings.ld_mode}
        status = self.client.data_source_status_provider.status
        return {
            "state": status.state.value,
            "since": status.since,
            "initialized": self.client.is_initialized(),
            "error": status.error.message if status.error else None,
            "mode": self.settings.ld_mode,
        }

    # -- listeners --------------------------------------------------------------

    def add_flag_change_listener(self, callback: Callable[[str], Any]) -> None:
        """callback(flag_key). The SDK calls it on a worker thread (documented in
        ldclient.interfaces.FlagTracker.add_listener), so callers must hand off to their
        event loop before touching async state."""
        assert self.client is not None
        self.client.flag_tracker.add_listener(lambda change: callback(change.key))

    def add_status_listener(self, callback: Callable[[dict], Any]) -> None:
        assert self.client is not None
        self.client.data_source_status_provider.add_listener(lambda _status: callback(self.connection()))


def summarize_config(enabled: bool, config: dict | None, meta: dict | None) -> dict:
    meta = meta or {}
    config = config or {}
    return {
        "enabled": bool(enabled),
        "variation_key": meta.get("variationKey") or None,
        "version": meta.get("version"),
        "mode": meta.get("mode"),
        "model": (config.get("model") or {}).get("name"),
        "provider": (config.get("provider") or {}).get("name"),
        "tools": sorted((config.get("tools") or {}).keys()),
    }
