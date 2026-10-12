"""All configuration comes from environment variables (or a .env file). See .env.example."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "")
    return value.strip() if value and value.strip() else default


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # "live" connects to LaunchDarkly with LD_SDK_KEY. "local" uses built-in test flags and makes
    # no network calls. "auto" picks live when LD_SDK_KEY is set.
    ld_mode: str
    ld_sdk_key: str

    # "openai" calls OpenAI through LaunchDarkly's OpenAI agent handler. "mock" uses a scripted
    # model so the app runs with no LLM key. "auto" picks openai when OPENAI_API_KEY is set.
    llm_mode: str

    # Keys of the LaunchDarkly resources this app reads. Change them here if you used other keys.
    flag_key: str
    agent_config_key: str
    judge_config_key: str  # empty string turns the judge off (code checks still run)

    judge_pass_threshold: float
    agent_timeout_seconds: float
    judge_timeout_seconds: float

    # Generic flag trigger URLs from LaunchDarkly (Flag > Configuration in environment > Triggers).
    # Treat them like passwords: they stay on the server and are never sent to the browser.
    trigger_off_url: str
    trigger_on_url: str
    trigger_http_method: str

    # Provider names that route to the OpenAI handler. The handler registers as "OpenAI";
    # the lowercase alias guards against a casing difference in the variation's provider name.
    openai_provider_names: tuple[str, ...] = field(default_factory=lambda: ("OpenAI", "openai"))

    mock_latency_ms: int = 700

    # Local test mode with LLM_MODE=openai only: real OpenAI model names for the local configs.
    local_model_small: str = "gpt-4o-mini"
    local_model_large: str = "gpt-4o"

    # Optional shared secret for the operator console. Empty = operator access from this
    # computer only (localhost). Set it to allow operators on other machines.
    operator_token: str = ""

    @property
    def live(self) -> bool:
        return self.ld_mode == "live"

    @property
    def judge_enabled(self) -> bool:
        return bool(self.judge_config_key)


def _judge_key() -> str:
    key = _env("JUDGE_CONFIG_KEY", "incident-policy-judge")
    return "" if key.lower() == "off" else key


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")

    sdk_key = _env("LD_SDK_KEY")
    ld_mode = _env("LD_MODE", "auto").lower()
    if ld_mode == "auto":
        ld_mode = "live" if sdk_key else "local"
    if ld_mode == "live" and not sdk_key:
        raise RuntimeError("LD_MODE=live needs LD_SDK_KEY. Set it in .env (see .env.example).")

    llm_mode = _env("LLM_MODE", "auto").lower()
    if llm_mode == "auto":
        # Local test flags use placeholder model names, so "auto" only picks the real model in
        # live mode. An OPENAI_API_KEY already set in your shell won't break the quick start.
        llm_mode = "openai" if (_env("OPENAI_API_KEY") and ld_mode == "live") else "mock"
    if llm_mode == "openai" and not _env("OPENAI_API_KEY"):
        raise RuntimeError("LLM_MODE=openai needs OPENAI_API_KEY. Set it in .env (see .env.example).")

    return Settings(
        ld_mode=ld_mode,
        ld_sdk_key=sdk_key,
        llm_mode=llm_mode,
        flag_key=_env("FLAG_KEY", "ai-incident-guidance"),
        agent_config_key=_env("AGENT_CONFIG_KEY", "incident-assistant"),
        judge_config_key=_judge_key(),
        judge_pass_threshold=_float("JUDGE_PASS_THRESHOLD", 0.7),
        agent_timeout_seconds=_float("AGENT_TIMEOUT_SECONDS", 45.0),
        judge_timeout_seconds=_float("JUDGE_TIMEOUT_SECONDS", 20.0),
        trigger_off_url=_env("LD_TRIGGER_OFF_URL"),
        trigger_on_url=_env("LD_TRIGGER_ON_URL"),
        trigger_http_method=_env("LD_TRIGGER_HTTP_METHOD", "POST").upper(),
        mock_latency_ms=int(_float("MOCK_LATENCY_MS", 700)),
        local_model_small=_env("LOCAL_MODEL_SMALL", "gpt-4o-mini"),
        local_model_large=_env("LOCAL_MODEL_LARGE", "gpt-4o"),
        operator_token=_env("OPERATOR_TOKEN"),
    )
