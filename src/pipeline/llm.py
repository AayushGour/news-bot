"""Chat client for the pipeline's two inference backends.

Ollama (local, the default) and OpenRouter (hosted, OpenAI-compatible) live
behind one class because every stage calls exactly ``cheap`` / ``good`` /
``vision``, and the test suite swaps the whole thing for ``FakeLLM`` with the
same three methods. Adding a provider must never change that surface.

T216: which provider actually serves a given call is resolved PER ROLE, via
``Settings.provider_for(role)`` — not once per client. ``LLM_PROVIDER`` is
still the global default; ``LLM_PROVIDER_CHEAP`` / ``_GOOD`` / ``_VISION``
let one role override it (e.g. ``good`` pinned to ``ollama`` while ``cheap``
and ``vision`` stay on ``openrouter``). ``_model`` and ``_chat`` below call
``provider_for`` directly; nothing in this module keeps a second copy of
that decision.

Every Ollama call pins ``num_ctx``. This is not tuning: unpinned, Ollama 0.33
loads a model at its full declared context (262144 for qwen3:4b-instruct),
reports ~43GB of allocation and spills to CPU, making the machine unusable.
Pinned to 8192 the same model is 3.9GB and fully GPU-resident. ``num_ctx`` is
an Ollama option with no OpenAI-API equivalent, so it is never sent there.

Failure classification is the other thing that must not drift between the two
paths: the worker defers a ``Retryforever`` without spending one of the item's
three attempts, and gives up permanently on a ``Terminal``. Mapping a rate
limit or a dead upstream to the wrong class turns a ten-minute outage into a
queue of permanently failed items.

A reasoning model can also file its whole answer in an out-of-band field
instead of the one the chat contract defines — OpenRouter's
``message.reasoning``, Ollama's ``message.thinking`` — leaving ``content``
empty. The field shapes are confirmed live (T96, ~25 reps against the
configured ``OPENROUTER_MODEL_GOOD``): a sibling ``reasoning`` /
``reasoning_details`` carrying real chain-of-thought text is real, and those
reps did NOT reproduce ``content == ""``. The "roughly half the time"
frequency claim is inherited from a DIFFERENT experiment, not measured here:
T78's reviewer saw ``content == ""`` on 4 of 8 reps, but only in that
experiment's Arm A — a deliberately weak prompt. The 10 reps run there under
the real production prompt (Arm B 5/5, Arm C 4/5) showed ZERO empty-content
responses, so how often this fires on the actual production path is
UNMEASURED. That is the model answering in the wrong place, not the model
failing, so ``_answer_text`` recovers it — logged at WARNING so the
frequency stays visible, since that log line is the only instrument that
will ever measure the real rate — without touching the
empty-completion/unparseable-JSON classification below it: a recovered
field that still yields nothing usable is exactly as bad as no field at
all, and falls through to the same ``BadCompletion``. The Ollama side of
the guard is unmeasured for a different reason: ``_ollama`` always sends
``"think": False`` (see below), so ``message.thinking`` recovery is expected
to be latent on today's models — kept for a model that ignores the flag, or
a future one that changes it, not because it is known to fire now.

``parse_json`` only proves a completion is *valid JSON*, never that it has
the shape a caller's schema asked for. No provider on this tier reliably
enforces one (T81, D27, 19 live reps): OpenRouter accepts
``response_format: {"strict": true}`` and still returns field names nowhere
in the schema, Ollama ignores a top-level ``oneOf`` outright (D18). Left
unchecked, well-formed JSON of the wrong shape used to flow straight through
the retry ladder and fail later as a missing key — or silently, as a dropped
slide in ``normalise_slides``. ``_finish`` therefore validates the parsed
completion against the schema it was actually sent and logs any divergence
at WARNING, naming the specific field(s) involved (``_log_schema_divergence``
below) — but this is diagnostic only: it never raises, and it never changes
what is returned or how a failure downstream gets classified. Under
production conditions the violation rate measured 0/5, and a shape a
provider gets wrong already fails safe further down the pipeline
(``normalise_slides`` -> ``Retryable("0 usable slides")``); turning this into
a second gate would take a single bad-prompt day from degraded-but-shipping
to failing every item, which is worse than the defect it would catch.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .config import NUM_CTX_FIELD_FOR_ROLE, Settings
from .faults import OnceAlerter
from .errors import (
    BadCompletion,
    ConfigFault,
    RateLimited,
    Retryable,
    Retryforever,
    Terminal,
)

try:
    import jsonschema
    from jsonschema.exceptions import relevance as _schema_error_relevance
except ImportError:  # pragma: no cover - jsonschema is a runtime dependency
    # (promoted from dev in T331); T81 needs the real thing in production, since a
    # hand-rolled reimplementation is exactly the D19 mistake — structurally
    # blind to value types and `oneOf` exclusivity, one bug already missed
    # by construction. This guard is what keeps a bare `import pipeline.llm` from
    # crashing the whole pipeline on startup: a missing validator must be no
    # worse than the validator not existing (AC 4's principle extended to
    # the import itself), never a new way for every item to fail.
    jsonschema = None  # type: ignore[assignment]
    _schema_error_relevance = None  # type: ignore[assignment]

#: T81 bounce 1 (B1): flips once the first time a schema-divergence check is
#: skipped because ``jsonschema`` itself is unavailable, so the WARNING below
#: fires once per process, not once per call. Without this the import guard
#: above degrades to a permanently silent no-op if the
#: package is ever missing from an environment — ``jsonschema`` is a runtime
#: dependency in ``pyproject.toml`` (T331), so this is a broken-install
#: backstop, not the normal path — and an operator grepping for
#: "diverges from the schema" and finding nothing could not tell "no
#: divergences" from "no validator" apart.
_warned_missing_jsonschema = False

#: Caches the compiled validator per schema object identity. ``check_schema``
#: re-validates the SCHEMA ITSELF against the JSON Schema metaschema on every
#: call — measured 13.73ms of a 13.87ms end-to-end cost against
#: ``SLIDES_SCHEMA`` (the actual instance validation is 0.11ms); at ~25 calls
#: per item that is ~0.35s/item of pure waste re-proving the same constant
#: schema is well-formed. Keyed by ``id(schema)`` with the schema itself held
#: alongside it (not just its id) so the cache can never serve a stale
#: validator to an unrelated object that happens to be allocated at a
#: previously-used id once the original schema is garbage collected — safe
#: because every real caller (``SLIDES_SCHEMA``, ``SLIDES_SCHEMA_FLAT``,
#: ``NOTE_SCHEMA``, ``RELEVANCE_SCHEMA``) is a module-level constant built
#: once at import time, never rebuilt per call.
_validator_cache: dict[int, tuple[dict, Any]] = {}


def _validator_for(schema: dict) -> Any:
    """The compiled validator for ``schema``, computed at most once."""
    cached = _validator_cache.get(id(schema))
    if cached is not None and cached[0] is schema:
        return cached[1]
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    _validator_cache[id(schema)] = (schema, validator)
    return validator


#: How many times to try OpenRouter before falling back to the local model.
OPENROUTER_ATTEMPTS = 3
#: Multiplied by the attempt number, so waits are 5s then 10s.
OPENROUTER_BACKOFF_S = 5

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

#: Hard ceiling on one model call, wall-clock.
#:
#: httpx applies its ``timeout`` to connect, read and write individually, and
#: the read timeout measures the gap BETWEEN bytes — not how long the whole
#: response takes. A provider trickling one token a minute never trips it, so a
#: 900s read timeout allowed a single compose to run 983s and would have
#: allowed it to run forever. Generous, because a large local model on CPU is
#: legitimately slow; finite, because "no cap at all" is how an item hangs.
CALL_DEADLINE_S = 900

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

#: OpenRouter rejects ``response_format: json_schema`` for models — or for the
#: upstream provider it happened to route to — that do not implement it, and
#: says so by name. Matching the message is what keeps the json_object fallback
#: narrow: a blind retry would also paper over genuinely malformed requests.
log = logging.getLogger(__name__)

_SCHEMA_UNSUPPORTED = re.compile(
    r"json[_ ]?schema|response_format|structured[_ ]?output", re.IGNORECASE
)

#: 4xx codes that no retry can fix: a wrong or revoked key, an exhausted
#: balance, a key without access to the model. These need a human, so they must
#: not sit in the retry queue pretending to be transient.
_TERMINAL_STATUSES = frozenset({401, 402, 403})

#: OpenRouter's wording when the failure is the upstream model provider's, not
#: ours. Our own bad requests ("context length exceeded", "bad request") come
#: back without this envelope, and must keep failing loudly rather than
#: quietly degrading to a weaker local model.
_PROVIDER_FAULT = re.compile(
    r"provider returned error|\bmetadata\b.{0,40}\braw\b", re.IGNORECASE | re.DOTALL
)


#: OpenRouter's wording when the configured slug itself is gone or malformed
#: (D210: a free slug turned paid overnight and 404'd every call). The model
#: is a config choice, so this is a ConfigFault, not the item's problem. A
#: "No endpoints found that support json_schema" body is the separate
#: capability case handled by the degrade branch, hence the exclusion.
_MODEL_GONE = re.compile(
    r"no endpoints found|is not a valid model|unavailable for free", re.IGNORECASE
)


def _is_model_gone(status: int, body: str) -> bool:
    return (
        status in (400, 404)
        and bool(_MODEL_GONE.search(body or ""))
        and not _SCHEMA_UNSUPPORTED.search(body or "")
    )


def _is_provider_fault(body: str) -> bool:
    return bool(_PROVIDER_FAULT.search(body or ""))


def _answer_text(
    message: Any, primary: str, fallback: str, source: str,
) -> tuple[Any, str | None]:
    """Prefer ``message[primary]``; recover ``message[fallback]`` if it is empty.

    A reasoning model can file its whole answer in an out-of-band field and
    leave ``content`` empty (see the module docstring, T96). ``fallback`` is
    only ever consulted when ``primary`` is missing or blank — a non-empty
    ``content`` is always used as-is, so a model that also emits normal
    chain-of-thought text alongside a real answer is untouched by this.

    ``message`` is typed ``Any``, not ``dict``, on purpose: a 200 whose
    ``choices[0].message`` (or Ollama's ``message``) is itself ``None``, a
    bare string, a number or a list is a real shape a provider can return,
    and calling ``.get`` on it would raise ``AttributeError`` instead of the
    ``BadCompletion`` every other unusable shape gets — exactly the
    classification drift the module docstring forbids. Not a dict at all
    means neither field can be read, so this returns "no answer" rather than
    raising.

    Returns ``(text, recovered_field)``. ``text`` is ``None`` (never raises)
    when neither field is usable, so the caller's own "empty completion"
    handling stays the one place that decides what a dead response means —
    this only widens what counts as an answer. ``recovered_field`` is
    ``None`` when ``primary`` supplied the answer (or nothing did), and is
    ``fallback``'s name when the out-of-band field had to be used instead —
    the recovered value is coerced to ``str`` in that case, since the field
    is not contractually text (a parts array is a real shape) and a non-str
    would otherwise throw downstream where callers assume a string. A later
    failure (e.g. the recovered text failing schema validation) can then name
    the field in its own message instead of only in the WARNING logged here
    — one grep instead of two correlated by log-line adjacency.
    """
    if not isinstance(message, dict):
        return None, None
    value = message.get(primary)
    if value is not None and str(value).strip():
        return value, None
    recovered = message.get(fallback)
    if recovered is not None and str(recovered).strip():
        log.warning(
            "%s message.%s was empty; using message.%s instead (%d chars) — "
            "the model answered, just in the wrong field",
            source, primary, fallback, len(str(recovered).strip()),
        )
        return str(recovered), fallback
    return None, None


_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def parse_json(raw: str) -> Any:
    """Parse a model's JSON reply, tolerating a stray markdown fence."""
    cleaned = _FENCE.sub("", (raw or "").strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise BadCompletion(
            f"model returned unparseable JSON: {cleaned[:200]!r}"
        ) from exc


def _divergence_detail(error: Any) -> str:
    """The most specific description available for one schema failure.

    A ``oneOf``/``anyOf`` failure (SLIDES_SCHEMA's 15-arm discriminated
    union, D18/D27) carries one sub-error per failed arm in ``.context``,
    and ``error.message`` alone is just "... is not valid under any of the
    given schemas" — true, but names nothing a human can act on. Every arm
    typically fails on the *same* instance-level problem (an unexpected
    field, a missing one), so de-duplicating ``.context``'s own messages
    recovers that shared, specific divergence instead of the union's
    generic wrapper — e.g. "Additional properties are not allowed
    ('subheadline' was unexpected)" survives being deduped across all 15
    failed arms, because it is true of every one of them.
    """
    if not error.context:
        return error.message
    seen: dict[str, None] = {}
    for sub in error.context:
        seen.setdefault(sub.message, None)
    return "; ".join(seen)


def _log_schema_divergence(parsed: Any, schema: dict | None, *, source: str) -> None:
    """Diagnose, never gate: log how ``parsed`` diverges from ``schema`` (T81).

    No provider on this tier reliably enforces the schema it is sent — see
    the module docstring (D18, D27). This function's only job is to make
    that visible: it logs the specific divergence at WARNING and otherwise
    does nothing. It never raises past its own boundary and never changes
    what ``_finish`` returns — a completion that fails this check is still
    returned exactly as parsed, because a shape violation already fails
    safe downstream (``normalise_slides`` -> ``Retryable``), and turning
    this into a second gate would take a single bad-prompt day from
    degraded-but-shipping to failing every item (D27's design constraint).

    Picks the top-level error by ``jsonschema.exceptions.relevance`` rather
    than the higher-level ``best_match`` helper on purpose: ``best_match``
    descends into a ``oneOf``/``anyOf`` error's ``.context`` on its own,
    landing on one arbitrary arm's leaf message (e.g. a confusing discriminator
    mismatch against an unrelated arm) whenever the descent is not exactly
    tied — inconsistent across arm counts, confirmed live against
    SLIDES_SCHEMA-shaped fixtures. Stopping at the top-level error and doing
    the ``.context`` dedup ourselves (``_divergence_detail``) is what
    actually names the shared, specific divergence.

    Also never raises on a pathological ``schema`` (or a ``parsed`` value
    the validator chokes on) — AC 4: a validator that can crash is worse
    than no validator, and this is diagnostic tooling, not a code path
    anything downstream depends on.

    Only the single shallowest error is logged (see ``max(..., key=
    _schema_error_relevance)`` below), so a root-level miss can mask a
    genuine slide-level divergence in the same completion — a deck missing
    ``theme`` *and* carrying an unexpected field on a slide logs only the
    ``theme`` problem, not both.
    """
    if schema is None:
        return
    if jsonschema is None:
        global _warned_missing_jsonschema
        if not _warned_missing_jsonschema:
            _warned_missing_jsonschema = True
            log.warning(
                "jsonschema is not installed -- T81's schema-divergence "
                "check is skipped for every completion until it is; see "
                "this module's import guard"
            )
        return
    try:
        errors = list(_validator_for(schema).iter_errors(parsed))
    except Exception:
        log.debug(
            "%s schema divergence check itself failed; skipping",
            source, exc_info=True,
        )
        return
    if not errors:
        return
    error = max(errors, key=_schema_error_relevance)
    where = "/".join(str(part) for part in error.absolute_path) or "<root>"
    log.warning(
        "%s completion parsed but diverges from the schema it was sent, "
        "at %s: %s",
        source, where, _divergence_detail(error)[:200],
    )


def _finish(
    content: Any,
    schema: dict | None,
    *,
    recovered_field: str | None = None,
    source: str,
    sent_schema: dict | None = None,
) -> Any:
    """Parse ``content`` against ``schema`` when one was sent; else return as-is.

    The single decision point for both providers — was ``_ollama`` and
    ``_openrouter`` each ending their call with the same
    ``parse_json(content) if schema is not None else content`` line. T81's
    schema-divergence diagnostic shares this one insertion point too,
    instead of adding a second path that could drift from it.

    ``recovered_field`` tags a ``BadCompletion`` raised here as the answer
    having come from an out-of-band field (T96's ``_answer_text``), so the
    failure is self-contained in one log line instead of needing correlation
    with an earlier WARNING by adjacency.

    ``source`` names which provider is calling, for the WARNING T81 logs —
    required, not defaulted: both call sites (``_ollama``, ``_openrouter``)
    always pass a real one, and a silently-defaulted ``"model"`` would only
    ever mean a third call site forgot to.

    ``sent_schema`` is what T81 validates the parsed completion against,
    when it differs from ``schema`` — OpenRouter's degraded ``json_object``
    path (see ``_openrouter``) prompts the model with a smaller
    ``degraded_schema`` instead of the wire ``schema``, and diagnosing
    against the bigger one there would log a "divergence" on every degraded
    call for a shape nobody ever asked the model for. Defaults to ``schema``
    itself, which is what Ollama always sends and what OpenRouter sends on
    its primary (non-degraded) path.
    """
    if schema is None:
        return content
    try:
        parsed = parse_json(content)
    except BadCompletion as exc:
        if recovered_field:
            raise BadCompletion(
                f"{exc} (answer recovered from message.{recovered_field})"
            ) from exc
        raise
    _log_schema_divergence(
        parsed, schema if sent_schema is None else sent_schema, source=source,
    )
    return parsed


def _mime_type(path: Path) -> str:
    """Data URLs must declare the image type; Ollama's ``images`` array does not.

    Defaulting to PNG keeps a screenshot with no extension from being dropped
    outright — vision models tolerate a wrong label far better than a missing
    attachment.
    """
    return _MIME_TYPES.get(path.suffix.lower(), "image/png")


def _schema_prompt(system: str, schema: dict) -> str:
    """Restate a JSON Schema in the system prompt for json_object mode.

    ``json_object`` only guarantees that *something* parses as JSON; the shape
    has to come from the prompt, or the fallback returns valid JSON of the
    wrong form and the stage fails on a missing key instead.
    """
    return (
        f"{system}\n\n"
        "Reply with a single JSON object and nothing else — no prose, no "
        "markdown fence. It must conform to this JSON Schema:\n"
        f"{json.dumps(schema)}"
    )


class LLMClient:
    """Async chat over Ollama or OpenRouter, chosen by ``LLM_PROVIDER``."""

    def __init__(self, settings: Settings, http: Any) -> None:
        self.settings = settings
        self.http = http
        # T216 (reviewer, bounce 1 nit): no `self.provider` attribute here
        # any more — which provider serves a given call is resolved per-ROLE,
        # not once per client, via settings.provider_for(role) (see
        # _model/_chat below). A client-wide attribute invites exactly the
        # bug this task fixed (routing on one value for every role), so it
        # is gone rather than kept as an unread back-compat field.
        # Each transport owns its endpoint — a single shared url meant the
        # local fallback posted an Ollama-shaped body to OpenRouter, so the
        # fallback could never have worked. There is now no single url to
        # precompute here either: OpenRouter's is the fixed OPENROUTER_URL
        # constant, Ollama's depends on settings.ollama_host.
        self.ollama_url = f"{settings.ollama_host.rstrip('/')}/api/chat"
        #: T259: optional async ``(message) -> None`` the entrypoint wires to
        #: the operator's chat. Fired once per distinct config fault.
        self._alerter = OnceAlerter()

    @property
    def on_config_fault(self) -> Callable[[str], Awaitable[None]] | None:
        return self._alerter.send

    @on_config_fault.setter
    def on_config_fault(self, send: Callable[[str], Awaitable[None]] | None) -> None:
        self._alerter.send = send

    async def _config_fault(self, key: str, message: str) -> ConfigFault:
        """Build a ConfigFault, alerting the operator once per ``key``.

        Once per fault per process, not per item: every queued item hits the
        same missing model, and an alert each would bury the one that matters.
        Never raises -- an alert failure must not mask the fault itself.
        """
        return await self._alerter.fault(key, message)

    async def cheap(
        self, system: str, user: str, schema: dict | None = None,
        temperature: float = 0.3, degraded_schema: dict | None = None,
    ) -> Any:
        return await self._chat(
            "cheap", system, user, schema, temperature, degraded_schema=degraded_schema,
        )

    async def good(
        self, system: str, user: str, schema: dict | None = None,
        temperature: float = 0.5, degraded_schema: dict | None = None,
    ) -> Any:
        return await self._chat(
            "good", system, user, schema, temperature, degraded_schema=degraded_schema,
        )

    async def vision(
        self, system: str, user: str, image_paths: list[str | Path],
        schema: dict | None = None, temperature: float = 0.2,
        degraded_schema: dict | None = None,
    ) -> Any:
        images: list[tuple[str, str]] = []
        for path in image_paths:
            file = Path(path)
            try:
                encoded = base64.b64encode(file.read_bytes()).decode()
            except OSError:
                continue  # a missing attachment must not kill extraction
            images.append((_mime_type(file), encoded))
        return await self._chat(
            "vision", system, user, schema, temperature, images=images,
            degraded_schema=degraded_schema,
        )

    async def verify(
        self, system: str, user: str, schema: dict | None = None,
        temperature: float = 0.3,
    ) -> Any:
        """The pre-render verifier's call (T222/T224, D196-D207).

        Goes straight to ``_ollama`` with ``model_verify``/``num_ctx_verify``
        — it never goes through ``_chat``, so it never consults
        ``provider_for("verify")`` and never falls back to OpenRouter. This
        is local ALWAYS, regardless of ``LLM_PROVIDER`` or
        ``LLM_PROVIDER_GOOD`` (the user's own words: "running locally").
        Deliberately not a third branch inside ``_chat``/``_model``: those
        are keyed to ``LLM_PROVIDER_ROLES`` (cheap/good/vision) exactly
        because every one of those three CAN move to OpenRouter — adding
        "verify" there would make it routable by the same per-role override
        mechanism (``LLM_PROVIDER_VERIFY``) this task was explicitly told
        not to create.

        No ``degraded_schema``/``images`` parameters: the verifier never
        sends images, and it only ever talks to Ollama, which enforces
        ``format`` as a JSON Schema argument directly (no prompt-embedded
        fallback — that degrade path exists only for OpenRouter's
        ``json_object`` mode in ``_openrouter``, which this method never
        reaches).
        """
        return await self._ollama(
            self.settings.model_verify, self.settings.num_ctx_verify,
            system, user, schema, temperature,
        )

    # ------------------------------------------------------------- internals

    def _model(self, role: str) -> str:
        """Per-provider model for a role.

        Each provider keeps its own three names so switching back and forth
        never means re-entering models the owner already configured. T216:
        which provider serves THIS role is ``settings.provider_for(role)``
        — there is no client-wide default to fall back to; a role with its
        own ``LLM_PROVIDER_<ROLE>`` override can differ from its siblings.
        """
        s = self.settings
        if s.provider_for(role) == "openrouter":
            return {
                "cheap": s.openrouter_model_cheap,
                "good": s.openrouter_model_good,
                "vision": s.openrouter_model_vision,
            }[role]
        return {
            "cheap": s.model_cheap,
            "good": s.model_good,
            "vision": s.model_vision,
        }[role]

    def _num_ctx(self, role: str) -> int:
        """``settings.num_ctx_<role>`` for cheap/good/vision.

        T222: reads the one shared ``NUM_CTX_FIELD_FOR_ROLE`` mapping
        (config.py) rather than keeping its own dict literal — budget.py's
        ``budget_tokens_for_role`` reads the identical mapping, so the two
        can no longer drift the way they had (T70 bounce 1's named debt).
        A plain ``[role]`` index, same as before: an unknown role is a
        programming error here and should raise KeyError, not silently fall
        back to something plausible — this is only ever called with
        "cheap"/"good"/"vision" (``verify`` bypasses it entirely; see
        ``LLMClient.verify`` below).
        """
        return getattr(self.settings, NUM_CTX_FIELD_FOR_ROLE[role])

    async def _chat(
        self, role: str, system: str, user: str,
        schema: dict | None, temperature: float,
        images: list[tuple[str, str]] | None = None,
        degraded_schema: dict | None = None,
    ) -> Any:
        if self.settings.provider_for(role) != "openrouter":
            try:
                return await self._ollama(
                    self._model(role), self._num_ctx(role),
                    system, user, schema, temperature, images,
                )
            except ConfigFault:
                # T259: a model Ollama does not have. The configured spare
                # (MODEL_*_FALLBACK) exists for exactly this; without one
                # the fault propagates and the worker defers the item.
                spare = self._local_chain(role)[1:]
                if not spare:
                    raise
                log.warning(
                    "local %s is misconfigured for %s; trying spare %s",
                    self._model(role), role, spare[0],
                )
                return await self._ollama(
                    spare[0], self._num_ctx(role),
                    system, user, schema, temperature, images,
                )

        # Everything transient gets the same treatment: three attempts at
        # OpenRouter, then this one call runs on the local model. Free-tier
        # backends fail in several ways and every one of them has now cost an
        # item — a rate limit, a completion that is empty or prose where a
        # schema was required, and the service being unreachable. Deferring
        # instead would stall the queue behind someone else's load while a
        # working local model sits idle.
        #
        # A 4xx about OUR request is deliberately excluded: it fails
        # identically every time, and falling back would hide a real request
        # bug behind a weaker model. A 4xx that OpenRouter attributes to the
        # upstream PROVIDER is a different animal and is classified as
        # infrastructure at the call site below.
        last: Exception | None = None
        reason = "unavailable"
        spent = 0
        for attempt in range(1, OPENROUTER_ATTEMPTS + 1):
            spent = attempt
            try:
                return await self._openrouter(
                    self._model(role), system, user, schema, temperature, images,
                    degraded_schema=degraded_schema,
                )
            except ConfigFault as exc:
                # The slug is wrong; two more attempts fail identically.
                # Straight to the local chain (or out, with fallback off).
                last, reason = exc, "misconfigured"
                break
            except RateLimited as exc:
                last, reason = exc, "rate-limited"
            except BadCompletion as exc:
                last, reason = exc, "returning an unusable completion"
            except Retryforever as exc:
                # Unreachable or 5xx. RateLimited subclasses this, so it must
                # be caught after it.
                last, reason = exc, "unreachable"

            if attempt < OPENROUTER_ATTEMPTS:
                delay = OPENROUTER_BACKOFF_S * attempt
                log.warning(
                    "openrouter %s on %s (attempt %d/%d), retrying in %ss",
                    reason, role, attempt, OPENROUTER_ATTEMPTS, delay,
                )
                await asyncio.sleep(delay)

        if not self.settings.fallback_to_local:
            raise last

        chain = self._local_chain(role)
        log.warning(
            "openrouter %s on %s after %d attempt(s); falling back to local %s",
            reason, role, spent, " then ".join(chain),
        )
        # Walk the local chain. A second local model earns its place only by
        # answering when the first cannot — a model that is absent from Ollama
        # or too degraded to return usable output should not end the item while
        # a working one sits behind it.
        local_error: Exception | None = None
        for position, model in enumerate(chain, start=1):
            try:
                return await self._ollama(
                    model, self._num_ctx(role),
                    system, user, schema, temperature, images,
                )
            except (ConfigFault, Retryforever, BadCompletion) as exc:
                # ConfigFault is a Retryforever; named so the spare is
                # demonstrably tried when the primary model is absent (T259).
                local_error = exc
                if position < len(chain):
                    log.warning(
                        "local %s failed on %s (%s); trying %s",
                        model, role, exc, chain[position],
                    )

        # Every provider is exhausted. Report the openrouter reason alongside
        # the local one so the log names the real situation rather than only
        # the last thing tried.
        exhausted = (
            f"openrouter {reason} and local chain "
            f"({', '.join(chain)}) unavailable: {local_error}"
        )
        raise (
            ConfigFault(exhausted)
            if isinstance(local_error, ConfigFault) else Retryforever(exhausted)
        ) from local_error

    def _model_local(self, role: str) -> str:
        """The Ollama model for a role, whatever provider is selected."""
        s = self.settings
        return {"cheap": s.model_cheap, "good": s.model_good,
                "vision": s.model_vision}[role]

    def _local_chain(self, role: str) -> list[str]:
        """Local models to try in order: the primary, then any spare."""
        s = self.settings
        spare = {"cheap": s.model_cheap_fallback,
                 "good": s.model_good_fallback,
                 "vision": s.model_vision_fallback}[role]
        chain = [self._model_local(role)]
        if spare and spare != chain[0]:
            chain.append(spare)
        return chain

    async def _ollama(
        self, model: str, num_ctx: int, system: str, user: str,
        schema: dict | None, temperature: float,
        images: list[tuple[str, str]] | None = None,
    ) -> Any:
        message: dict[str, Any] = {"role": "user", "content": user}
        if images:
            message["images"] = [encoded for _, encoded in images]

        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, message],
            "stream": False,
            "think": False,
            "options": {"temperature": temperature, "num_ctx": num_ctx},
        }
        if schema is not None:
            payload["format"] = schema

        for attempt in (1, 2):
            try:
                response = await asyncio.wait_for(
                    self.http.post(
                        self.ollama_url, json=payload, timeout=CALL_DEADLINE_S
                    ),
                    timeout=CALL_DEADLINE_S,
                )
            except asyncio.TimeoutError as exc:
                raise Retryforever(
                    f"ollama exceeded {CALL_DEADLINE_S}s"
                ) from exc
            except Exception as exc:  # connection refused, DNS, timeout
                raise Retryforever(f"ollama unreachable: {exc}") from exc

            status = response.status_code
            if status == 400 and attempt == 1 and "think" in _body_text(response).lower():
                # Non-thinking models reject the `think` parameter outright.
                payload.pop("think", None)
                continue
            if status >= 500:
                raise Retryforever(f"ollama {status}: {_body_text(response)[:200]}")
            if status == 404:
                # Ollama answers 404 for a model it has not pulled. Every item
                # hits the same wall until the operator pulls it or edits config.
                raise await self._config_fault(
                    f"ollama:{model}",
                    f"Ollama has no model {model!r} "
                    f"(404: {_body_text(response)[:200]}); `ollama pull` it "
                    f"or fix the MODEL_* setting",
                )
            if status >= 400:
                raise Retryable(f"ollama {status}: {_body_text(response)[:200]}")

            try:
                reply = response.json()["message"]
            except (KeyError, TypeError, ValueError) as exc:
                # A 200 whose body is not JSON (proxy error page, truncated
                # stream) is the model side misbehaving, not a request bug.
                raise BadCompletion(
                    f"ollama returned no completion: {_body_text(response)[:200]}"
                ) from exc
            content, recovered_field = _answer_text(reply, "content", "thinking", "ollama")
            if content is None:
                # As unusable as a missing key would be, and would otherwise
                # reach parse_json or be returned as a valid empty answer.
                # Also covers `reply` not being a dict at all (see
                # `_answer_text`) — a pre-existing gap this closes too.
                raise BadCompletion(
                    f"ollama returned an empty completion: {_body_text(response)[:200]}"
                )
            return _finish(
                content, schema, recovered_field=recovered_field, source="ollama",
            )

        raise Retryable("ollama call did not resolve")  # pragma: no cover

    async def _openrouter(
        self, model: str, system: str, user: str,
        schema: dict | None, temperature: float,
        images: list[tuple[str, str]] | None = None,
        degraded_schema: dict | None = None,
    ) -> Any:
        """POST the OpenAI-compatible chat endpoint.

        Same three differences from Ollama every time: structured output rides
        on ``response_format`` instead of ``format``, images ride in a content
        array as data URLs instead of a base64 ``images`` list, and ``num_ctx``
        does not exist here.
        """
        content: Any = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                }
                for mime, encoded in images
            ]

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "stream": False,
            "temperature": temperature,
        }
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": schema},
            }

        headers = {"Authorization": f"Bearer {self.settings.openrouter_api_key}"}

        # What T81 diagnoses the answer against. Starts as the real wire
        # `schema`; the degrade branch below repoints it at whatever schema
        # actually got embedded in the prompt, so the diagnostic never flags
        # a degraded completion for not matching a schema it was never asked
        # to follow.
        sent_schema = schema

        for attempt in (1, 2):
            try:
                response = await asyncio.wait_for(
                    self.http.post(
                        OPENROUTER_URL, json=payload, headers=headers,
                        timeout=CALL_DEADLINE_S,
                    ),
                    timeout=CALL_DEADLINE_S,
                )
            except asyncio.TimeoutError as exc:
                # Wall-clock, unlike httpx's between-bytes read timeout: a
                # response that trickles forever is stopped here.
                raise Retryforever(
                    f"openrouter exceeded {CALL_DEADLINE_S}s"
                ) from exc
            except Exception as exc:  # connection refused, DNS, timeout
                raise Retryforever(f"openrouter unreachable: {exc}") from exc

            status = response.status_code
            body = _body_text(response)

            if (
                schema is not None
                and attempt == 1
                and 400 <= status < 500
                and _SCHEMA_UNSUPPORTED.search(body)
            ):
                # This model has no json_schema support. Degrade to json_object
                # with the schema in the prompt rather than dropping structure:
                # every caller that passes a schema needs a dict back. Callers
                # that pass a smaller `degraded_schema` (e.g. compose.py's flat
                # slide schema in place of its 15-arm discriminated union) get
                # that embedded instead — a prompt-embedded schema was never a
                # real validator here regardless of which one is sent, so a
                # smaller one loses no enforcement while costing fewer tokens.
                prompt_schema = schema if degraded_schema is None else degraded_schema
                payload["response_format"] = {"type": "json_object"}
                payload["messages"][0]["content"] = _schema_prompt(system, prompt_schema)
                sent_schema = prompt_schema
                continue
            if status == 429:
                # A rate limit is the service saying "not now". The item is
                # fine, so it must not spend an attempt on someone else's load.
                raise RateLimited(f"openrouter 429: {body[:200]}")
            if status >= 500:
                raise Retryforever(f"openrouter {status}: {body[:200]}")
            if status in _TERMINAL_STATUSES:
                raise Terminal(f"openrouter {status}: {body[:200]}")
            if _is_model_gone(status, body):
                raise await self._config_fault(
                    f"openrouter:{model}",
                    f"OpenRouter model {model!r} is unavailable "
                    f"({status}: {body[:200]}); pick a new OPENROUTER_MODEL_*",
                )
            if status >= 400 and _is_provider_fault(body):
                # OpenRouter reached us fine and is reporting that the model's
                # upstream broke — item 124 got a 404 whose body was "Provider
                # returned error" wrapping "Function id ... Not Found". Nothing
                # about the request is wrong, so this must not spend the item's
                # attempt, and it should retry (OpenRouter may route elsewhere)
                # and then fall back locally. Left as a plain Retryable it
                # escaped the retry ladder altogether: no retries, no fallback,
                # three attempts burned, item failed.
                raise Retryforever(f"openrouter {status}: {body[:200]}")
            if status >= 400:
                raise Retryable(f"openrouter {status}: {body[:200]}")

            try:
                reply = response.json()["choices"][0]["message"]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                # A 200 carrying an error envelope instead of a completion is
                # normal here: OpenRouter answers that way when the upstream
                # provider dies after the response has started.
                raise BadCompletion(
                    f"openrouter returned no completion: {body[:200]}"
                ) from exc
            content, recovered_field = _answer_text(reply, "content", "reasoning", "openrouter")
            if content is None:
                # Neither field carried anything usable, OR `message` was not
                # a dict at all (null, a bare string, a number, a list — a
                # real 200 shape; see `_answer_text`). Either way this is as
                # unusable as a missing key, and would otherwise reach
                # parse_json, throw AttributeError, or be returned as a valid
                # answer.
                raise BadCompletion(
                    f"openrouter returned an empty completion: {body[:200]}"
                )
            return _finish(
                content, schema, recovered_field=recovered_field,
                source="openrouter", sent_schema=sent_schema,
            )

        raise Retryable("openrouter call did not resolve")  # pragma: no cover


def _body_text(response: Any) -> str:
    try:
        return response.text or ""
    except Exception:  # pragma: no cover
        return ""
