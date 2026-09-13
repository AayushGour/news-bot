import pytest

from pipeline.config import few_shot_enabled, is_free_model, MissingConfig, Settings

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
    assert s.openrouter_model_cheap != s.model_cheap
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



# --- free models only -------------------------------------------------------
#
# A paid model reached production by omission: OPENROUTER_MODEL_CHEAP was
# commented out, the default was google/gemini-2.5-flash-lite, and every triage
# and relevance call billed silently. Defaults are free and paid slugs are
# refused at startup.

def test_defaults_are_free_models():
    s = Settings.load({})
    for slug in (s.openrouter_model_cheap, s.openrouter_model_good,
                 s.openrouter_model_vision):
        assert is_free_model(slug), f"{slug} is not free"


@pytest.mark.parametrize("slug", [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-31b-it:free",
    "openrouter/free",
])
def test_free_slugs_are_recognised(slug):
    assert is_free_model(slug) is True


@pytest.mark.parametrize("slug", [
    "google/gemini-2.5-flash-lite",   # the one that actually billed
    "google/gemini-2.5-flash",
    "anthropic/claude-sonnet-4",
    "",
    "openrouter/auto",                # routes to paid models
])
def test_paid_slugs_are_rejected(slug):
    assert is_free_model(slug) is False


def _openrouter_env(**over):
    env = {
        "TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "h",
        "TELEGRAM_BOT_TOKEN": "t", "OPERATOR_USER_ID": "1",
        "CHANNEL_IDS": "-100", "LLM_PROVIDER": "openrouter",
        "OPENROUTER_API_KEY": "sk-or-x",
    }
    env.update(over)
    return env


def test_startup_refuses_a_paid_model():
    env = _openrouter_env(OPENROUTER_MODEL_CHEAP="google/gemini-2.5-flash-lite")
    with pytest.raises(MissingConfig, match="not free"):
        Settings.load(env).validate_for_run(env)


def test_startup_names_every_paid_model_not_just_the_first():
    env = _openrouter_env(
        OPENROUTER_MODEL_CHEAP="google/gemini-2.5-flash-lite",
        OPENROUTER_MODEL_VISION="google/gemini-2.5-flash",
    )
    with pytest.raises(MissingConfig) as exc:
        Settings.load(env).validate_for_run(env)
    assert "OPENROUTER_MODEL_CHEAP" in str(exc.value)
    assert "OPENROUTER_MODEL_VISION" in str(exc.value)


def test_all_free_models_start_fine():
    env = _openrouter_env()
    Settings.load(env).validate_for_run(env)


def test_local_provider_is_unaffected_by_the_free_rule():
    """Ollama models are local and cost nothing; the rule is OpenRouter-only."""
    env = _openrouter_env(LLM_PROVIDER="ollama")
    env.pop("OPENROUTER_API_KEY")
    Settings.load(env).validate_for_run(env)


# --- few-shot scope ---------------------------------------------------------
#
# A single on/off switch forced a trade: on the golden set, examples took
# enumeration from 2/4 composed to 4/4 while news went 10/10 to 8/10. Scoping
# by intent keeps the gain without paying for it on the other path.

@pytest.mark.parametrize("raw,expected", [
    ("off", "off"), ("list", "list"), ("all", "all"),
    ("LIST", "list"), (" all ", "all"),
    ("true", "all"), ("1", "all"), ("yes", "all"),   # legacy boolean
    ("false", "off"), ("0", "off"), ("no", "off"),   # explicitly disabled
    ("", "list"), (None, "list"),                    # unset takes the default
    ("nonsense", "list"),
])
def test_few_shot_mode_parsing(raw, expected):
    env = {} if raw is None else {"FEW_SHOT_EXAMPLES": raw}
    assert Settings.load(env).few_shot_examples == expected


@pytest.mark.parametrize("mode,intent,expected", [
    ("off", "list", False), ("off", "news", False), ("off", None, False),
    ("list", "list", True), ("list", "news", False), ("list", None, False),
    ("all", "list", True), ("all", "news", True), ("all", None, True),
])
def test_few_shot_scope(mode, intent, expected):
    assert few_shot_enabled(mode, intent) is expected


def test_an_item_with_no_intent_is_treated_as_news():
    """Items predating the intent column must not silently get examples."""
    assert few_shot_enabled("list", None) is False


def test_the_default_is_list_because_that_is_what_measured_best():
    """On the golden set: list composed 24/24 with 0 failures, against 22 and
    21 for off and all. On news it sends a byte-identical prompt to off."""
    assert Settings.load({}).few_shot_examples == "list"


def test_it_can_still_be_turned_off_explicitly():
    assert Settings.load({"FEW_SHOT_EXAMPLES": "off"}).few_shot_examples == "off"
    assert Settings.load({"FEW_SHOT_EXAMPLES": "false"}).few_shot_examples == "off"
