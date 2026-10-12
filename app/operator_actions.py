"""Operator actions: roll back the AI explanation (and restore it) with a LaunchDarkly flag trigger.

Live mode calls the generic trigger URL you created in LaunchDarkly. The flag change then comes
back through the SDK's streaming connection, and the listener updates every open browser. This
endpoint does not change the UI itself, so what you see reflects what LaunchDarkly actually did.

Local test mode has no LaunchDarkly, so it simulates the trigger by turning targeting off on
the local flag, through the same listener path.

Notes:
- The trigger URL works like a password. It stays on the server and is never sent to the browser.
- LaunchDarkly's trigger docs do not state the HTTP method. Webhooks are normally POST, so this
  uses POST by default; set LD_TRIGGER_HTTP_METHOD if your account expects something else.
- Triggers are rate limited by LaunchDarkly, so repeated clicks may not all take effect.
"""

from __future__ import annotations

import httpx

from .ld import LDRuntime
from .settings import Settings


async def fire_trigger(settings: Settings, runtime: LDRuntime, turn_on: bool) -> dict:
    action = "restore" if turn_on else "rollback"
    if not settings.live:
        runtime.local.set_feature_targeting(turn_on)
        return {"ok": True, "action": action, "via": "local test mode (simulated trigger)"}

    url = settings.trigger_on_url if turn_on else settings.trigger_off_url
    env_name = "LD_TRIGGER_ON_URL" if turn_on else "LD_TRIGGER_OFF_URL"
    if not url:
        return {
            "ok": False,
            "action": action,
            "via": "launchdarkly trigger",
            "error": f"{env_name} is not set. Create a generic trigger on the flag and paste its URL into .env.",
        }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.request(settings.trigger_http_method, url)
    except Exception as exc:  # network errors, and httpx.InvalidURL for a mistyped URL
        # Report the error type only: the message could contain the secret URL.
        return {"ok": False, "action": action, "via": "launchdarkly trigger", "error": f"Trigger request failed: {exc.__class__.__name__}. Check {env_name} in .env."}
    return {
        "ok": response.is_success,
        "action": action,
        "via": "launchdarkly trigger",
        "http_status": response.status_code,
        "error": None if response.is_success else f"LaunchDarkly answered HTTP {response.status_code}",
    }
