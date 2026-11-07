from dotenv import dotenv_values
import pytest

from pipeline.config import (
    few_shot_enabled,
    is_auto_router,
    is_free_model,
    LLM_PROVIDER_ROLES,
    MissingConfig,
    ROOT,
    Settings,
)

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


def test_default_cheap_model_is_pinned_and_verified_enforcing():
    """openrouter/free (an auto-router) and nvidia/nemotron-3.5-lightning:free
    (does not support response_format at all) both served this role and both
    were wrong for different reasons. nvidia/nemotron-3.5-super-vl:free
    is the slug that was live-tested to actually honour a json_schema with a
    contradictory enum rather than ignore it or auto-route elsewhere."""
    from pipeline.config import _DEFAULT_OPENROUTER_MODEL_CHEAP
    assert Settings.load({}).openrouter_model_cheap == (
        _DEFAULT_OPENROUTER_MODEL_CHEAP
    )


@pytest.mark.parametrize("slug", [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-31b-it:free",
])
def test_free_slugs_are_recognised(slug):
    assert is_free_model(slug) is True


@pytest.mark.parametrize("slug", [
    "google/gemini-2.5-flash-lite",   # the one that actually billed
    "google/gemini-2.5-flash",
    "anthropic/claude-sonnet-4",
    "",
    "openrouter/auto",                # auto-router, not a paid model — but
                                       # is_free_model only answers "costs
                                       # money?", so it is still False here
])
def test_paid_slugs_are_rejected(slug):
    assert is_free_model(slug) is False


def test_openrouter_free_is_no_longer_a_free_alias():
    """openrouter/free costs nothing but is an auto-router, not a model — it
    must not be treated as free just because it carries no price. It gets its
    own rejection in validate_for_run (test_startup_refuses_an_auto_router)
    rather than silently passing the free-model check."""
    assert is_free_model("openrouter/free") is False


@pytest.mark.parametrize("slug", [
    "openrouter/free",
    "openrouter/auto",
    "OpenRouter/Free",        # case must not matter
    " openrouter/free ",      # surrounding whitespace must not matter
])
def test_auto_router_slugs_are_recognised(slug):
    assert is_auto_router(slug) is True


@pytest.mark.parametrize("slug", [
    "nvidia/nemotron-3.5-super-vl:free",
    "google/gemini-2.5-flash-lite",
    "",
    None,
])
def test_non_router_slugs_are_not_flagged(slug):
    assert is_auto_router(slug) is False


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


# --- auto-router rejection ---------------------------------------------------
#
# openrouter/free costs nothing (it passed the old free-model check by an
# explicit alias) but re-picks the upstream model on every call. 9 days of
# pipeline.log: 791 unusable completions, 221 rate limits, 140 silent local
# fallbacks — all on this one variable — and every A/B run sharing it was
# comparing against a moving target. is_free_model is not the right guard for
# this: it answers "does this cost money", not "is this reproducible".

def test_startup_refuses_an_auto_router():
    env = _openrouter_env(OPENROUTER_MODEL_CHEAP="openrouter/free")
    with pytest.raises(MissingConfig, match="reproducib"):
        Settings.load(env).validate_for_run(env)


def test_auto_router_error_is_distinct_from_the_paid_model_error():
    """The two must not be interchangeable: a paid-model fix (swap to a free
    slug) does not fix an auto-router, and reading "not free" about a slug
    that is literally named 'free' would send someone chasing the wrong
    thing."""
    env = _openrouter_env(OPENROUTER_MODEL_CHEAP="openrouter/free")
    with pytest.raises(MissingConfig) as exc:
        Settings.load(env).validate_for_run(env)
    assert "not free" not in str(exc.value)


def test_startup_names_every_auto_routed_model_not_just_the_first():
    env = _openrouter_env(
        OPENROUTER_MODEL_CHEAP="openrouter/free",
        OPENROUTER_MODEL_VISION="openrouter/auto",
    )
    with pytest.raises(MissingConfig) as exc:
        Settings.load(env).validate_for_run(env)
    assert "OPENROUTER_MODEL_CHEAP" in str(exc.value)
    assert "OPENROUTER_MODEL_VISION" in str(exc.value)


def test_auto_router_is_checked_before_the_generic_paid_check():
    """A mix of one auto-routed and one genuinely paid slug must fail on the
    auto-router message — it is the more specific, more actionable defect,
    and a startup that raises one error at a time should raise the one that
    actually matches what is wrong with each variable."""
    env = _openrouter_env(
        OPENROUTER_MODEL_CHEAP="openrouter/free",
        OPENROUTER_MODEL_GOOD="google/gemini-2.5-flash-lite",
    )
    with pytest.raises(MissingConfig, match="reproducib"):
        Settings.load(env).validate_for_run(env)


def test_local_provider_is_unaffected_by_the_free_rule():
    """Ollama models are local and cost nothing; the rule is OpenRouter-only."""
    env = _openrouter_env(LLM_PROVIDER="ollama")
    env.pop("OPENROUTER_API_KEY")
    Settings.load(env).validate_for_run(env)


# --- validate_model_config (T85) --------------------------------------------
#
# A model-only tool (eval_compose.py, smoke.py, build_golden.py, dashboard.py)
# never runs the Telegram listener and smoke.py is explicitly documented to
# work with "no Telegram, no Instagram" before any account is wired up.
# validate_for_run()'s Settings.REQUIRED check would make that workflow
# impossible, so these four call validate_model_config() instead — the same
# provider/auto-router/paid-model checks, none of the Telegram requirement.

def test_model_config_passes_with_no_telegram_vars_at_all():
    """The exact scenario smoke.py documents: zero Telegram/Instagram env."""
    Settings.load(env={}).validate_model_config()


def test_model_config_still_refuses_an_auto_router_with_no_telegram_vars():
    env = {"LLM_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-or-x",
           "OPENROUTER_MODEL_CHEAP": "openrouter/free"}
    with pytest.raises(MissingConfig, match="reproducib"):
        Settings.load(env).validate_model_config()


def test_model_config_still_refuses_a_paid_model_with_no_telegram_vars():
    env = {"LLM_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-or-x",
           "OPENROUTER_MODEL_CHEAP": "google/gemini-2.5-flash-lite"}
    with pytest.raises(MissingConfig, match="not free"):
        Settings.load(env).validate_model_config()


def test_model_config_still_requires_the_api_key():
    env = {"LLM_PROVIDER": "openrouter"}
    with pytest.raises(MissingConfig, match="OPENROUTER_API_KEY"):
        Settings.load(env).validate_model_config()


def test_validate_for_run_is_a_strict_superset_of_validate_model_config():
    """Every check validate_for_run performs on the model config must still
    fire when reached through validate_for_run — the split must not let one
    of T28's or T69's checks silently stop running on the full-service path."""
    env = _openrouter_env(OPENROUTER_MODEL_CHEAP="openrouter/free")
    with pytest.raises(MissingConfig, match="reproducib"):
        Settings.load(env).validate_for_run(env)
    env = _openrouter_env(OPENROUTER_MODEL_CHEAP="google/gemini-2.5-flash-lite")
    with pytest.raises(MissingConfig, match="not free"):
        Settings.load(env).validate_for_run(env)


# --- per-role provider override (T216) --------------------------------------
#
# OpenRouter's configured "good" model returned an unusable completion on
# EVERY call in production (3 attempts, ~5min each, twice per item) before
# falling back to local anyway -- so "good" needs to go straight to local
# while "cheap"/"vision" (which work fine hosted) stay on openrouter.
# LLM_PROVIDER_CHEAP / _GOOD / _VISION each override LLM_PROVIDER for their
# one role; empty/unset means "use the global value", unchanged from before
# this existed.

def test_provider_for_defaults_to_the_global_value_when_no_override_is_set():
    """Unchanged-path pin: with none of the three overrides set,
    provider_for must return exactly llm_provider for every role, under
    BOTH global values."""
    local = Settings.load(env={})
    assert local.llm_provider == "ollama"
    for role in LLM_PROVIDER_ROLES:
        assert local.provider_for(role) == "ollama"

    hosted = Settings.load(env={**MINIMAL, "LLM_PROVIDER": "openrouter"})
    for role in LLM_PROVIDER_ROLES:
        assert hosted.provider_for(role) == "openrouter"


def test_provider_for_honors_a_per_role_override():
    s = Settings.load(
        env={**MINIMAL, "LLM_PROVIDER": "openrouter", "LLM_PROVIDER_GOOD": "ollama"}
    )
    assert s.provider_for("good") == "ollama"
    assert s.provider_for("cheap") == "openrouter"
    assert s.provider_for("vision") == "openrouter"


def test_provider_override_works_in_either_direction():
    """A role can also reach for the hosted provider while the global
    default stays local."""
    s = Settings.load(env={**MINIMAL, "LLM_PROVIDER_CHEAP": "openrouter"})
    assert s.llm_provider == "ollama"
    assert s.provider_for("cheap") == "openrouter"
    assert s.provider_for("good") == "ollama"


def test_provider_override_is_normalised():
    s = Settings.load(env={**MINIMAL, "LLM_PROVIDER_GOOD": " OpenRouter "})
    assert s.provider_for("good") == "openrouter"


def test_an_unknown_per_role_override_is_rejected_at_startup():
    env = {**MINIMAL, "LLM_PROVIDER_GOOD": "claude"}
    with pytest.raises(MissingConfig, match="not a known provider"):
        Settings.load(env).validate_for_run(env)


def test_an_unknown_per_role_override_names_the_specific_variable():
    env = {**MINIMAL, "LLM_PROVIDER_VISION": "claude"}
    with pytest.raises(MissingConfig, match="LLM_PROVIDER_VISION"):
        Settings.load(env).validate_for_run(env)


def test_an_all_local_override_needs_no_api_key_even_with_a_hosted_global():
    """Every role overridden back to ollama means openrouter is never
    actually reached, so the key requirement must not fire."""
    env = _openrouter_env(
        LLM_PROVIDER_CHEAP="ollama",
        LLM_PROVIDER_GOOD="ollama",
        LLM_PROVIDER_VISION="ollama",
    )
    env.pop("OPENROUTER_API_KEY")
    Settings.load(env).validate_model_config()  # must not raise


def test_a_mixed_config_only_requires_the_key_for_roles_still_on_openrouter():
    env = _openrouter_env(LLM_PROVIDER_GOOD="ollama")
    env.pop("OPENROUTER_API_KEY")
    with pytest.raises(MissingConfig, match="OPENROUTER_API_KEY") as exc:
        Settings.load(env).validate_model_config()
    # names the roles that actually need it, not the one that was excused
    message = str(exc.value)
    assert "cheap" in message and "vision" in message
    assert "good" not in message


def test_a_mixed_config_does_not_check_an_unused_slug_for_a_local_role():
    """good's OPENROUTER_MODEL_GOOD is a paid slug, but good resolves to
    ollama in this config -- that slug is never sent anywhere, so it must
    not block startup."""
    env = _openrouter_env(
        LLM_PROVIDER_GOOD="ollama",
        OPENROUTER_MODEL_GOOD="google/gemini-2.5-flash-lite",
    )
    Settings.load(env).validate_model_config()  # must not raise


def test_a_mixed_config_still_checks_the_slug_for_roles_still_on_openrouter():
    """cheap stays on openrouter in this config, so its paid slug must
    still be caught -- the per-role scoping must not quietly disable the
    whole check."""
    env = _openrouter_env(
        LLM_PROVIDER_GOOD="ollama",
        OPENROUTER_MODEL_CHEAP="google/gemini-2.5-flash-lite",
    )
    with pytest.raises(MissingConfig, match="not free"):
        Settings.load(env).validate_model_config()


def test_a_mixed_config_still_catches_an_auto_router_on_a_role_still_hosted():
    env = _openrouter_env(
        LLM_PROVIDER_GOOD="ollama",
        OPENROUTER_MODEL_VISION="openrouter/free",
    )
    with pytest.raises(MissingConfig, match="reproducib"):
        Settings.load(env).validate_model_config()


# --- the reverse direction: global ollama, one role moved UP to openrouter --
#
# Reviewer (T216 bounce 1): every mixed-config test above starts from a
# hosted global (_openrouter_env, LLM_PROVIDER=openrouter) and overrides a
# role DOWN to ollama. None of them starts from a LOCAL global and overrides
# a role UP to openrouter -- so they cannot tell "openrouter_roles actually
# drives these three guards" apart from "the global being openrouter does",
# since in every case above the two happen to agree. Reverting config.py's
# `if openrouter_roles:` (and the key check's `if openrouter_roles and not
# self.openrouter_api_key:`) to `if self.llm_provider == "openrouter":` left
# the pre-bounce suite green -- these three close that gap: LLM_PROVIDER
# stays "ollama" (the default, unset here) and only LLM_PROVIDER_CHEAP
# resolves to openrouter, so a mutant that gates on the global instead of
# `provider_for` would let a paid or auto-routed OPENROUTER_MODEL_CHEAP (or
# a missing key) reach a live call -- the exact T28/T69 class of defect.

def test_an_override_toward_openrouter_from_a_local_global_requires_the_key():
    env = {**MINIMAL, "LLM_PROVIDER_CHEAP": "openrouter"}
    with pytest.raises(MissingConfig, match="OPENROUTER_API_KEY"):
        Settings.load(env).validate_model_config()


def test_an_override_toward_openrouter_from_a_local_global_still_refuses_a_paid_slug():
    env = {
        **MINIMAL,
        "LLM_PROVIDER_CHEAP": "openrouter",
        "OPENROUTER_API_KEY": "sk-or-x",
        "OPENROUTER_MODEL_CHEAP": "google/gemini-2.5-flash-lite",
    }
    with pytest.raises(MissingConfig, match="not free"):
        Settings.load(env).validate_model_config()


def test_an_override_toward_openrouter_from_a_local_global_still_refuses_an_auto_router():
    env = {
        **MINIMAL,
        "LLM_PROVIDER_CHEAP": "openrouter",
        "OPENROUTER_API_KEY": "sk-or-x",
        "OPENROUTER_MODEL_CHEAP": "openrouter/auto",
    }
    with pytest.raises(MissingConfig, match="reproducib"):
        Settings.load(env).validate_model_config()


# --- research note role routing (T71) ---------------------------------------
#
# The note is the post's substance, so which LLMClient role writes it is
# config, not a hardcoded call site — a one-variable, reversible flip.
# "good" is the intended destination but ships defaulted to "cheap" until
# T135 re-measures (D94) — see the docstring on Settings.research_note_role.

def test_research_note_role_defaults_to_cheap():
    assert Settings.load(env={}).research_note_role == "cheap"


def test_research_note_role_is_overridable():
    s = Settings.load(env={**MINIMAL, "RESEARCH_NOTE_ROLE": " Good "})
    assert s.research_note_role == "good"


def test_research_note_role_rejects_an_unknown_value_at_startup():
    env = {**MINIMAL, "RESEARCH_NOTE_ROLE": "vision"}
    with pytest.raises(MissingConfig, match="RESEARCH_NOTE_ROLE"):
        Settings.load(env=env).validate_for_run(env=env)


def test_model_config_refuses_an_unknown_research_note_role_with_no_telegram_vars():
    env = {"RESEARCH_NOTE_ROLE": "vision"}
    with pytest.raises(MissingConfig, match="RESEARCH_NOTE_ROLE"):
        Settings.load(env).validate_model_config()


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


# --- .env.example is pinned against its own guard (B5, bounce 1, reviewer) -
#
# T120 fixed .env.example's shipped OPENROUTER_MODEL_* slugs from paid
# Gemini defaults to free-tier ones, but nothing asserted that the shipped
# file actually clears validate_model_config() -- the exact guard those
# slugs exist to satisfy. A future hand-edit to .env.example (or a botched
# revert of T120) could silently reintroduce a paid or auto-routed slug and
# nothing in the suite would notice until a real run 401'd or billed. This
# loads the REAL shipped file via dotenv_values (never a re-typed copy of
# its values, per coding-standards.md house rule 3) and proves it against
# the real Settings.load / validate_model_config path.

def test_env_example_model_slugs_pass_validate_model_config():
    raw = dotenv_values(ROOT / ".env.example")
    env = {k: v for k, v in raw.items() if v is not None}
    env["LLM_PROVIDER"] = "openrouter"
    env["OPENROUTER_API_KEY"] = "sk-or-x"  # only presence is checked, not validity
    Settings.load(env=env).validate_model_config()  # must not raise


def test_env_example_slugs_agree_with_config_py_defaults():
    """nit (bounce 1, reviewer): the three OPENROUTER_MODEL_* slugs are
    re-typed literals in THREE places -- config.py's dataclass defaults,
    .env.example, and docs/setup.md's table -- with nothing pinning them
    together (coding-standards.md house rule 3: "if you must restate, add a
    test that asserts the two copies agree"). This pins the two load-bearing
    copies: an unset .env (Settings.load({})) must resolve to the exact same
    slugs as .env.example's shipped, uncommented defaults, so the file a new
    operator copies and the code's own fallback can never silently diverge.
    """
    raw = dotenv_values(ROOT / ".env.example")
    env_defaults = {k: v for k, v in raw.items() if v is not None}
    code_defaults = Settings.load(env={})

    assert env_defaults["OPENROUTER_MODEL_CHEAP"] == code_defaults.openrouter_model_cheap
    assert env_defaults["OPENROUTER_MODEL_GOOD"] == code_defaults.openrouter_model_good
    assert env_defaults["OPENROUTER_MODEL_VISION"] == code_defaults.openrouter_model_vision


# --- T222/D197: the verifier's local-only config ----------------------------
#
# model_verify/num_ctx_verify/VERIFY never reach LLM_PROVIDER_ROLES or
# provider_for at all -- they are plain Settings fields the verifier's own
# code path (LLMClient.verify, budget.budget_tokens_for_role) reads directly.


def test_verify_defaults_on():
    assert Settings.load(env={}).verify_enabled is True


@pytest.mark.parametrize("value, expected", [
    ("0", False), ("false", False), ("off", False), ("no", False),
    ("1", True), ("true", True), ("on", True), ("", True),
])
def test_verify_switch_parses_like_other_bools(value, expected):
    assert Settings.load(env={"VERIFY": value}).verify_enabled is expected


def test_model_verify_defaults_to_model_good_when_unset():
    """D197: "MODEL_VERIFY defaults to MODEL_GOOD" means dynamically -- not a
    second hardcoded copy of today's MODEL_GOOD literal that goes stale the
    first time someone changes MODEL_GOOD without also touching this."""
    s = Settings.load(env={"MODEL_GOOD": "hf.co/some-other-model"})
    assert s.model_verify == "hf.co/some-other-model"
    assert s.model_verify == s.model_good


def test_num_ctx_verify_defaults_to_num_ctx_good_when_unset():
    s = Settings.load(env={"NUM_CTX_GOOD": "12345"})
    assert s.num_ctx_verify == 12345
    assert s.num_ctx_verify == s.num_ctx_good


def test_model_verify_and_num_ctx_verify_are_independently_overridable():
    """An explicit MODEL_VERIFY/NUM_CTX_VERIFY wins over the model_good
    inheritance -- the fallback is "no override given", not "always equal
    to good"."""
    s = Settings.load(env={
        "MODEL_GOOD": "hf.co/good-model", "MODEL_VERIFY": "hf.co/verify-model",
        "NUM_CTX_GOOD": "16384", "NUM_CTX_VERIFY": "4096",
    })
    assert s.model_verify == "hf.co/verify-model"
    assert s.num_ctx_verify == 4096


def test_bare_settings_construction_cannot_diverge_model_verify_from_good():
    """Coordinator's check: a caller that builds ``Settings()`` directly,
    bypassing ``load()`` entirely, must not be able to end up with
    model_verify pointing at a model model_good itself does not name either
    -- both bare dataclass defaults are literally the same module constant
    (_DEFAULT_MODEL_GOOD), so this holds by construction, not by convention."""
    from pipeline.config import _DEFAULT_MODEL_GOOD, _DEFAULT_NUM_CTX_GOOD

    s = Settings()
    assert s.model_verify == s.model_good == _DEFAULT_MODEL_GOOD
    assert s.num_ctx_verify == s.num_ctx_good == _DEFAULT_NUM_CTX_GOOD


def test_verify_is_not_a_provider_role():
    """D197: "not added to LLM_PROVIDER_ROLES and not routable to
    OpenRouter" -- pinned directly, so a future edit adding it there is a
    visible diff against this test, not a silent capability change."""
    assert "verify" not in LLM_PROVIDER_ROLES


# ------------------------------------------------ T301: agentic revision switches


def test_revise_defaults_on_with_the_documented_cap():
    from pipeline.config import REVISE_ENGINES_BLOCKED_MAX_S

    s = Settings.load(env={})
    assert s.revise_enabled is True and s.revise_engines_blocked_max_s == REVISE_ENGINES_BLOCKED_MAX_S


@pytest.mark.parametrize("value, expected", [("false", False), ("0", False), ("off", False),
                                             ("true", True), ("1", True), ("", True)])
def test_revise_switch_parses_like_other_bools(value, expected):
    assert Settings.load(env={"REVISE_ENABLED": value}).revise_enabled is expected


@pytest.mark.parametrize("value, expected", [("90", 90), ("0", 0), ("-5", 0), ("junk", 1800)])
def test_revise_cap_is_an_int_floored_at_zero(value, expected):
    assert Settings.load(env={"REVISE_ENGINES_BLOCKED_MAX_S": value}).revise_engines_blocked_max_s == expected
