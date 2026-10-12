import pytest_asyncio

from app.agent import OperatorLog, TurnEngine, build_handlers
from app.ld import LDRuntime
from app.settings import Settings


def make_settings(**overrides) -> Settings:
    base = dict(
        ld_mode="local",
        ld_sdk_key="",
        llm_mode="mock",
        flag_key="ai-incident-guidance",
        agent_config_key="incident-assistant",
        judge_config_key="incident-policy-judge",
        judge_pass_threshold=0.7,
        agent_timeout_seconds=10,
        judge_timeout_seconds=10,
        trigger_off_url="",
        trigger_on_url="",
        trigger_http_method="POST",
        mock_latency_ms=0,
    )
    base.update(overrides)
    return Settings(**base)


class Harness:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.runtime = LDRuntime(settings)
        self.oplog = OperatorLog()
        self.engine = None

    async def start(self):
        await self.runtime.start()
        self.engine = TurnEngine(self.settings, self.runtime, build_handlers(self.settings), self.oplog)
        return self

    async def stop(self):
        await self.runtime.stop()


@pytest_asyncio.fixture
async def harness():
    h = await Harness(make_settings()).start()
    yield h
    await h.stop()
