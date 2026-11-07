"""Environment -> typed settings.

Settings load with permissive defaults so tests and offline development need no
credentials. Two validators fail fast, for two different audiences, and both
are wired into more than one caller — see T85:

- ``validate_for_run(env=None)`` — the full service guard (Telegram
  credentials, publish config, and the model config below). Callers:
  ``src/pipeline/__main__.py`` and ``scripts/preflight.py``.
- ``validate_model_config()`` — the narrower model/provider-only guard
  (auto-router, paid-slug, provider/key checks). ``validate_for_run`` calls
  it too, so the full service still gets every check it makes; it is also
  called directly by the four model-only tools that have no Telegram account
  to require: ``scripts/eval_compose.py``, ``scripts/smoke.py``,
  ``scripts/build_golden.py``, and ``scripts/dashboard.py``.

Six call sites, two validators — the process refuses to start (or make a real
model call) with a half-configured environment rather than discovering it
mid-run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[2]

#: Inference backends ``LLMClient`` knows how to talk to. Selected by
#: ``LLM_PROVIDER`` so moving off the local GPU is one variable, not a code edit.
LLM_PROVIDERS = ("ollama", "openrouter")

#: Roles that can each pick their own provider (T216). ``LLMClient._model``,
#: ``_chat`` and ``budget.budget_tokens_for_role`` all key on exactly these
#: three — see ``Settings.provider_for``, the one place the per-role decision
#: is made.
LLM_PROVIDER_ROLES = ("cheap", "good", "vision")

#: Role -> the ``Settings`` field name holding its Ollama ``num_ctx`` (T222).
#: Folds what used to be two independent copies of this mapping — budget.py's
#: own ``_NUM_CTX_FIELD`` (a named debt, left unfolded at T70 because llm.py
#: was out of that task's file scope) and the dict literal inline in
#: ``LLMClient._num_ctx`` — into the one place that actually owns the
#: Settings schema. Both modules import this now; neither keeps its own copy.
#: "verify" is listed for completeness (it genuinely is ``num_ctx_verify``'s
#: role) even though nothing reaches it through this dict today:
#: ``LLMClient.verify`` calls ``_ollama`` directly with
#: ``settings.num_ctx_verify``, never through ``_num_ctx``/``provider_for``
#: (D197 — it must stay off that machinery, or a future refactor could make
#: it OpenRouter-routable), and ``budget.budget_tokens_for_role`` special-cases
#: "verify" before it ever consults this dict, for the identical reason.
NUM_CTX_FIELD_FOR_ROLE: dict[str, str] = {
    "cheap": "num_ctx_cheap",
    "good": "num_ctx_good",
    "vision": "num_ctx_vision",
    "verify": "num_ctx_verify",
}

#: T222/D197: ``model_verify``/``num_ctx_verify`` default to "whatever
#: ``model_good``/``num_ctx_good`` default to" — a single module constant,
#: not a second hand-typed copy of the literal, so the two bare dataclass
#: defaults structurally cannot diverge (test_config.py pins this). This is
#: ONLY the fallback for ``Settings()`` built directly, bypassing ``load()``
#: (tests do this; production never does) — ``load()`` makes the real
#: inheritance dynamic, from whatever MODEL_GOOD/NUM_CTX_GOOD actually
#: resolved to in the environment, not from this constant.
_DEFAULT_MODEL_GOOD = "qwen3.5:9b"
_DEFAULT_NUM_CTX_GOOD = 16384

#: OpenRouter cheap model default — a single module constant to avoid
#: hand-typed copies at both the dataclass default (line 251) and the
#: load() fallback (line 398). Live-tested to enforce response_format:
#: json_schema rather than advertise support and ignore it (T69, T231).
_DEFAULT_OPENROUTER_MODEL_CHEAP = "nvidia/nemotron-3.5-super-vl:free"

#: OpenRouter marks zero-cost models with a ``:free`` suffix. This is an
#: extension point for a slug that is verified free (priced at 0 for both
#: prompt and completion against the models API) but does not happen to carry
#: the suffix. ``openrouter/free`` does NOT belong here even though it costs
#: nothing — it is an auto-router, not a model, and is rejected separately by
#: ``is_auto_router`` with its own error. See T69.
FREE_MODEL_ALIASES: frozenset[str] = frozenset()

#: OpenRouter reserves its own ``openrouter/`` namespace for routing
#: pseudo-models rather than a real vendor's model — ``openrouter/free`` picks
#: a random free model on every call, ``openrouter/auto`` a random paid one.
#: No model is actually published under this org, so matching the whole
#: namespace catches every routing alias OpenRouter has or adds, not just the
#: one slug that has already cost 9 days of unusable completions.
AUTO_ROUTER_PREFIX = "openrouter/"


#: How widely worked examples are shown to the composer.
FEW_SHOT_MODES = ("off", "list", "all")

#: Roles the research note call may be routed to (T71). Not "vision" — the
#: note call sends no images, and ``LLMClient.vision`` requires a positional
#: ``image_paths`` argument ``cheap``/``good`` do not take, so routing there
#: would fail the call outright rather than degrade it.
RESEARCH_NOTE_ROLES = ("cheap", "good")


#: Retention defaults (T241). Editorial limits - the operator's call - and the
#: single definition: Settings fields and the loader both read these.
RETENTION_AWAITING_APPROVAL_MAX_DAYS = 120  # operator, 2026-11-01
RETENTION_CLEANUP_INTERVAL_S = 6 * 3600
RETENTION_TERMINAL_GRACE_DAYS = 1
RETENTION_FAILED_GRACE_DAYS = 30
RETENTION_S3_CONNECT_TIMEOUT_S = 5
RETENTION_S3_READ_TIMEOUT_S = 60
#: A floor so a typo (CLEANUP_INTERVAL_S=1) cannot spin the job.
MIN_CLEANUP_INTERVAL_S = 60

#: Instagram throttling (T287). Meta's action/spam throttle ("User is performing
#: too many actions") fired after ~40 posts in an hour, well under the 24h
#: publish cap. Throttled items are deferred, never failed, and the delay is
#: long because retrying inside a block can extend it: Graph rate-limit windows
#: are rolling hours, and action blocks last hours.
IG_RATE_LIMIT_BACKOFF_S = 2 * 3600
#: Code 368 is an abuse/disallowed-action block that can last days, and
#: writes made while blocked may extend it - so probe once a day, not every 2h.
IG_POLICY_BLOCK_BACKOFF_S = 24 * 3600
#: Minimum spacing between successive publishes, so a burst cannot recur.
#: 180s caps the account at 20 posts/hour while still letting an operator's
#: approval go out within three minutes at worst.
IG_MIN_PUBLISH_GAP_S = 180

#: Revision planner deadline (D266, T296). Editorial: wall-clock for ONE planner
#: model call. The local good model decodes ~14 tok/s (T293 spike), so a full
#: plan is ~30-60s and the spike saw 150s stalls; past this the call is treated
#: as infrastructure being slow and the revision is deferred (Retryforever, no
#: attempt spent), never failed.
PLANNER_TIMEOUT_S = 120

#: Agentic revision (T301). Editorial: how long a revision keeps deferring while
#: the search engines are suspended/blocked (EnginesBlocked, T260) before it
#: gives up on the search-backed actions (research, fix_fact, replace_image),
#: reports them as not done and runs the rest. A CAPTCHA suspension can outlast
#: days; the operator is waiting on a preview, not on SearXNG. 30 minutes lets a
#: short rate-limit pass.
REVISE_ENGINES_BLOCKED_MAX_S = 30 * 60
#: Editorial: retry spacing while engines are blocked, so the 30 minutes above
#: costs ~6 re-plans, not ~30 (the worker's default backoff is 60s).
REVISE_ENGINES_BLOCKED_RETRY_S = 5 * 60


def _few_shot_mode(value: str | None, default: str = "off") -> str:
    """Parse FEW_SHOT_EXAMPLES, tolerating the boolean it used to be.

    The default is "list" because that is what the golden set measured: with
    examples scoped to enumerations the whole set composed, 24 of 24 with no
    failures, against 22 and 21 for off and everywhere. On news items it sends
    a byte-identical prompt to off, so the default costs nothing there.
    """
    text = (value or "").strip().lower()
    if text in FEW_SHOT_MODES:
        return text
    if text in {"1", "true", "yes", "on"}:
        return "all"
    if text in {"0", "false", "no", "off"}:
        return "off"
    return default


def few_shot_enabled(mode: str, intent: str | None) -> bool:
    """Does a request with this intent get a worked example?"""
    if mode == "all":
        return True
    if mode == "list":
        return (intent or "news") == "list"
    return False


def is_free_model(slug: str) -> bool:
    """Is this OpenRouter slug free to call?

    Structural, so it holds with no network. ``preflight`` confirms the actual
    price against the API — this only has to make a paid model impossible to
    select by accident, which is how ``google/gemini-2.5-flash-lite`` ended up
    serving every triage call after an override was commented out.
    """
    slug = (slug or "").strip()
    return slug.endswith(":free") or slug in FREE_MODEL_ALIASES


def is_auto_router(slug: str) -> bool:
    """Does this slug serve a different upstream model on every call?

    An auto-router breaks reproducibility even when it is free: which model
    actually answered a given item is unknown and unstable, so no A/B
    measurement can be trusted and context budgeting is impossible. This is a
    distinct defect from cost, so it gets its own check and its own error —
    see ``validate_model_config``.
    """
    return (slug or "").strip().lower().startswith(AUTO_ROUTER_PREFIX)


class MissingConfig(RuntimeError):
    """Raised at startup when required environment variables are absent."""


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _ints(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    return tuple(int(part) for part in value.replace(" ", "").split(",") if part)


@dataclass(frozen=True, slots=True)
class Settings:
    # --- telegram ---
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_bot_token: str = ""
    operator_user_id: int = 0
    channel_ids: tuple[int, ...] = ()
    session_path: Path = ROOT / "secrets" / "telegram.session"

    # --- models ---
    #: "ollama" (local, default) or "openrouter" (hosted, OpenAI-compatible).
    llm_provider: str = "ollama"
    #: Per-role overrides (T216): empty means "use llm_provider" for that
    #: role. Exists because a role can be good enough locally (or broken on
    #: the hosted side) independently of the other two — e.g. good=ollama
    #: while cheap/vision stay on openrouter, so only the roles that still
    #: need the hosted model pay its latency and quota. ``Settings.provider_for``
    #: is the one place that resolves override-vs-global; nothing else should
    #: re-derive this.
    llm_provider_cheap: str = ""
    llm_provider_good: str = ""
    llm_provider_vision: str = ""
    #: Ollama models and their pinned contexts. num_ctx is a correctness
    #: requirement, not tuning — see llm.py.
    ollama_host: str = "http://localhost:11434"
    model_cheap: str = "qwen3:4b-instruct"
    model_good: str = _DEFAULT_MODEL_GOOD
    model_vision: str = "qwen2.5vl:7b"
    num_ctx_cheap: int = 8192
    num_ctx_good: int = _DEFAULT_NUM_CTX_GOOD
    num_ctx_vision: int = 8192
    #: T222/D197: the pre-render verifier's own local-only model. Pinned
    #: local regardless of ``LLM_PROVIDER``/``LLM_PROVIDER_GOOD`` — the user's
    #: own words were "running locally" — so it is deliberately NOT in
    #: LLM_PROVIDER_ROLES and never reaches ``provider_for``/OpenRouter
    #: (``LLMClient.verify`` calls ``_ollama`` directly; see llm.py). The bare
    #: dataclass default below is the SAME constant ``model_good`` uses two
    #: lines up — not a second hand-typed copy — so the two bare defaults
    #: cannot silently diverge (e.g. if a host's real MODEL_GOOD, read via
    #: ``load()`` below, is a locally-installed model this literal is not —
    #: true on at least one deployed host — ``model_verify`` is exactly as
    #: correct or stale as ``model_good`` already is, never worse).
    #: ``load()`` makes the real inheritance dynamic (defaults to whatever
    #: MODEL_GOOD actually resolved to in the environment, not this
    #: constant); this bare default is only what a caller gets who builds
    #: ``Settings()`` directly, bypassing ``load()`` (tests do; production
    #: never does).
    model_verify: str = _DEFAULT_MODEL_GOOD
    #: Same local-only reasoning as ``model_verify``, same anti-divergence
    #: constant as ``num_ctx_good`` three lines up. T219/D206: the verifier
    #: needs ~16384 like `good`, and sharing the figure means Ollama does not
    #: reload the 4B/9B model between a `good` call and a `verify` call that
    #: both ask for the same num_ctx. Made dynamic in ``load()``, same
    #: caveat as ``model_verify`` above re: this bare literal.
    num_ctx_verify: int = _DEFAULT_NUM_CTX_GOOD
    #: Kill switch for the whole pre-render verifier (T222/T224, D196-D207).
    #: Advisory and off-path when False: compose() calls nothing and writes
    #: nothing to ``items.verification`` (house rule 15 / D205 — it gates
    #: nothing either way, this just controls whether it runs at all).
    verify_enabled: bool = True
    #: Rollback switch for agentic revision (T301, D254). False: a
    #: REVISION_REQUESTED item goes straight to SYNTHESIZED with its note as
    #: ``regen_note`` - the pre-agentic regeneration, unchanged.
    revise_enabled: bool = True
    #: See REVISE_ENGINES_BLOCKED_MAX_S. 0 means give up on the first deferral.
    revise_engines_blocked_max_s: int = REVISE_ENGINES_BLOCKED_MAX_S
    #: Second local model, tried only when the primary local one also fails.
    #: Empty means the chain stops at the primary. Exists because the last
    #: resort should be a model that answers at all, not the best one.
    model_cheap_fallback: str = ""
    model_good_fallback: str = ""
    model_vision_fallback: str = ""
    #: OpenRouter models live in their own variables so switching providers
    #: back and forth never means re-typing model names.
    openrouter_api_key: str = ""
    #: Defaults must be free. A commented-out override silently fell through to
    #: a paid Gemini model and billed every triage and relevance call, so the
    #: safe value is the one you get by forgetting to set anything.
    #: Pinned to a single model, not ``openrouter/free`` (an auto-router — see
    #: ``is_auto_router``), and live-tested to actually enforce
    #: ``response_format: json_schema`` rather than merely advertising support
    #: for it in the models API and then ignoring it at runtime. The previous
    #: default, ``nvidia/nemotron-3.5-lightning:free``, does neither.
    openrouter_model_cheap: str = _DEFAULT_OPENROUTER_MODEL_CHEAP
    openrouter_model_good: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    openrouter_model_vision: str = "google/gemma-4-31b-it:free"

    # --- research ---
    searxng_url: str = "http://localhost:8080"
    #: SearXNG categories to query. "general" alone starves whenever its
    #: scraping engines are rate-limited, which happens under load.
    searxng_categories: str = "general,it,news"
    triage_threshold: int = 6
    #: Handle credited in every caption, e.g. "@aipost".
    source_credit: str = ""
    research_concurrency: int = 3
    #: Which LLMClient role writes the research note (T71). "good" is the
    #: intended destination — the note is the post's substance, so everything
    #: downstream (brief, deck) can only be as specific as it is — but the
    #: only measurement taken so far (D94) showed avg_grounding_ratio -61%
    #: and items_completed 3->2, so this ships defaulted to "cheap" until
    #: T135 re-measures on the bounce-1 code and clears the flip. Do not read
    #: "cheap" as the settled answer.
    #: Quota-neutral (D8): OpenRouter's free tier is one 20 RPM / 1000-day
    #: pool shared account-wide by every ":free" slug, so moving this one
    #: call between tiers spends the same quota either way. Config, not a
    #: hardcoded call site, so the experiment is a one-variable flip and
    #: fully reversible.
    research_note_role: str = "cheap"
    #: How many missed messages a single boot will replay. Keep small on a
    #: first run: every backfilled item costs a full pipeline pass.
    backfill_limit: int = 20
    results_per_query: int = 6
    docs_per_query: int = 3

    # --- media hosting ---
    r2_account_id: str = ""
    r2_access_key: str = ""
    r2_secret_key: str = ""
    r2_bucket: str = ""
    r2_public_base: str = ""
    #: Run a Cloudflare quick tunnel in-process so media URLs always point at
    #: a host that is actually up. Off for a stable base (a named tunnel, a
    #: real bucket), where R2_PUBLIC_BASE is already correct and permanent.
    manage_tunnel: bool = False
    #: Override for any S3-compatible backend (MinIO, B2, S3).
    #: Empty means Cloudflare R2, derived from r2_account_id.
    s3_endpoint: str = ""
    s3_region: str = ""

    # --- instagram ---
    ig_user_id: str = ""
    ig_access_token: str = ""

    # --- runtime ---
    dry_run: bool = True
    db_path: Path = ROOT / "data" / "app.db"
    media_dir: Path = ROOT / "data" / "media"

    # --- retention (T241): the one place every cleanup knob lives ---
    #: Master switch for the worker's periodic cleanup job. scripts/cleanup.py
    #: is unaffected - it is the manual CLI over the same logic.
    retention_enabled: bool = True
    #: Plan and log what would be deleted/rejected, change nothing. Use it to
    #: trust the job before letting it delete.
    retention_dry_run: bool = False
    #: An item that has sat in awaiting_approval this long is auto-rejected
    #: and its files deleted (the DB row is kept). 0 disables the rule.
    #: Editorial limit (the operator's call, 2026-11-01), not a platform one.
    awaiting_approval_max_days: int = RETENTION_AWAITING_APPROVAL_MAX_DAYS
    #: Seconds between cleanup runs.
    cleanup_interval_s: int = RETENTION_CLEANUP_INTERVAL_S
    #: Days a published/rejected/dropped item keeps its files after entering
    #: that state, so the dashboard can still show what went out.
    terminal_media_grace_days: int = RETENTION_TERMINAL_GRACE_DAYS
    #: Failed items can be requeued, and a requeue to ingested re-runs extract
    #: on the ORIGINAL downloaded media - so a failed item keeps every file for
    #: this long before it is treated like the other terminals.
    failed_media_grace_days: int = RETENTION_FAILED_GRACE_DAYS
    #: Delete a published item's local media/renders (the IG post is live).
    cleanup_published_media: bool = True
    #: Delete the item's objects in the media bucket (items/<id>/). Published
    #: items only once ig_post_id is recorded; never while approved/publishing.
    cleanup_minio: bool = True
    template_dir: Path = ROOT / "templates"
    theme_path: Path = ROOT / "config" / "theme.json"
    #: Theme name from config/themes/, or "rotate" to cycle per item.
    theme: str = "signal"
    #: On a sustained OpenRouter rate limit, use the local Ollama model for
    #: that call rather than stalling the item behind shared free-tier load.
    fallback_to_local: bool = True
    #: Which requests get a worked example deck: "off", "list" (enumerations
    #: only) or "all".
    #:
    #: Scoped rather than global because the golden-set run split cleanly by
    #: intent: examples took enumeration from 2/4 composed to 4/4 and produced
    #: the first links index the scorer has ever seen, while news went 10/10 to
    #: 8/10. A single switch forces one of those on the other.
    #: Legacy "true"/"false" still parse, to "all" and "off".
    few_shot_examples: str = "list"
    #: Handle printed on every slide. Overrides the theme file, so it
    #: lives in one place rather than being duplicated per theme.
    handle: str = ""
    max_attempts: int = 3
    poll_interval_s: float = 5.0
    #: Loopback by default — approving an item in the dashboard publishes it,
    #: and there is no login (see scripts/dashboard.py's module docstring).
    #: The only legitimate reason to override this is a container's internal
    #: bind, where Docker cannot route a published port to a process bound to
    #: the container's own loopback; the host-side port publish must stay
    #: loopback-restricted regardless (see docker-compose.yml's `dashboard`
    #: service). Never set this to 0.0.0.0 for a bare host run.
    dashboard_host: str = "127.0.0.1"

    #: Variables without which the process cannot meaningfully run.
    REQUIRED: ClassVar[tuple[str, ...]] = (
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "TELEGRAM_BOT_TOKEN",
        "OPERATOR_USER_ID",
        "CHANNEL_IDS",
    )

    #: Additionally required before anything can actually be published.
    REQUIRED_FOR_PUBLISH: ClassVar[tuple[str, ...]] = (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY",
        "R2_SECRET_KEY",
        "R2_BUCKET",
        "R2_PUBLIC_BASE",
        "IG_USER_ID",
        "IG_ACCESS_TOKEN",
    )

    @classmethod
    def load(cls, env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ if env is None else env)
        session = e.get("TELEGRAM_SESSION_PATH")
        # T222/D197: computed ahead of the cls(...) call below (rather than
        # re-read from the kwarg) so model_verify/num_ctx_verify can inherit
        # the REAL resolved value of MODEL_GOOD/NUM_CTX_GOOD — "the defaults
        # inherit from good" means dynamically, not a second copy of
        # _DEFAULT_MODEL_GOOD/_DEFAULT_NUM_CTX_GOOD that would go stale the
        # moment someone sets MODEL_GOOD without also touching this.
        model_good = e.get("MODEL_GOOD", _DEFAULT_MODEL_GOOD)
        num_ctx_good = _int(e.get("NUM_CTX_GOOD"), _DEFAULT_NUM_CTX_GOOD)
        return cls(
            telegram_api_id=_int(e.get("TELEGRAM_API_ID"), 0),
            telegram_api_hash=e.get("TELEGRAM_API_HASH", ""),
            telegram_bot_token=e.get("TELEGRAM_BOT_TOKEN", ""),
            operator_user_id=_int(e.get("OPERATOR_USER_ID"), 0),
            channel_ids=_ints(e.get("CHANNEL_IDS")),
            session_path=Path(session) if session else ROOT / "secrets" / "telegram.session",
            llm_provider=e.get("LLM_PROVIDER", "ollama").strip().lower(),
            llm_provider_cheap=e.get("LLM_PROVIDER_CHEAP", "").strip().lower(),
            llm_provider_good=e.get("LLM_PROVIDER_GOOD", "").strip().lower(),
            llm_provider_vision=e.get("LLM_PROVIDER_VISION", "").strip().lower(),
            ollama_host=e.get("OLLAMA_HOST", "http://localhost:11434"),
            model_cheap=e.get("MODEL_CHEAP", "qwen3:4b-instruct"),
            model_good=model_good,
            model_vision=e.get("MODEL_VISION", "qwen2.5vl:7b"),
            # T222/D197: local-only, never routed through LLM_PROVIDER/
            # LLM_PROVIDER_GOOD — see LLMClient.verify. Unset MODEL_VERIFY/
            # NUM_CTX_VERIFY inherit whatever model_good/num_ctx_good
            # actually resolved to, above.
            model_verify=e.get("MODEL_VERIFY", "") or model_good,
            num_ctx_verify=_int(e.get("NUM_CTX_VERIFY"), num_ctx_good),
            verify_enabled=_bool(e.get("VERIFY"), True),
            revise_enabled=_bool(e.get("REVISE_ENABLED"), True),
            revise_engines_blocked_max_s=max(0, _int(
                e.get("REVISE_ENGINES_BLOCKED_MAX_S"), REVISE_ENGINES_BLOCKED_MAX_S)),
            num_ctx_cheap=_int(e.get("NUM_CTX_CHEAP"), 8192),
            num_ctx_good=num_ctx_good,
            num_ctx_vision=_int(e.get("NUM_CTX_VISION"), 8192),
            model_cheap_fallback=e.get("MODEL_CHEAP_FALLBACK", ""),
            model_good_fallback=e.get("MODEL_GOOD_FALLBACK", ""),
            model_vision_fallback=e.get("MODEL_VISION_FALLBACK", ""),
            openrouter_api_key=e.get("OPENROUTER_API_KEY", ""),
            openrouter_model_cheap=e.get(
                "OPENROUTER_MODEL_CHEAP", _DEFAULT_OPENROUTER_MODEL_CHEAP),
            openrouter_model_good=e.get(
                "OPENROUTER_MODEL_GOOD", "nvidia/nemotron-3-ultra-550b-a55b:free"),
            openrouter_model_vision=e.get(
                "OPENROUTER_MODEL_VISION", "google/gemma-4-31b-it:free"),
            searxng_url=e.get("SEARXNG_URL", "http://localhost:8080"),
            searxng_categories=e.get("SEARXNG_CATEGORIES", "general,it,news"),
            triage_threshold=_int(e.get("TRIAGE_THRESHOLD"), 6),
            source_credit=e.get("SOURCE_CREDIT", ""),
            research_concurrency=_int(e.get("RESEARCH_CONCURRENCY"), 3),
            research_note_role=e.get("RESEARCH_NOTE_ROLE", "cheap").strip().lower(),
            backfill_limit=_int(e.get("BACKFILL_LIMIT"), 20),
            r2_account_id=e.get("R2_ACCOUNT_ID", ""),
            r2_access_key=e.get("R2_ACCESS_KEY", ""),
            r2_secret_key=e.get("R2_SECRET_KEY", ""),
            r2_bucket=e.get("R2_BUCKET", ""),
            r2_public_base=e.get("R2_PUBLIC_BASE", "").rstrip("/"),
            manage_tunnel=e.get("MANAGE_TUNNEL", "").strip().lower()
            in ("1", "true", "yes", "on"),
            s3_endpoint=e.get("S3_ENDPOINT", ""),
            s3_region=e.get("S3_REGION", ""),
            ig_user_id=e.get("IG_USER_ID", ""),
            ig_access_token=e.get("IG_ACCESS_TOKEN", ""),
            theme=e.get("THEME", "signal"),
            handle=e.get("HANDLE", ""),
            few_shot_examples=_few_shot_mode(
                e.get("FEW_SHOT_EXAMPLES"), default="list"),
            fallback_to_local=_bool(e.get("FALLBACK_TO_LOCAL"), True),
            dashboard_host=e.get("DASHBOARD_HOST", "127.0.0.1"),
            dry_run=_bool(e.get("DRY_RUN"), True),
            retention_enabled=_bool(e.get("RETENTION_ENABLED"), True),
            retention_dry_run=_bool(e.get("RETENTION_DRY_RUN"), False),
            awaiting_approval_max_days=max(0, _int(
                e.get("AWAITING_APPROVAL_MAX_DAYS"),
                RETENTION_AWAITING_APPROVAL_MAX_DAYS)),
            cleanup_interval_s=max(MIN_CLEANUP_INTERVAL_S, _int(
                e.get("CLEANUP_INTERVAL_S"), RETENTION_CLEANUP_INTERVAL_S)),
            terminal_media_grace_days=max(0, _int(
                e.get("TERMINAL_MEDIA_GRACE_DAYS"), RETENTION_TERMINAL_GRACE_DAYS)),
            failed_media_grace_days=max(0, _int(
                e.get("FAILED_MEDIA_GRACE_DAYS"), RETENTION_FAILED_GRACE_DAYS)),
            cleanup_published_media=_bool(e.get("CLEANUP_PUBLISHED_MEDIA"), True),
            cleanup_minio=_bool(e.get("CLEANUP_MINIO"), True),
            db_path=Path(e["DB_PATH"]) if e.get("DB_PATH") else ROOT / "data" / "app.db",
        )

    def provider_for(self, role: str) -> str:
        """The effective inference backend for ``role`` (T216).

        Honours a per-role override (``LLM_PROVIDER_CHEAP`` /
        ``LLM_PROVIDER_GOOD`` / ``LLM_PROVIDER_VISION``) when it is set;
        otherwise falls back to the global ``llm_provider`` — the exact
        pre-T216 behaviour when no override is configured. This is the ONE
        place the provider-per-role decision is made: ``llm.py``'s
        ``_model``/``_chat`` and ``budget.py``'s ``budget_tokens_for_role``
        both call this rather than keeping a second copy of the choice.
        """
        override = {
            "cheap": self.llm_provider_cheap,
            "good": self.llm_provider_good,
            "vision": self.llm_provider_vision,
        }.get(role, "")
        return override or self.llm_provider

    def validate_model_config(self) -> None:
        """Fail fast on a provider/model config that cannot produce a
        reproducible, non-billing model call.

        Deliberately narrower than ``validate_for_run()``: it is everything a
        tool that only calls a model — the eval harness, ``smoke.py``,
        ``build_golden.py``, the dashboard — needs checked, and nothing a
        model-only tool has no business requiring. ``smoke.py`` is documented
        to run with "no Telegram, no Instagram" *before wiring up any
        accounts*, so gating it on ``Settings.REQUIRED`` (Telegram vars) would
        make the documented workflow impossible to run — that is exactly the
        mistake this split avoids (T85). Takes no ``env`` because every check
        here reads already-resolved ``self.*`` fields, never the environment
        directly.

        ``validate_for_run()`` calls this too, so the full service still gets
        every check this one makes; nothing here is a weaker version of that
        one, only a narrower slice of it (T28's and T69's checks fire exactly
        as before — see test_config.py).
        """
        if self.llm_provider not in LLM_PROVIDERS:
            raise MissingConfig(
                f"LLM_PROVIDER={self.llm_provider!r} is not a known provider. "
                f"Use one of: {', '.join(LLM_PROVIDERS)}."
            )
        # T216: a per-role override is checked against the same allow-list as
        # the global variable — an unknown value here would otherwise only
        # surface the first time that role's provider_for() result reaches
        # LLMClient, as a KeyError deep inside _model/_chat instead of a
        # MissingConfig at startup.
        bad_overrides = {
            name: value
            for name, value in (
                ("LLM_PROVIDER_CHEAP", self.llm_provider_cheap),
                ("LLM_PROVIDER_GOOD", self.llm_provider_good),
                ("LLM_PROVIDER_VISION", self.llm_provider_vision),
            )
            if value and value not in LLM_PROVIDERS
        }
        if bad_overrides:
            listed = ", ".join(f"{n}={v!r}" for n, v in sorted(bad_overrides.items()))
            raise MissingConfig(
                f"{listed} is not a known provider. Use one of: "
                f"{', '.join(LLM_PROVIDERS)}."
            )
        # T71: an unroutable role would reach research.py's
        # getattr(llm, settings.research_note_role) and fail as an
        # unclassified TypeError (LLMClient.vision takes a required
        # image_paths argument the note call never supplies) instead of the
        # taxonomy in errors.py — fail fast here instead.
        if self.research_note_role not in RESEARCH_NOTE_ROLES:
            raise MissingConfig(
                f"RESEARCH_NOTE_ROLE={self.research_note_role!r} is not "
                f"valid. Use one of: {', '.join(RESEARCH_NOTE_ROLES)}."
            )
        # T216: each of cheap/good/vision can resolve to a different
        # provider now (see provider_for) — the key and the per-model slug
        # checks below must bind to whichever roles ACTUALLY reach
        # openrouter, not to the global variable. A role that resolves to
        # ollama must not be forced to carry a key or a validated slug it
        # never uses, and a role that resolves to openrouter must still be
        # checked even if LLM_PROVIDER itself is "ollama".
        openrouter_roles = tuple(
            role
            for role in LLM_PROVIDER_ROLES
            if self.provider_for(role) == "openrouter"
        )
        # Without a key every model call would 401, and a 401 is Terminal — the
        # whole queue would fail permanently one item at a time. Fail here.
        if openrouter_roles and not self.openrouter_api_key:
            raise MissingConfig(
                f"role(s) {', '.join(openrouter_roles)} resolve to "
                "LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is not set."
            )
        if openrouter_roles:
            slug_for_role = {
                "cheap": ("OPENROUTER_MODEL_CHEAP", self.openrouter_model_cheap),
                "good": ("OPENROUTER_MODEL_GOOD", self.openrouter_model_good),
                "vision": ("OPENROUTER_MODEL_VISION", self.openrouter_model_vision),
            }
            configured = tuple(slug_for_role[role] for role in openrouter_roles)
            # An auto-router serves a different upstream model on every call —
            # unknown context window, unknown instruction-following, and a
            # model that silently ignores response_format looks identical to
            # one that honours it until the completion comes back as prose.
            # That makes every run irreproducible, which is a different defect
            # from cost, so it is checked and reported separately: a paid slug
            # and an auto-routed one must not be confused for the same mistake.
            routed = {name: slug for name, slug in configured if is_auto_router(slug)}
            if routed:
                listed = ", ".join(f"{n}={s!r}" for n, s in sorted(routed.items()))
                raise MissingConfig(
                    "OpenRouter auto-router slugs are not allowed: "
                    f"{listed}. An auto-router serves a different model on "
                    "every call, so no run is reproducible and no A/B result "
                    "can be trusted — pin one specific ':free' model slug "
                    "instead (browse them at "
                    "https://openrouter.ai/models?max_price=0)."
                )
            # Refuse to start rather than bill. A paid model reaches production
            # by omission, not by decision — an unset variable used to fall
            # through to a priced default and nothing said so until the
            # invoice.
            paid = {
                name: slug for name, slug in configured if not is_free_model(slug)
            }
            if paid:
                listed = ", ".join(f"{n}={s!r}" for n, s in sorted(paid.items()))
                raise MissingConfig(
                    "Only free OpenRouter models are allowed, but these are not "
                    f"free: {listed}. Use a slug ending in ':free' (browse them "
                    "at https://openrouter.ai/models?max_price=0)."
                )

    def validate_for_run(self, env: dict[str, str] | None = None) -> None:
        e = dict(os.environ if env is None else env)
        missing = [k for k in self.REQUIRED if not e.get(k)]
        if missing:
            raise MissingConfig(
                "Missing required environment variables: "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill them in."
            )
        self.validate_model_config()
        if not self.dry_run:
            missing_pub = [k for k in self.REQUIRED_FOR_PUBLISH if not e.get(k)]
            if missing_pub:
                raise MissingConfig(
                    "DRY_RUN is false but publishing is not configured. Missing: "
                    + ", ".join(missing_pub)
                )
