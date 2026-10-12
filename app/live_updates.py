"""Pushes LaunchDarkly changes to open browsers with server-sent events (SSE). No page reload.

Flow for one change:
  LaunchDarkly (or a trigger) changes a flag
  -> the SDK's flag change listener fires on an SDK worker thread
  -> we hand the event to the app's event loop with call_soon_threadsafe
  -> for each open browser, re-evaluate the flag and config for that browser's engineer
  -> send only what actually changed, plus a timeline entry for the operator panel.

A browser that connects (or reconnects) gets a full snapshot first, so it never shows stale state.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .ld import LDRuntime
from .personas import get_persona

log = logging.getLogger("sev_one.live")

HEARTBEAT_SECONDS = 15
_CLOSE = "__close__"


@dataclass
class Subscriber:
    id: int
    persona_key: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=200))
    last_state: dict | None = None


class LiveHub:
    def __init__(self, runtime: LDRuntime, watched_keys: set[str]):
        self.runtime = runtime
        self.watched_keys = watched_keys
        self._subscribers: dict[int, Subscriber] = {}
        self._ids = itertools.count(1)
        self._loop: asyncio.AbstractEventLoop | None = None
        self.timeline: list[dict] = []

    # -- wiring ---------------------------------------------------------------

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register SDK listeners. Their callbacks run on SDK threads; we only schedule work."""
        self._loop = loop
        self.runtime.add_flag_change_listener(self._from_sdk_thread_flag_changed)
        self.runtime.add_status_listener(self._from_sdk_thread_status_changed)

    def _from_sdk_thread_flag_changed(self, flag_key: str) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._spawn, self.on_flag_changed(flag_key))

    def _from_sdk_thread_status_changed(self, connection: dict) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._spawn, self.on_connection_changed(connection))

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        task.add_done_callback(_log_task_error)

    # -- subscribers ----------------------------------------------------------

    async def subscribe(self, persona_key: str) -> Subscriber:
        get_persona(persona_key)  # validates
        sub = Subscriber(id=next(self._ids), persona_key=persona_key)
        self._subscribers[sub.id] = sub
        state = await self.state_for(persona_key)
        sub.last_state = state
        await self._send(sub, "snapshot", {"state": state, "timeline": self.timeline[-25:]})
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.pop(sub.id, None)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def stream(self, sub: Subscriber):
        """Yields SSE-formatted strings until the client goes away or the stream is closed."""
        try:
            while True:
                try:
                    event, payload = await asyncio.wait_for(sub.queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if event == _CLOSE:
                    return  # the browser's EventSource reconnects and gets a fresh snapshot
                yield f"event: {event}\ndata: {json.dumps(payload)}\n\n"
        finally:
            self.unsubscribe(sub)

    def close(self) -> None:
        """End every open stream (used at shutdown so the server can exit promptly)."""
        for sub in list(self._subscribers.values()):
            self._end(sub)

    # -- state ----------------------------------------------------------------

    async def state_for(self, persona_key: str) -> dict:
        persona = get_persona(persona_key)
        ctx = persona.ld_context()
        return {
            "persona_key": persona_key,
            "feature_on": self.runtime.feature_enabled(ctx),
            "config_available": await self.runtime.config_available(ctx),
            "connection": self.runtime.connection(),
        }

    async def on_flag_changed(self, flag_key: str) -> None:
        if flag_key not in self.watched_keys:
            return
        entry = {"at": time.time(), "kind": "flag_changed", "flag_key": flag_key}
        self._remember(entry)
        await self.broadcast("timeline", entry)
        await self.refresh_all()

    async def on_connection_changed(self, connection: dict) -> None:
        entry = {"at": time.time(), "kind": "connection", "state": connection.get("state"), "error": connection.get("error")}
        self._remember(entry)
        await self.broadcast("timeline", entry)
        await self.refresh_all()

    async def refresh_all(self) -> None:
        for sub in list(self._subscribers.values()):
            state = await self.state_for(sub.persona_key)
            if state != sub.last_state:
                sub.last_state = state
                await self._send(sub, "state", state)

    async def note(self, kind: str, **fields: Any) -> None:
        """Add an app event (trigger fired, answer blocked) to every timeline."""
        entry = {"at": time.time(), "kind": kind, **fields}
        self._remember(entry)
        await self.broadcast("timeline", entry)

    async def broadcast(self, event: str, payload: dict) -> None:
        for sub in list(self._subscribers.values()):
            await self._send(sub, event, payload)

    def _remember(self, entry: dict) -> None:
        self.timeline.append(entry)
        del self.timeline[:-100]

    async def _send(self, sub: Subscriber, event: str, payload: dict) -> None:
        try:
            sub.queue.put_nowait((event, payload))
        except asyncio.QueueFull:
            # A stuck browser tab should not hold up everyone else. End its stream; the
            # browser reconnects and gets a fresh snapshot.
            log.warning("Closing slow subscriber %s", sub.id)
            self._end(sub)

    def _end(self, sub: Subscriber) -> None:
        self.unsubscribe(sub)
        while not sub.queue.empty():
            sub.queue.get_nowait()
        sub.queue.put_nowait((_CLOSE, {}))


def _log_task_error(task: asyncio.Future) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("Live update task failed", exc_info=task.exception())
