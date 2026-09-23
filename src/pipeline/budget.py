"""Pack a research corpus into a character budget without losing anything
silently.

T61: ``num_ctx_cheap=8192`` cannot hold the ~10k-token corpus three uncapped
articles produce, and Ollama does not error on overflow — it silently drops
the tail, so the note gets written from a fraction of the material (see
``llm.py``'s module docstring for why ``num_ctx`` is pinned rather than
raised). D11 (project-context.md) narrows the fix: live-tested against the
real ``NOTE_SCHEMA`` at 40,274 chars (three full articles), every candidate
OpenRouter model extracted the right fact — the primary hosted path was never
the problem. The fix belongs entirely to the LOCAL path: the model actually
loaded with a fixed ``num_ctx``, whether that is the configured primary
provider or ``llm.py``'s fallback chain after OpenRouter is exhausted.

**The seam.** ``LLMClient._chat`` decides provider and fallback internally
(``llm.py``, out of this task's file scope) and the caller only ever sees
``llm.cheap(system, user, schema)`` — by the time that call is made, the
exact model that will answer it is not observable from here, and cannot be,
because it can depend on real-time OpenRouter availability. This module does
not try to predict that. It reads the same two settings ``LLMClient`` itself
reads to make the choice — ``llm_provider`` for the transport, and
``num_ctx_<role>`` for the local context window — and sizes the budget for
the model the CONFIGURED provider will hand this call to first:

- ``llm_provider == "ollama"``: local is the only path. Budget from
  ``num_ctx_<role>``, the exact figure ``llm.py`` pins the model to.
- ``llm_provider == "openrouter"``: budget generously from the shared
  262144-token window D11 measured, because constraining a healthy path to a
  local-sized budget "just in case" throws away real quality for no
  measured benefit (D11 is explicit that T70 must not do this).

Residual gap, named rather than hidden: if OpenRouter is configured, exhausts
its three attempts, and falls back to local *mid-call*, ``llm.py`` re-sends
the SAME ``user`` string that was budgeted for OpenRouter's window to the
local model — this module cannot intervene inside that call, and closing it
fully needs ``llm.py`` to expose the model before the call (impossible, it is
resolved at request time) or a repack-on-fallback hook (a change to the
shared client contract every stage uses, out of scope for this task's file
list). This is materially smaller than the bug this task closes: it now
requires an actual OpenRouter failure (T69 pins a validated model specifically
to remove the routine-failure trigger) rather than firing on every local call,
and unlike today it is bounded by whatever OpenRouter budget was chosen, not
unbounded uncapped text. Tracked as a follow-up against ``llm.py`` rather than
claimed closed here.

**Fair share.** Every source gets an equal slice of the budget, except a
source shorter than its slice only takes what it needs — the leftover is
handed to sources that are still over their share (water-filling / max-min
fairness), so one short source never starves two long ones and one long
source never starves the other two.

**Nothing vanishes without a trace (D5).** A source that does not fit is cut
head+tail (the lede is usually up top; a concluding figure is often at the
bottom) with an explicit ``[... N chars omitted ...]`` marker where the
middle was removed. ``kept_chars + omitted_chars == original_chars`` always,
for every source, regardless of budget — including budgets too small to
even fit the marker, where the packed text can be shorter than the marker
itself but the accounting still reconciles exactly.

**Marker forgery.** The corpus is untrusted fetched web text (search.py caps
one page at ``MAX_BODY_BYTES = 2_000_000`` after HTML extraction, so a single
source is bounded before it ever reaches this module). A page could contain
the literal string ``"[... 999999 chars omitted ...]"`` — by accident, or
planted deliberately so a downstream reader (human or the research LLM
itself) mistakes attacker content for pipeline-authored metadata. The real
marker is wrapped in a Private Use Area sentinel and BOTH the source text
*and* its id are stripped of that sentinel before packing (``_sanitize``,
below), so the wrapped form can only ever appear once we insert it — this
covers ``PackedSource.id`` too, since ``research.py`` interpolates it
straight into the prompt as ``[SOURCE: {id}]``.

This sanitization is **load-bearing, not defense-in-depth.** trafilatura
2.2.0 (the version pinned in ``pyproject.toml``) does strip a raw U+E000,
``&#xE000;`` and ``&#57344;`` from HTML it extracts — but ``research.py``
falls back to the RAW, un-extracted SearXNG ``content`` snippet whenever
extraction returns nothing (search.py's fetch path), and that text never
passes through trafilatura at all. ``_sanitize`` is the only thing standing
between that fallback path and a forged marker, not a redundant safety net
on top of trafilatura's own stripping.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .config import Settings

log = logging.getLogger(__name__)

#: Conservative chars-per-token estimate. English prose BPE tokenizers
#: average nearer 4 chars/token, but this module's own ``RESEARCH_SYSTEM``
#: prompt explicitly hunts verbatim code artifacts, and dense code / CJK text
#: runs closer to 2 chars/token — at 3, an 8192-token ``num_ctx_cheap`` budget
#: (RESERVED_TOKENS deducted) converts to 18,432 corpus chars, which is
#: already ~9,216 tokens of code-dense content alone: enough to overflow
#: ``num_ctx`` by itself, before the reserved system-prompt/question/
#: completion tokens are even counted. That is the exact silent-overflow bug
#: this module exists to close, so this constant is pinned to the WORST
#: CASE (dense code/CJK), not the average case — a real BPE-token count is
#: not available at pack() time (see the module docstring: the serving model
#: is only known inside ``llm.py``), so this stays a conservative fixed
#: estimate rather than a measurement.
CHARS_PER_TOKEN = 2

#: Tokens reserved, on the local path, for everything that is not the
#: fetched corpus: the system prompt, the original item text, the research
#: question, and the model's own completion. Deliberately generous — too
#: small costs a little unused context, too large silently overflows
#: ``num_ctx``, which is the exact bug this module exists to close.
RESERVED_TOKENS = 2048

#: D11 (project-context.md, 2026-09-22): every free-tier "cheap" candidate
#: tested against the real NOTE_SCHEMA at 40,274 chars declares a
#: 262144-token window and extracted the correct fact with the full corpus —
#: OpenRouter never truncated anything. Budgeted at a quarter of the declared
#: window rather than all of it: headroom for a future tier with a smaller
#: window, and unlike Ollama, OpenRouter errors loudly on genuine overflow
#: rather than silently dropping, so erring generous here cannot reproduce
#: the silent-truncation bug — it can only ever fail loud.
OPENROUTER_CONTEXT_TOKENS = 262_144
OPENROUTER_BUDGET_TOKENS = OPENROUTER_CONTEXT_TOKENS // 4

#: How a truncated source is split between its lede and its ending. Head-only
#: loses conclusions, which is frequently where the figure lives; majority-head
#: still favours the inverted pyramid most reporting uses.
HEAD_RATIO = 0.7

#: Private Use Area codepoint (U+E000), written as an escape rather than
#: the literal invisible character so it is visible in a diff/editor.
#: Legitimate TRAFILATURA-extracted web text cannot contain it -- but see
#: the module docstring's "load-bearing, not defense-in-depth" note: not
#: every source on the fetch path goes through trafilatura, which is
#: exactly why it is also stripped from every source (and id) before
#: packing regardless.
_MARKER_SENTINEL = "\ue000"
_MARKER_BODY = "[... {n} chars omitted ...]"
_MARKER_TEMPLATE = f"{_MARKER_SENTINEL}{_MARKER_BODY}{_MARKER_SENTINEL}"
#: Reserve enough room for a marker naming any omitted count up to 12 digits
#: (a trillion chars — search.py's MAX_BODY_BYTES bounds a real source to 7).
_MARKER_RESERVE = len(_MARKER_TEMPLATE.format(n="9" * 12))
_MARKER_PATTERN = re.compile(
    re.escape(_MARKER_SENTINEL) + r"\[\.\.\. \d+ chars omitted \.\.\.\]" + re.escape(_MARKER_SENTINEL)
)

#: DRY note (reviewer, T70 bounce 1): this duplicates ``llm.py``'s own
#: ``LLMClient._num_ctx`` role->field mapping, and the two differ in failure
#: mode (this one uses ``getattr(..., "num_ctx_cheap")`` as a default for an
#: unknown role; ``llm.py``'s is a plain dict lookup that raises `KeyError`).
#: Not folded together here: ``llm.py`` is explicitly out of this task's file
#: scope (see the module docstring's "the seam" section), and T87 already
#: touches ``llm.py`` for unrelated reasons — that is the natural place to
#: extract one shared mapping both modules import. Left as a named, not
#: hidden, duplicate.
_NUM_CTX_FIELD = {
    "cheap": "num_ctx_cheap",
    "good": "num_ctx_good",
    "vision": "num_ctx_vision",
}


def marker_present(text: str) -> bool:
    """True iff ``text`` contains a genuine (unforgeable) omission marker."""
    return bool(_MARKER_PATTERN.search(text))


def _sanitize(text: str) -> str:
    """Strip the marker sentinel so ours, inserted later, is the only one.

    Otherwise a fetched page (or a crafted URL used as a source id — see the
    module docstring) could plant the sentinel-wrapped pattern itself and
    have it read downstream as pipeline-authored metadata rather than
    attacker content. Called on both ``Source.id`` and ``Source.text``: an
    unforgeable marker in the text is no protection if the id carrying it
    is interpolated into the prompt right next to it (``research.py``'s
    ``[SOURCE: {id}]``).
    """
    if _MARKER_SENTINEL in text:
        log.warning("stripped marker sentinel from untrusted source")
        return text.replace(_MARKER_SENTINEL, "")
    return text


def budget_tokens_for_role(
    settings: Settings, role: str = "cheap", *, reserved_tokens: int = RESERVED_TOKENS,
) -> int:
    """How many tokens of corpus this call's role can carry, given which
    provider ``LLMClient`` is currently configured to call first.

    Mirrors ``LLMClient``'s own choice (``settings.llm_provider`` and
    ``settings.num_ctx_<role>``) rather than querying it — see the module
    docstring for why the actual serving model cannot be known any earlier
    than ``llm.py`` itself knows it.

    ``reserved_tokens`` (D59/T109): ``RESERVED_TOKENS`` was sized for
    research.py's own scaffold and is wrong for any caller whose own SYSTEM
    prompt (plus whatever else rides the same call, e.g. a JSON schema) is
    bigger — compose's is 3.3x it. There is no single reserve that is right
    for every stage, so a caller with a materially different scaffold passes
    its own measured figure here; the default keeps every caller that does
    NOT pass one (research.py, today) byte-identical to before this
    parameter existed. Whoever passes a non-default value owns citing how
    they measured it (house rule 4) next to THEIR own scaffold constant —
    this function only accepts the number, it has no notion of what a
    "scaffold" is for any given stage.
    """
    if settings.llm_provider == "openrouter":
        return OPENROUTER_BUDGET_TOKENS
    num_ctx = getattr(settings, _NUM_CTX_FIELD.get(role, "num_ctx_cheap"))
    return max(0, num_ctx - reserved_tokens)


def budget_chars_for_role(
    settings: Settings, role: str = "cheap", *, reserved_tokens: int = RESERVED_TOKENS,
) -> int:
    """``budget_tokens_for_role`` converted to the char budget ``pack`` uses.

    See ``budget_tokens_for_role`` for ``reserved_tokens`` (D59/T109's
    per-stage reserve).
    """
    return budget_tokens_for_role(
        settings, role, reserved_tokens=reserved_tokens
    ) * CHARS_PER_TOKEN


@dataclass(frozen=True, slots=True)
class Source:
    """One fetched, relevance-gated document, before packing."""

    id: str
    text: str


@dataclass(frozen=True, slots=True)
class PackedSource:
    """One source after packing. ``kept_chars + omitted_chars ==
    original_chars`` always, whether or not anything was actually cut."""

    id: str
    text: str
    original_chars: int
    kept_chars: int
    omitted_chars: int


def _fair_shares(lengths: list[int], budget: int) -> list[int]:
    """Max-min fair share: process shortest-demand-first, give each source
    the smaller of (what it actually needs, an equal split of what is left
    among the sources not yet settled). A source that needs less than an
    equal share takes only that; the rest is redistributed to the sources
    still over theirs.
    """
    n = len(lengths)
    if n == 0 or budget <= 0:
        return [0] * n
    shares = [0] * n
    remaining_budget = budget
    for rank, i in enumerate(sorted(range(n), key=lambda i: lengths[i])):
        slots_left = n - rank
        even_share = remaining_budget // slots_left
        take = min(lengths[i], even_share)
        shares[i] = take
        remaining_budget -= take
    return shares


def _pack_one(source: Source, share: int) -> PackedSource:
    """Pack one ALREADY-SANITIZED source (see ``pack``, which sanitizes id
    and text exactly once for the whole corpus before calling this)."""
    text = source.text
    original = len(text)

    if original <= share:
        return PackedSource(source.id, text, original, original, 0)

    # Reserve room for the marker out of the share itself, never more than
    # the share has — the per-source output can never exceed `share`, so the
    # corpus-wide total can never exceed the sum of shares (== budget).
    marker_room = min(share, _MARKER_RESERVE)
    content_budget = share - marker_room
    head_len = int(content_budget * HEAD_RATIO)
    tail_len = content_budget - head_len
    kept = head_len + tail_len
    omitted = original - kept

    marker = _MARKER_TEMPLATE.format(n=omitted)
    if len(marker) > marker_room:
        marker = marker[:marker_room]  # only in a budget too small for any digit width

    tail = text[-tail_len:] if tail_len > 0 else ""
    packed_text = text[:head_len] + marker + tail
    return PackedSource(source.id, packed_text, original, kept, omitted)


def pack(sources: list[Source], budget_chars: int) -> list[PackedSource]:
    """Fit ``sources`` into ``budget_chars`` total, fair-shared, nothing
    dropped without ``kept_chars + omitted_chars == original_chars`` still
    holding and a marker at the cut.

    Guaranteed for any input, including a budget of 0: the sum of packed
    text lengths never exceeds ``budget_chars``.

    ``id`` and ``text`` are both sanitized here, once, before anything else
    runs — not inside ``_pack_one`` (which would sanitize the same text a
    second time, since ``pack`` also needs the sanitized length to compute
    fair shares) and not skipped for ``id`` (an attacker-controlled id, e.g.
    a fetched URL, can forge the marker just as easily as the text can —
    see the module docstring).
    """
    budget = max(0, budget_chars)
    sanitized = [Source(id=_sanitize(s.id), text=_sanitize(s.text)) for s in sources]
    lengths = [len(s.text) for s in sanitized]
    shares = _fair_shares(lengths, budget)
    return [_pack_one(source, share) for source, share in zip(sanitized, shares)]
