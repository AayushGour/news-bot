"""Environment -> typed settings.

Settings load with permissive defaults so tests and offline development need no
credentials. ``validate_for_run()`` is what fails fast, and it is called only by
``__main__`` — the process refuses to start with a half-configured environment
rather than discovering it three stages into a pipeline run.
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

#: OpenRouter marks zero-cost models with a ``:free`` suffix. These aliases are
#: free without carrying it — verified against the models API, prompt and
#: completion both priced at 0.
FREE_MODEL_ALIASES = frozenset({"openrouter/free"})


#: How widely worked examples are shown to the composer.
FEW_SHOT_MODES = ("off", "list", "all")


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
    #: Ollama models and their pinned contexts. num_ctx is a correctness
    #: requirement, not tuning — see llm.py.
    ollama_host: str = "http://localhost:11434"
    model_cheap: str = "qwen3:4b-instruct"
    model_good: str = "qwen3.5:9b"
    model_vision: str = "qwen2.5vl:7b"
    num_ctx_cheap: int = 8192
    num_ctx_good: int = 16384
    num_ctx_vision: int = 8192
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
    openrouter_model_cheap: str = "nvidia/nemotron-3.5-lightning:free"
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
        return cls(
            telegram_api_id=_int(e.get("TELEGRAM_API_ID"), 0),
            telegram_api_hash=e.get("TELEGRAM_API_HASH", ""),
            telegram_bot_token=e.get("TELEGRAM_BOT_TOKEN", ""),
            operator_user_id=_int(e.get("OPERATOR_USER_ID"), 0),
            channel_ids=_ints(e.get("CHANNEL_IDS")),
            session_path=Path(session) if session else ROOT / "secrets" / "telegram.session",
            llm_provider=e.get("LLM_PROVIDER", "ollama").strip().lower(),
            ollama_host=e.get("OLLAMA_HOST", "http://localhost:11434"),
            model_cheap=e.get("MODEL_CHEAP", "qwen3:4b-instruct"),
            model_good=e.get("MODEL_GOOD", "qwen3.5:9b"),
            model_vision=e.get("MODEL_VISION", "qwen2.5vl:7b"),
            num_ctx_cheap=_int(e.get("NUM_CTX_CHEAP"), 8192),
            num_ctx_good=_int(e.get("NUM_CTX_GOOD"), 16384),
            num_ctx_vision=_int(e.get("NUM_CTX_VISION"), 8192),
            model_cheap_fallback=e.get("MODEL_CHEAP_FALLBACK", ""),
            model_good_fallback=e.get("MODEL_GOOD_FALLBACK", ""),
            model_vision_fallback=e.get("MODEL_VISION_FALLBACK", ""),
            openrouter_api_key=e.get("OPENROUTER_API_KEY", ""),
            openrouter_model_cheap=e.get(
                "OPENROUTER_MODEL_CHEAP", "nvidia/nemotron-3.5-lightning:free"),
            openrouter_model_good=e.get(
                "OPENROUTER_MODEL_GOOD", "nvidia/nemotron-3-ultra-550b-a55b:free"),
            openrouter_model_vision=e.get(
                "OPENROUTER_MODEL_VISION", "google/gemma-4-31b-it:free"),
            searxng_url=e.get("SEARXNG_URL", "http://localhost:8080"),
            searxng_categories=e.get("SEARXNG_CATEGORIES", "general,it,news"),
            triage_threshold=_int(e.get("TRIAGE_THRESHOLD"), 6),
            source_credit=e.get("SOURCE_CREDIT", ""),
            research_concurrency=_int(e.get("RESEARCH_CONCURRENCY"), 3),
            backfill_limit=_int(e.get("BACKFILL_LIMIT"), 20),
            r2_account_id=e.get("R2_ACCOUNT_ID", ""),
            r2_access_key=e.get("R2_ACCESS_KEY", ""),
            r2_secret_key=e.get("R2_SECRET_KEY", ""),
            r2_bucket=e.get("R2_BUCKET", ""),
            r2_public_base=e.get("R2_PUBLIC_BASE", "").rstrip("/"),
            s3_endpoint=e.get("S3_ENDPOINT", ""),
            s3_region=e.get("S3_REGION", ""),
            ig_user_id=e.get("IG_USER_ID", ""),
            ig_access_token=e.get("IG_ACCESS_TOKEN", ""),
            theme=e.get("THEME", "signal"),
            handle=e.get("HANDLE", ""),
            few_shot_examples=_few_shot_mode(
                e.get("FEW_SHOT_EXAMPLES"), default="list"),
            fallback_to_local=_bool(e.get("FALLBACK_TO_LOCAL"), True),
            dry_run=_bool(e.get("DRY_RUN"), True),
            db_path=Path(e["DB_PATH"]) if e.get("DB_PATH") else ROOT / "data" / "app.db",
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
        if self.llm_provider not in LLM_PROVIDERS:
            raise MissingConfig(
                f"LLM_PROVIDER={self.llm_provider!r} is not a known provider. "
                f"Use one of: {', '.join(LLM_PROVIDERS)}."
            )
        # Without a key every model call would 401, and a 401 is Terminal — the
        # whole queue would fail permanently one item at a time. Fail here.
        if self.llm_provider == "openrouter" and not self.openrouter_api_key:
            raise MissingConfig(
                "LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is not set."
            )
        # Refuse to start rather than bill. A paid model reaches production by
        # omission, not by decision — an unset variable used to fall through to
        # a priced default and nothing said so until the invoice.
        if self.llm_provider == "openrouter":
            paid = {
                name: slug
                for name, slug in (
                    ("OPENROUTER_MODEL_CHEAP", self.openrouter_model_cheap),
                    ("OPENROUTER_MODEL_GOOD", self.openrouter_model_good),
                    ("OPENROUTER_MODEL_VISION", self.openrouter_model_vision),
                )
                if not is_free_model(slug)
            }
            if paid:
                listed = ", ".join(f"{n}={s!r}" for n, s in sorted(paid.items()))
                raise MissingConfig(
                    "Only free OpenRouter models are allowed, but these are not "
                    f"free: {listed}. Use a slug ending in ':free' (browse them "
                    "at https://openrouter.ai/models?max_price=0)."
                )
        if not self.dry_run:
            missing_pub = [k for k in self.REQUIRED_FOR_PUBLISH if not e.get(k)]
            if missing_pub:
                raise MissingConfig(
                    "DRY_RUN is false but publishing is not configured. Missing: "
                    + ", ".join(missing_pub)
                )
