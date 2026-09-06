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
    #: OpenRouter models live in their own variables so switching providers
    #: back and forth never means re-typing model names.
    openrouter_api_key: str = ""
    openrouter_model_cheap: str = "google/gemini-2.5-flash-lite"
    openrouter_model_good: str = "google/gemini-2.5-flash"
    openrouter_model_vision: str = "google/gemini-2.5-flash"

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
            openrouter_api_key=e.get("OPENROUTER_API_KEY", ""),
            openrouter_model_cheap=e.get(
                "OPENROUTER_MODEL_CHEAP", "google/gemini-2.5-flash-lite"),
            openrouter_model_good=e.get(
                "OPENROUTER_MODEL_GOOD", "google/gemini-2.5-flash"),
            openrouter_model_vision=e.get(
                "OPENROUTER_MODEL_VISION", "google/gemini-2.5-flash"),
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
        if not self.dry_run:
            missing_pub = [k for k in self.REQUIRED_FOR_PUBLISH if not e.get(k)]
            if missing_pub:
                raise MissingConfig(
                    "DRY_RUN is false but publishing is not configured. Missing: "
                    + ", ".join(missing_pub)
                )
