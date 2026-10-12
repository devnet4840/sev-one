import pytest

from app.settings import load_settings

KEYS = ["LD_SDK_KEY", "LD_MODE", "LLM_MODE", "OPENAI_API_KEY", "JUDGE_CONFIG_KEY", "LD_TRIGGER_HTTP_METHOD", "OPERATOR_TOKEN", "LOCAL_MODEL_SMALL", "LOCAL_MODEL_LARGE"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("app.settings.load_dotenv", lambda *a, **k: None)


def test_no_keys_means_local_flags_and_mock_model():
    s = load_settings()
    assert (s.ld_mode, s.llm_mode) == ("local", "mock")
    assert s.judge_config_key == "incident-policy-judge"


def test_keys_switch_to_live_and_openai(monkeypatch):
    monkeypatch.setenv("LD_SDK_KEY", "sdk-123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-123")
    s = load_settings()
    assert (s.ld_mode, s.llm_mode) == ("live", "openai")


def test_forcing_live_without_a_key_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("LD_MODE", "live")
    with pytest.raises(RuntimeError, match="LD_SDK_KEY"):
        load_settings()


def test_judge_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("JUDGE_CONFIG_KEY", "OFF")
    s = load_settings()
    assert s.judge_config_key == "" and not s.judge_enabled


def test_an_openai_key_in_the_shell_does_not_break_local_mode(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-123")
    s = load_settings()
    assert (s.ld_mode, s.llm_mode) == ("local", "mock")


def test_local_mode_can_still_use_the_real_model_on_purpose(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-123")
    monkeypatch.setenv("LLM_MODE", "openai")
    assert load_settings().llm_mode == "openai"
