import pytest

from pipeline.config import MissingConfig, Settings

MINIMAL = {
    "TELEGRAM_API_ID": "12345",
    "TELEGRAM_API_HASH": "hash",
    "TELEGRAM_BOT_TOKEN": "token",
    "OPERATOR_USER_ID": "999",
    "CHANNEL_IDS": "-1001526709058",
}


def test_defaults_are_safe_without_any_env():
    s = Settings.load(env={})
    assert s.dry_run is True, "DRY_RUN must default on so nothing publishes by accident"
    assert s.num_ctx_cheap == 8192 and s.num_ctx_good == 16384
    assert s.triage_threshold == 6


def test_vision_default_is_not_the_broken_model():
    """llama3.2-vision cannot load on Ollama 0.33+ — its mllama architecture was
    dropped and the server 500s. Benchmarked 0/11 and 0/8 against qwen2.5vl's
    11/11 and 8/8, purely because it never starts."""
    assert Settings.load(env={}).model_vision == "qwen2.5vl:7b"
    assert "llama3.2-vision" not in Settings.load(env={}).model_vision


def test_channel_ids_parse_as_negative_ints():
    s = Settings.load(env=MINIMAL)
    assert s.channel_ids == (-1001526709058,)


def test_multiple_channel_ids_parse():
    s = Settings.load(env={**MINIMAL, "CHANNEL_IDS": "-100123, -100456"})
    assert s.channel_ids == (-100123, -100456)


def test_validate_lists_every_missing_required_var():
    s = Settings.load(env={})
    with pytest.raises(MissingConfig) as exc:
        s.validate_for_run(env={})
    message = str(exc.value)
    for key in Settings.REQUIRED:
        assert key in message


def test_validate_passes_with_minimal_env():
    Settings.load(env=MINIMAL).validate_for_run(env=MINIMAL)


def test_disabling_dry_run_requires_publish_credentials():
    """Turning off DRY_RUN without R2/Instagram config must fail at startup,
    not three stages into a pipeline run."""
    env = {**MINIMAL, "DRY_RUN": "false"}
    with pytest.raises(MissingConfig, match="publishing is not configured"):
        Settings.load(env=env).validate_for_run(env=env)


def test_dry_run_accepts_truthy_spellings():
    for value, expected in [("1", True), ("true", True), ("on", True),
                            ("0", False), ("false", False), ("no", False)]:
        assert Settings.load(env={**MINIMAL, "DRY_RUN": value}).dry_run is expected


def test_provider_defaults_to_local_ollama():
    """No LLM_PROVIDER in .env must mean exactly the previous behaviour."""
    assert Settings.load(env={}).llm_provider == "ollama"


def test_provider_is_normalised():
    s = Settings.load(env={**MINIMAL, "LLM_PROVIDER": " OpenRouter "})
    assert s.llm_provider == "openrouter"


def test_each_provider_keeps_its_own_model_names():
    """Switching providers must not overwrite or require re-typing the other
    provider's models."""
    s = Settings.load(env={**MINIMAL, "LLM_PROVIDER": "openrouter"})
    assert s.model_cheap == "qwen3:4b-instruct"
    assert s.openrouter_model_cheap.startswith("google/")
    assert s.openrouter_model_good and s.openrouter_model_vision


def test_openrouter_models_are_overridable():
    s = Settings.load(env={**MINIMAL, "OPENROUTER_MODEL_GOOD": "anthropic/claude-sonnet-4.5"})
    assert s.openrouter_model_good == "anthropic/claude-sonnet-4.5"


def test_openrouter_without_a_key_fails_at_startup():
    """Every call would 401, and a 401 is Terminal — the whole queue would fail
    permanently, one item at a time. Fail here instead."""
    env = {**MINIMAL, "LLM_PROVIDER": "openrouter"}
    with pytest.raises(MissingConfig, match="OPENROUTER_API_KEY"):
        Settings.load(env=env).validate_for_run(env=env)


def test_openrouter_with_a_key_validates():
    env = {**MINIMAL, "LLM_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-or-x"}
    Settings.load(env=env).validate_for_run(env=env)


def test_unknown_provider_fails_at_startup():
    env = {**MINIMAL, "LLM_PROVIDER": "openai"}
    with pytest.raises(MissingConfig, match="not a known provider"):
        Settings.load(env=env).validate_for_run(env=env)

