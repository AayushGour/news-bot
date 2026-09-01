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
