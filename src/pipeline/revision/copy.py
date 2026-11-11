"""Operator-facing wording for agentic regeneration (T302, spec: docs/design/revision-copy.md).

ONE pure module: no I/O, no clock, no model. The bot, the dashboard and the worker's
notifier render through it, so no wording is typed twice (D272). Everything that
reaches the operator is built from CLOSED inputs only:

- ``OutcomeCode`` / ``Reason`` / op / check status values and the closed ``Outcome.reason``
  codes, mapped to fixed sentences below (a missing mapping is a failing test, like
  ``ACTION_SPECS``);
- counts, slide numbers, and the operator's / planner's own words (``note``, ``asks``,
  headlines), which are sanitised and capped by ``clean`` / ``quote``.

Rule 15: no exception text, class name, path, URL, model name or raw enum value is ever
interpolated. ``Outcome.reason`` free text never gets here (``record.closed_reason`` drops
it when the run is written); an unknown code falls to a generic line, never to its text.

Plain text only (no ``parse_mode`` anywhere): model- and web-derived text is inert.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..deck_shape import CLOSING_TYPES, MAX_SLIDES, MIN_SLIDES
from ..limits import TELEGRAM_CALLBACK_ANSWER_LIMIT, TELEGRAM_TEXT_LIMIT
from .check import Status as CheckStatus
from .execute import REASON_GLOBAL_OP, REASON_MODEL_UNUSABLE, REASON_SEARCH_BLOCKED
from .fetch import (
    REASON_IMAGE_UNCHECKED,
    REASON_NO_CANDIDATES,
    REASON_NO_RESULTS,
    REASON_NO_USABLE_IMAGE,
    REASON_NOT_ANSWERED,
    REASON_SAME_IMAGE,
    REASON_SOURCE_UNVERIFIED,
)
from .model import (
    MAX_ACTIONS,
    MAX_ASKS,
    MAX_NOTE_CHARS,
    MAX_RESEARCH_ACTIONS,
    Action,
    Op,
    OutcomeCode,
    Reason,
)
from .request import ConfirmResult, actions_from_plan, confirm_items, pending_confirm
from .text_edit import REASON_EMPTY_SLIDE
from .structural import (
    REASON_ALREADY_REMOVED,
    REASON_BODYLESS,
    REASON_CLOSING_CLAMP,
    REASON_CTA_DROPPED,
    REASON_HOOK,
    REASON_MAX_SLIDES,
    REASON_MIN_SLIDES,
    WARN_TEXT_ONLY_THEME,
)

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Caps. EDITORIAL unless noted (house rule 2); the platform limits are imported.
# --------------------------------------------------------------------------

#: Toast over a tapped button: a PLATFORM limit (limits.py).
TOAST_MAX = TELEGRAM_CALLBACK_ANSWER_LIMIT
CHANGES_MAX_CHARS = 1200  # the whole Changes block
CHANGE_LINE_MAX = 100  # one line of it, cut at a word with an ellipsis
NOTE_QUOTE_MAX = 80  # the operator's note in the header
HEADLINE_QUOTE_MAX = 24  # a headline in a slide label
ASK_QUOTE_MAX = 60  # an unsatisfied ask in the check line
MAX_NOT_DONE_LINES = 6  # not-done / unverified lines shown, then "+N more not done"
MAX_QUOTED_ASKS = 3  # asks named in the check line, then "+N more"
SLIDE_LIST_MAX_LINES = 10  # the slide list in the ask message (= MAX_SLIDES)
THEMES_CLAUSE_MAX = 100  # the available-themes list in a line
MAX_ALSO_DOING = 3  # held-plan neighbours named in a confirm prompt, then "+N more"
MAX_CONFIRM_SLIDE_LIST = 3  # above this the confirm says "N slides" (the list stays)
REGEN_PROMPT_MAX = 1000  # AC-1
#: Below this budget the Changes block is cut to header + check line (spec §5.9 step 6).
LAST_RESORT_BUDGET = 200
#: Separators between the Changes block, the caption and the stats (spec §5.9 step 1).
PREVIEW_SEPARATOR_ALLOWANCE = 40

# --------------------------------------------------------------------------
# Sanitising
# --------------------------------------------------------------------------

ELLIPSIS = "…"
_WHITESPACE = re.compile(r"\s+")


def clean(value: Any, cap: int) -> str:
    """One line of inert text: control and format characters dropped (bidi overrides,
    zero-width), whitespace collapsed, cut at ``cap`` characters (the ellipsis counts)."""
    text = "" if value is None else str(value)
    kept = [" " if ch.isspace() else ch for ch in text if ch.isspace() or not unicodedata.category(ch).startswith("C")]
    return _cut(_WHITESPACE.sub(" ", "".join(kept)).strip(), cap)


def _cut(text: str, cap: int) -> str:
    """``text`` within ``cap`` characters, cut at a word when one is near the end."""
    if len(text) <= cap:
        return text
    head = text[: max(0, cap - 1)]
    space = head.rfind(" ")
    if space >= cap // 2:
        head = head[:space]
    return head.rstrip(" ,;:-—–") + ELLIPSIS


def quote(value: Any, cap: int) -> str:
    """``clean`` text wrapped in straight double quotes (an inner ``"`` becomes ``'``
    so a quote can never be closed early). ``cap`` bounds the text, not the quotes."""
    return '"' + clean(value, cap).replace('"', "'") + '"'


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ints(values: Any) -> list[int]:
    return [n for n in (_int(v, -1) for v in values if not isinstance(v, bool)) if n >= 0] if isinstance(values, list) else []


def _mappings(values: Any) -> list[Mapping[str, Any]]:
    return [v for v in values if isinstance(v, Mapping)] if isinstance(values, list) else []


def _slide_word(numbers: Sequence[int]) -> str:
    return ("slide " if len(numbers) == 1 else "slides ") + _runs(numbers)


def _runs(numbers: Iterable[int], *, sep: str = ", ") -> str:
    """``[1, 2, 4, 5, 6]`` -> ``1–2, 4–6`` (consecutive numbers collapse to an en-dash range)."""
    nums = sorted(set(numbers))
    parts: list[str] = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        if j > i:
            parts.append(f"{nums[i]}–{nums[j]}")
        else:
            parts.extend(str(n) for n in nums[i : j + 1])
        i = j + 1
    return sep.join(parts)


# --------------------------------------------------------------------------
# The fixed sentences
# --------------------------------------------------------------------------

#: A glyph is never the only carrier of meaning: every not-done line also says so in words.
MARK_OK, MARK_BAD, MARK_UNKNOWN, MARK_NEUTRAL = "✓", "✗", "?", "–"
SEPARATOR = "— — —"


class Mark(StrEnum):
    OK = MARK_OK
    BAD = MARK_BAD
    UNKNOWN = MARK_UNKNOWN
    NEUTRAL = MARK_NEUTRAL
    HIDE = ""  # shown nowhere (the model over-reached, or a duplicate)


@dataclass(frozen=True)
class Row:
    mark: Mark
    text: str


def _r(mark: Mark, text: str = "") -> Row:
    return Row(mark, text)


HIDDEN = _r(Mark.HIDE)
CODE_ANY: str | None = None  # wildcard in OUTCOME_COPY keys

REGEN_TOAST = "Reply with what to change, or /skip."
ACK_NOTE = "Got it — revising item {id}. New preview shortly."
ACK_SKIP = "Making a fresh version of item {id} — new preview shortly."
ITEM_MOVED = "Item {id} has moved on, so I didn't change it. Check its latest preview."
REPLACED_CONFIRM = "Replaced by your newer note."
DEFERRED = (
    "The model is busy, so item {id} is waiting. I'll retry on my own — your note is kept, "
    "no need to resend."
)
#: Dashboard: stands in for ``items.last_error`` while a revision waits (it holds str(exc), rule 15).
REVISION_WAITING_SUMMARY = "Revision is waiting to retry (the model or web search is unavailable). Details are in the log."
BUSY_REVISING_TOAST = "That item is being revised — wait for the new preview."
BUSY_CONFIRM_TOAST = "That item is waiting for your Yes/No — answer it, or send a newer note with Regenerate."

_REGEN_PROMPT_HEAD = (
    "Revising item {id}. Tell me what to change. Examples:\n"
    "• \"slide 3 shorter\"\n"
    "• \"the chart slide is too wordy\"\n"
    "• \"swap the photo on slide 2 for a clearer shot of the chip\"\n"
    "• \"is the 40% on slide 5 right?\"\n"
    "• \"make the caption more casual\"\n"
    "• \"remove slide 4\" · \"swap slides 2 and 5\"{theme_example}\n"
    "Only what you name changes. Removing slides or starting over asks you to confirm first.\n"
    "/skip = fresh full version (every slide rewritten)."
)


def regen_prompt(item_id: int, themes: Sequence[str] = ()) -> str:
    """The Regenerate prompt (a chat message: ~440 characters, over the toast cap)."""
    names = [clean(t, 30) for t in themes if clean(t, 30)]
    text = _REGEN_PROMPT_HEAD.format(
        id=item_id, theme_example=f" · \"use the {names[0]} theme\"" if names else ""
    )
    if names:
        listing = "Themes: " + ", ".join(names)
        if len(text) + 1 + len(listing) <= REGEN_PROMPT_MAX:
            text += "\n" + listing
    return text


# ---- confirmation (spec §3)

CONFIRM_REMOVE_HEAD = "Remove {what}? There is no undo — the old version isn't kept."
CONFIRM_REBUILD_HEAD = "Rebuild the whole deck from scratch? Every slide gets new text. There is no undo."
BUTTON_YES_REMOVE = "✅ Yes, remove"
BUTTON_NO_KEEP_IT = "↩️ No, keep it"
BUTTON_NO_KEEP_THEM = "↩️ No, keep them"
BUTTON_YES_REBUILD = "✅ Yes, rebuild"
BUTTON_NO_KEEP_DECK = "↩️ No, keep deck"

#: Toast per ``ConfirmResult`` (<= TOAST_MAX). Keyed by the enum so a new result without
#: copy fails ``test_every_confirm_result_has_a_toast``.
CONFIRM_TOASTS: Mapping[ConfirmResult, str] = {
    ConfirmResult.YES: "Confirmed — working on it.",
    ConfirmResult.NO_CONTINUE: "Kept it. Applying the rest.",
    ConfirmResult.NO_NOTHING_LEFT: "Kept it. Nothing was changed.",
    ConfirmResult.ALREADY_ANSWERED: "Already answered — working on it.",
    ConfirmResult.STALE: "That question is out of date — I'm using your newer note.",
    ConfirmResult.NOT_WAITING: "That's no longer needed — nothing was changed.",
    ConfirmResult.NO_ITEM: "That item no longer exists.",
}
#: The confirm message is edited (buttons gone) to this for results that leave nothing to press.
CONFIRM_EDITS_STATIC: Mapping[ConfirmResult, str] = {
    ConfirmResult.STALE: REPLACED_CONFIRM,
    ConfirmResult.NOT_WAITING: CONFIRM_TOASTS[ConfirmResult.NOT_WAITING],
    ConfirmResult.NO_ITEM: CONFIRM_TOASTS[ConfirmResult.NO_ITEM],
    ConfirmResult.ALREADY_ANSWERED: "Answered — working on it.",
}

# ---- ask the operator (spec §4)


class AskCause(StrEnum):
    VAGUE = "vague"  # no concrete change, off-topic, hostile
    NEGATED = "negated"  # "don't remove slide 2"
    WHICH_SLIDE = "which_slide"  # could not pin it to one slide
    EXPLICIT_NEEDED = "explicit_needed"  # remove/swap/restyle/rebuild needs exact wording
    WHICH_THEME = "which_theme"  # no theme named, or a name that does not exist


ASK_CAUSE_LINES: Mapping[AskCause, str] = {
    AskCause.VAGUE: "I couldn't tell what to change — nothing was touched.",
    AskCause.NEGATED: "You said not to, so I left it alone. If you do want a change, say it directly.",
    AskCause.WHICH_SLIDE: "Which slide do you mean? I couldn't pin it to one.",
    AskCause.EXPLICIT_NEEDED: "Removing, swapping and restyling need exact wording, so I didn't guess.",
    AskCause.WHICH_THEME: "Which theme?{themes_clause}",
}
ASK_NOTHING_TOUCHED = "Nothing was touched."
#: D289: the planner gave no usable plan (twice) and the rules had nothing; never a silent rebuild.
ASK_PLAN_FAILED_LINE = "I couldn't work out what to change — nothing was touched."
ASK_SLIDES_HEAD = "Slides in item {id}:"
ASK_CLOSING_LINE = "{span} added automatically (can't be edited)"
ASK_EXIT = "Reply with a new note, or /skip for a fresh full version."

#: Reasons whose drop is the operator's own mistake and is worth naming even when it
#: leaves nothing to run (shown in the ask message under the cause line).
_ASK_SPECIFIC_REASONS = frozenset({Reason.OUT_OF_RANGE, Reason.CLOSING_SLIDE, Reason.NO_IMAGE})


def ask_cause(dropped: Iterable[Any]) -> AskCause:
    """Why nothing was grounded, from the drop reasons alone (no model call, no note
    text). Priority: negated > theme > explicit-wording > slide > vague. ``dropped``
    are ``Dropped`` objects or run rows (``{"reason": ...}``)."""
    reasons = {_reason_of(d) for d in dropped}
    for cause, wanted in (
        (AskCause.NEGATED, {Reason.NEGATED}),
        (AskCause.WHICH_THEME, {Reason.THEME_NOT_NAMED, Reason.BAD_THEME}),
        (AskCause.EXPLICIT_NEEDED, {Reason.RULES_ONLY}),
        (AskCause.WHICH_SLIDE, {Reason.NO_SLIDE_REFERENCE}),
    ):
        if reasons & wanted:
            return cause
    return AskCause.VAGUE


def _reason_of(d: Any) -> Reason | None:
    raw = d.get("reason") if isinstance(d, Mapping) else getattr(d, "reason", None)
    try:
        return Reason(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Per-outcome lines (spec §5.4) and per-reason lines (§5.5)
# --------------------------------------------------------------------------
# Placeholders: {S} subject ("Slide 3", "Slide 3 image", "Caption", "New slide after 3"),
# {L} plain "Slide n", {Sw} "Slide n (was)", {name} the new theme, {N_clause} " — the deck
# has N slides" or "", {themes_clause} " Available: a, b." or "". Values are code-built or
# sanitised; templates are code-owned.

#: (code, op, reason) -> Row. ``None`` is a wildcard for op / reason.
OUTCOME_COPY: Mapping[tuple[OutcomeCode, str | None, str | None], Row] = {
    # ---- applied: every op has its own line (a new Op without one fails a test)
    (OutcomeCode.APPLIED, Op.EDIT_SLIDE, None): _r(Mark.OK, "{S} — reworded"),
    (OutcomeCode.APPLIED, Op.ADD_SLIDE, None): _r(Mark.OK, "{S} — added"),
    (OutcomeCode.APPLIED, Op.REMOVE_SLIDE, None): _r(Mark.OK, "{Sw} — removed"),
    (OutcomeCode.APPLIED, Op.REORDER, None): _r(Mark.OK, "Slide order — changed"),
    (OutcomeCode.APPLIED, Op.SET_THEME, None): _r(Mark.OK, "Theme — now {name}"),
    (OutcomeCode.APPLIED, Op.SET_THEME, WARN_TEXT_ONLY_THEME): _r(
        Mark.OK, "Theme — now {name}; photos won't show on this theme"),
    (OutcomeCode.APPLIED, Op.REMOVE_IMAGE, None): _r(Mark.OK, "{S} — removed"),
    (OutcomeCode.APPLIED, Op.REPLACE_IMAGE, None): _r(Mark.OK, "{S} — replaced"),
    (OutcomeCode.APPLIED, Op.REPLACE_IMAGE, REASON_IMAGE_UNCHECKED): _r(
        Mark.UNKNOWN, "{S} — replaced, but I couldn't check the new photo; please look at it"),
    (OutcomeCode.APPLIED, Op.RESEARCH, None): _r(Mark.OK, "Research — new findings added"),
    (OutcomeCode.APPLIED, Op.FIX_FACT, None): _r(Mark.OK, "{S} — figure checked and corrected"),
    (OutcomeCode.APPLIED, Op.REWRITE_CAPTION, None): _r(Mark.OK, "Caption — rewritten"),
    (OutcomeCode.APPLIED, Op.FULL_RECOMPOSE, None): _r(Mark.OK, "Whole deck — rebuilt"),
    (OutcomeCode.APPLIED, CODE_ANY, None): _r(Mark.OK, "{S} — done"),
    # ---- unchanged
    (OutcomeCode.UNCHANGED, CODE_ANY, None): _r(Mark.BAD, "{S} — came out the same; kept as is"),
    (OutcomeCode.UNCHANGED, Op.FIX_FACT, None): _r(Mark.BAD, "{S} — no change made"),
    (OutcomeCode.UNCHANGED, Op.SET_THEME, None): _r(Mark.NEUTRAL, "Theme — it was already that one"),
    (OutcomeCode.UNCHANGED, CODE_ANY, REASON_ALREADY_REMOVED): _r(Mark.NEUTRAL, "{L} was already removed."),
    (OutcomeCode.UNCHANGED, Op.REPLACE_IMAGE, REASON_SAME_IMAGE): _r(
        Mark.BAD, "{S} — the only usable photo is the one already there; kept it"),
    (OutcomeCode.UNCHANGED, CODE_ANY, REASON_MODEL_UNUSABLE): _r(
        Mark.BAD, "{S} — the model's answer wasn't usable; kept as is"),
    # ---- not found
    (OutcomeCode.NOT_FOUND, CODE_ANY, None): _r(Mark.BAD, "{S} — not found; nothing changed"),
    (OutcomeCode.NOT_FOUND, Op.REPLACE_IMAGE, None): _r(Mark.BAD, "{S} — no usable photo found; kept the old one"),
    (OutcomeCode.NOT_FOUND, Op.REPLACE_IMAGE, REASON_NO_CANDIDATES): _r(
        Mark.BAD, "{S} — no photo found; kept the old one"),
    (OutcomeCode.NOT_FOUND, Op.REPLACE_IMAGE, REASON_NO_USABLE_IMAGE): _r(
        Mark.BAD, "{S} — found photos, but none were usable; kept the old one"),
    (OutcomeCode.NOT_FOUND, Op.RESEARCH, None): _r(Mark.BAD, "Research — nothing useful found"),
    (OutcomeCode.NOT_FOUND, Op.RESEARCH, REASON_NO_RESULTS): _r(Mark.BAD, "Research — nothing useful found"),
    (OutcomeCode.NOT_FOUND, Op.RESEARCH, REASON_NOT_ANSWERED): _r(
        Mark.BAD, "Research — what I found didn't answer it; nothing added"),
    (OutcomeCode.NOT_FOUND, Op.FIX_FACT, None): _r(Mark.BAD, "{S} — couldn't find a source to check it; left as is"),
    (OutcomeCode.NOT_FOUND, CODE_ANY, REASON_SEARCH_BLOCKED): _r(
        Mark.BAD, "{S} — web search is blocked right now, so I didn't try. Send it again later."),
    # ---- rejected by the integrity check
    (OutcomeCode.REJECTED_INTEGRITY, CODE_ANY, None): _r(
        Mark.BAD, "{S} — failed the fact check; kept as is"),
    (OutcomeCode.REJECTED_INTEGRITY, Op.EDIT_SLIDE, None): _r(
        Mark.BAD, "{S} — new wording had claims your sources don't back; kept the old text"),
    (OutcomeCode.REJECTED_INTEGRITY, Op.ADD_SLIDE, None): _r(
        Mark.BAD, "{S} — had claims your sources don't back; not added"),
    (OutcomeCode.REJECTED_INTEGRITY, Op.FIX_FACT, None): _r(
        Mark.BAD, "{S} — corrected figure wasn't backed by a source; left as is"),
    (OutcomeCode.REJECTED_INTEGRITY, Op.REWRITE_CAPTION, None): _r(
        Mark.BAD, "Caption — new version had unbacked claims; kept the old one"),
    (OutcomeCode.REJECTED_INTEGRITY, Op.REPLACE_IMAGE, None): _r(
        Mark.BAD, "{S} — the new photo failed the relevance check; kept the old one"),
    (OutcomeCode.REJECTED_INTEGRITY, CODE_ANY, REASON_EMPTY_SLIDE): _r(
        Mark.BAD, "{S} — the model's slide came back with no content in it; nothing was added. Try asking again"),
    # ---- refused to keep the deck valid
    (OutcomeCode.REFUSED_INVARIANT, CODE_ANY, None): _r(Mark.BAD, "{S} — couldn't be done without breaking the deck"),
    (OutcomeCode.REFUSED_INVARIANT, Op.REMOVE_SLIDE, REASON_MIN_SLIDES): _r(
        Mark.BAD, "{S} — not removed: the deck needs at least {MIN_SLIDES} slides"),
    (OutcomeCode.REFUSED_INVARIANT, Op.ADD_SLIDE, REASON_MAX_SLIDES): _r(
        Mark.BAD, "{S} — not added: {MAX_SLIDES} slides is the most"),
    (OutcomeCode.REFUSED_INVARIANT, Op.REMOVE_SLIDE, REASON_HOOK): _r(
        Mark.BAD, "{S} — not removed: the first slide stays"),
    (OutcomeCode.REFUSED_INVARIANT, Op.REORDER, None): _r(
        Mark.BAD, "Slide order — unchanged: the first slide stays first, closing slides stay last"),
    (OutcomeCode.REFUSED_INVARIANT, Op.REORDER, REASON_HOOK): _r(
        Mark.BAD, "Slide order — unchanged: the first slide stays first, closing slides stay last"),
    (OutcomeCode.REFUSED_INVARIANT, Op.REMOVE_IMAGE, REASON_BODYLESS): _r(
        Mark.BAD, "{S} — not removed: it's all that slide has"),
    (OutcomeCode.REFUSED_INVARIANT, CODE_ANY, REASON_CTA_DROPPED): _r(
        Mark.BAD, "{S} — not done: it would have removed a slide you didn't ask to remove"),
    (OutcomeCode.REFUSED_INVARIANT, CODE_ANY, REASON_CLOSING_CLAMP): _r(
        Mark.BAD, "{S} — not done: it would have squeezed out other slides"),
    # ---- a checker's own fault
    (OutcomeCode.UNAVAILABLE, CODE_ANY, None): _r(
        Mark.UNKNOWN, "{S} — changed, but I couldn't check it; please read it"),
    (OutcomeCode.UNAVAILABLE, Op.RESEARCH, REASON_SOURCE_UNVERIFIED): _r(
        Mark.UNKNOWN, "Research — found something, but couldn't vet the source; not added"),
    # ---- never in a preview (a standalone message, DEFERRED)
    (OutcomeCode.DEFERRED, CODE_ANY, None): HIDDEN,
    # ---- the operator said No
    (OutcomeCode.DECLINED, CODE_ANY, None): _r(Mark.NEUTRAL, "{S}: kept (you said no)"),
    (OutcomeCode.DECLINED, Op.REMOVE_SLIDE, None): _r(Mark.NEUTRAL, "{L}: kept (you said no)"),
    (OutcomeCode.DECLINED, Op.FULL_RECOMPOSE, None): _r(Mark.NEUTRAL, "Whole-deck rebuild: not run (you said no)"),
    # ---- dropped before running: by reason (REASON_COPY), else this
    (OutcomeCode.DROPPED_INVALID, CODE_ANY, None): _r(Mark.BAD, "Part of your note couldn't be turned into a clear edit — skipped."),
    (OutcomeCode.DROPPED_INVALID, Op.FULL_RECOMPOSE, REASON_GLOBAL_OP): _r(
        Mark.BAD, "Whole-deck rebuild — can't run together with edits; skipped"),
    (OutcomeCode.DROPPED_UNGROUNDED, CODE_ANY, None): _r(Mark.BAD, "Part of your note couldn't be turned into a clear edit — skipped."),
    (OutcomeCode.DROPPED_CONFLICT, CODE_ANY, None): _r(Mark.BAD, "Part of your note conflicted with another part — skipped."),
}

_UNCLEAR_LINE = "Part of your note couldn't be turned into a clear edit — skipped."
#: ``Reason`` -> Row, for planner drops AND executor outcomes that carry a Reason value.
REASON_COPY: Mapping[Reason, Row] = {
    Reason.MALFORMED: _r(Mark.BAD, _UNCLEAR_LINE),
    Reason.UNKNOWN_OP: _r(Mark.BAD, _UNCLEAR_LINE),
    Reason.MISSING_ARG: _r(Mark.BAD, _UNCLEAR_LINE),
    Reason.OUT_OF_RANGE: _r(Mark.BAD, "{L} doesn't exist{N_clause}."),
    Reason.CLOSING_SLIDE: _r(Mark.BAD, "{L} is added automatically and can't be edited."),
    Reason.NO_IMAGE: _r(Mark.BAD, "{L} has no image to remove."),
    Reason.BAD_THEME: _r(Mark.BAD, "No theme by that name.{themes_clause}"),
    Reason.BAD_ORDER: _r(Mark.BAD, "Couldn't work out the new order; unchanged. Try \"swap slides 2 and 5\"."),
    Reason.NOOP_REORDER: _r(Mark.NEUTRAL, "Slides were already in that order."),
    Reason.DUPLICATE: HIDDEN,  # asked twice, done once
    Reason.TARGET_REMOVED: _r(Mark.NEUTRAL, "{L} edit skipped: that slide was removed."),
    Reason.RESEARCH_CAP: _r(
        Mark.BAD, "Only {MAX_RESEARCH_ACTIONS} lookups per revision; skipped one. Send it as a new note."),
    Reason.RECOMPOSE_NOT_ALONE: _r(
        Mark.NEUTRAL, "Full rebuild not run: your note also asked for specific edits, so I did only those."),
    Reason.MAX_ACTIONS: _r(
        Mark.BAD, "Too many changes in one note — did the first {MAX_ACTIONS}. Send the rest as a new note."),
    Reason.MAX_ASKS: _r(
        Mark.BAD, "Too many separate requests in one note — did the first {MAX_ASKS}. Send the rest as a new note."),
    # Model over-reach or an unclear note, not something the operator asked for and did not get.
    Reason.NO_SLIDE_REFERENCE: HIDDEN,
    Reason.NO_TRIGGER: HIDDEN,
    Reason.THEME_NOT_NAMED: HIDDEN,
    Reason.SCOPED_NOTE: HIDDEN,
    Reason.RULES_ONLY: HIDDEN,
    Reason.NEGATED: HIDDEN,
    Reason.CONSUMED_BY_RULES: HIDDEN,
}

UNKNOWN_OUTCOME = _r(Mark.UNKNOWN, "{S} — I couldn't tell what happened; please read it")

#: The closed ``Outcome.reason`` codes the executors can write, beyond ``Reason`` values.
#: ``tests`` also scans the executor modules for any ``REASON_*`` / ``WARN_*`` not listed.
EXECUTOR_REASONS: frozenset[str] = frozenset({
    REASON_SEARCH_BLOCKED, REASON_MODEL_UNUSABLE, REASON_GLOBAL_OP,
    REASON_NO_RESULTS, REASON_NOT_ANSWERED, REASON_SOURCE_UNVERIFIED, REASON_NO_CANDIDATES,
    REASON_NO_USABLE_IMAGE, REASON_SAME_IMAGE, REASON_IMAGE_UNCHECKED,
    REASON_MIN_SLIDES, REASON_MAX_SLIDES, REASON_HOOK, REASON_BODYLESS, REASON_CTA_DROPPED,
    REASON_CLOSING_CLAMP, REASON_ALREADY_REMOVED, WARN_TEXT_ONLY_THEME,
    REASON_EMPTY_SLIDE,
})


def resolve(code: Any, op: Any, reason: Any) -> Row:
    """The copy row for one action outcome. Most specific first:
    (code, op, reason) -> (code, *, reason) -> the reason's own line -> (code, op) -> (code)."""
    try:
        code = OutcomeCode(code)
    except ValueError:
        return UNKNOWN_OUTCOME
    op = op if op in {o.value for o in Op} else CODE_ANY
    reason = reason if isinstance(reason, str) and reason else None
    keys: list[tuple[OutcomeCode, str | None, str | None]] = []
    if reason:
        keys += [(code, op, reason), (code, CODE_ANY, reason)]
    for key in keys:
        if key in OUTCOME_COPY:
            return OUTCOME_COPY[key]
    if reason and code is not OutcomeCode.APPLIED:
        try:
            return REASON_COPY[Reason(reason)]
        except ValueError:
            pass
    for key in ((code, op, None), (code, CODE_ANY, None)):
        if key in OUTCOME_COPY:
            return OUTCOME_COPY[key]
    return UNKNOWN_OUTCOME  # pragma: no cover - every code has a wildcard row (tested)


# ---- notes on what an op is called in a line ("subject")

_NOUN_BY_OP: Mapping[str, str] = {
    Op.REORDER: "Slide order",
    Op.SET_THEME: "Theme",
    Op.REWRITE_CAPTION: "Caption",
    Op.RESEARCH: "Research",
    Op.FULL_RECOMPOSE: "Whole deck",
}
_IMAGE_OPS = frozenset({Op.REMOVE_IMAGE, Op.REPLACE_IMAGE})
#: What a collapsed "✓ N changes applied: ..." names for an op that has no slide.
_SHORT_BY_OP: Mapping[str, str] = {
    Op.REORDER: "slide order", Op.SET_THEME: "theme", Op.REWRITE_CAPTION: "caption",
    Op.RESEARCH: "research", Op.FULL_RECOMPOSE: "whole deck",
}
#: A held / planned action in a "Also doing:" list (spec §3.1, §5).
_ALSO_DOING: Mapping[str, str] = {
    Op.EDIT_SLIDE: "slide {n} reworded",
    Op.ADD_SLIDE: "new slide after {n}",
    Op.REMOVE_SLIDE: "slide {n} removed",
    Op.REORDER: "slides reordered",
    Op.SET_THEME: "theme changed",
    Op.REMOVE_IMAGE: "slide {n} image removed",
    Op.REPLACE_IMAGE: "slide {n} image replaced",
    Op.RESEARCH: "new research added",
    Op.FIX_FACT: "slide {n} figure checked",
    Op.REWRITE_CAPTION: "caption rewritten",
    Op.FULL_RECOMPOSE: "whole deck rebuilt",
}


@dataclass(frozen=True)
class Numbering:
    """Old (the operator's) slide numbers -> the new deck's, from what the run
    removed / added. Exact when the run did not reorder; a reorder keeps the old
    numbers (the header says so)."""

    removed: frozenset[int] = frozenset()
    added: frozenset[int] = frozenset()
    reordered: bool = False

    @property
    def moved(self) -> bool:
        return bool(self.removed or self.added or self.reordered)

    def new(self, old: int) -> int:
        if old <= 0 or self.reordered or old in self.removed or not (self.removed or self.added):
            return old
        rank = old - sum(1 for r in self.removed if r < old)
        n = k = 0
        while True:
            n += 1
            if n in self.added:
                continue
            k += 1
            if k == rank:
                return n


@dataclass(frozen=True)
class _Ctx:
    num: Numbering
    deck: Sequence[Mapping[str, Any]] | None
    themes: Sequence[str]
    deck_size_then: int | None  # the deck's size when the plan was made

    def headline(self, new_number: int) -> str:
        if not self.deck or self.num.reordered or not 1 <= new_number <= len(self.deck):
            return ""
        slide = self.deck[new_number - 1]
        return clean(slide.get("headline") if isinstance(slide, Mapping) else "", HEADLINE_QUOTE_MAX)


def _label(ctx: _Ctx, slide: int, *, was: bool = False, headline: bool = False) -> str:
    """``Slide n`` (the NEW number), ``Slide n (was)`` for a removed one, with the
    headline when numbers moved and the line says something is wrong."""
    if slide <= 0:
        return "A slide"
    if was or slide in ctx.num.removed:
        return f"Slide {slide} (was)"
    n = ctx.num.new(slide)
    text = ctx.headline(n) if headline and ctx.num.moved else ""
    return f"Slide {n} \"{text}\"" if text else f"Slide {n}"


def _values(ctx: _Ctx, op: str, slide: int, *, name: str, headline: bool) -> dict[str, Any]:
    plain = _label(ctx, slide, headline=headline)
    if op in _NOUN_BY_OP:
        subject = _NOUN_BY_OP[op]
    elif op == Op.ADD_SLIDE:
        subject = f"New slide after {ctx.num.new(slide)}" if slide > 0 else "New slide"
    elif op in _IMAGE_OPS:
        subject = f"{plain} image"
    else:
        subject = plain
    themes = _themes_text(ctx.themes)
    return {
        "S": subject, "L": plain, "Sw": _label(ctx, slide, was=True), "name": name or "the new one",
        "N_clause": (f" — the deck has {ctx.deck_size_then} slides" if ctx.deck_size_then else ""),
        "themes_clause": (f" Available: {themes}." if themes else ""),
        "MIN_SLIDES": MIN_SLIDES, "MAX_SLIDES": MAX_SLIDES, "MAX_ACTIONS": MAX_ACTIONS,
        "MAX_ASKS": MAX_ASKS, "MAX_RESEARCH_ACTIONS": MAX_RESEARCH_ACTIONS,
    }


def _themes_text(themes: Sequence[str]) -> str:
    return _cut(", ".join(t for t in (clean(x, 30) for x in themes) if t), THEMES_CLAUSE_MAX)


@dataclass(frozen=True)
class Line:
    mark: Mark
    text: str
    slide: int = 0  # NEW number (0: not about one slide) - sort key
    short: str = ""  # for the collapsed "✓ N changes applied" line

    @property
    def rendered(self) -> str:
        return _cut(f"{self.mark.value} {self.text}", CHANGE_LINE_MAX)


def _row_line(ctx: _Ctx, row: Row, op: str, slide: int, *, name: str = "") -> Line | None:
    if row.mark is Mark.HIDE:
        return None
    bad = row.mark in (Mark.BAD, Mark.UNKNOWN)
    text = row.text.format(**_values(ctx, op, slide, name=name, headline=bad))
    new = ctx.num.new(slide) if slide > 0 and slide not in ctx.num.removed else 0
    short = _SHORT_BY_OP.get(op, "") if slide <= 0 else ""
    return Line(row.mark, text, new, short)


# --------------------------------------------------------------------------
# The Changes block (spec §5)
# --------------------------------------------------------------------------

HEADER_BY_STATUS: Mapping[CheckStatus, str] = {
    CheckStatus.SATISFIED: "Revised per your note: {note}",
    CheckStatus.PARTIAL: "Partly revised — {note}",
    CheckStatus.UNSATISFIED: "Couldn't do what you asked — {note}",
    CheckStatus.UNCHECKED: "Revised, not verified — {note}",
}
HEADER_NEUTRAL = "Your note: {note}"  # a run with no check (the operator declined everything else)
HEADER_FALLBACK = "Rebuilt the whole deck with your note: {note}"
HEADER_NO_NOTE = "Fresh version (no note)."
HEADER_OVERFLOW = "A slide didn't fit, so the deck was rewritten with tighter text."
CLIPPED_LINE = "Your note was longer than {limit} characters; I only read the start."
RENUMBER_LINE = "Slide numbers below are the new ones."
REORDERED_LINE = "Slides were reordered; numbers below are the ones in your note."
FALLBACK_CAUSE_LINES: Mapping[str, str] = {
    "skip": "You asked for a fresh version.",
    "no_slides": "I couldn't plan targeted edits, so I rebuilt it.",
    "plan_failed": "I couldn't plan targeted edits, so I rebuilt it.",
    "rebuild": "Your note changes the whole deck, so I rebuilt it.",
    "overflow": "",
}
FALLBACK_COMPARE_LINE = "Every slide is new — compare before approving."
FALLBACK_UNCHECKED_LINE = "? Not checked against your note — read it through."
CHECK_SATISFIED_ALL = "Checked against your note: all {n} done."
CHECK_SATISFIED_ONE = "Checked against your note: done."
CHECK_PARTIAL = "Checked against your note: {k} of {n} done. Not done: {missing}."
CHECK_PARTIAL_NAMED = "Checked against your note: partly done. Not done: {missing}."
CHECK_UNSATISFIED = "Checked against your note: not done. Missing: {missing}."
CHECK_UNSATISFIED_BARE = "Checked against your note: not done."
CHECK_UNCHECKED = "? Couldn't check this against your note — read the slides before approving."
MORE_NOT_DONE = "+{n} more not done"
MORE_ASKS = "+{n} more"
SOFT_UNADDRESSED = "? I may have missed: {asks}."  # D281: model text, phrased softly
UNTOUCHED_SOME = "Unchanged: {which}"
UNTOUCHED_NONE_CHANGED = "Slides unchanged."
NOTHING_CHANGED = "Nothing changed."
COLLAPSED_OK = "{k} changes applied: {what}"


def _status_of(run: Mapping[str, Any]) -> CheckStatus | None:
    try:
        status = CheckStatus(run.get("check"))
    except ValueError:
        return None
    # A note cut at the parse cap was never fully read: it is never "checked" (AC-8).
    if run.get("clipped") and status in (CheckStatus.SATISFIED, CheckStatus.PARTIAL):
        return CheckStatus.UNCHECKED
    return status


def _note_quote(run: Mapping[str, Any], prefix_len: int) -> str:
    cap = max(10, min(NOTE_QUOTE_MAX, CHANGE_LINE_MAX - prefix_len - 2))
    return quote(run.get("note"), cap)


def _header(run: Mapping[str, Any], status: CheckStatus | None) -> str:
    note = run.get("note") if isinstance(run.get("note"), str) and run.get("note").strip() else None
    if run.get("fallback"):
        if run.get("cause") == "overflow":
            return HEADER_OVERFLOW
        if note is None:
            return HEADER_NO_NOTE
        template = HEADER_FALLBACK
    elif note is None:
        return HEADER_NO_NOTE
    else:
        template = HEADER_BY_STATUS.get(status, HEADER_NEUTRAL) if status else HEADER_NEUTRAL
    return template.format(note=_note_quote(run, len(template) - len("{note}")))


def _asks_text(run: Mapping[str, Any], indexes: Sequence[int]) -> str:
    asks = run.get("asks") if isinstance(run.get("asks"), list) else []
    named = [quote(asks[i], ASK_QUOTE_MAX) for i in indexes if 0 <= i < len(asks) and clean(asks[i], 1)]
    shown = named[:MAX_QUOTED_ASKS]
    text = "; ".join(shown)
    if len(named) > MAX_QUOTED_ASKS:
        text += "; " + MORE_ASKS.format(n=len(named) - MAX_QUOTED_ASKS)
    return text


def _declined_asks(run: Mapping[str, Any]) -> set[int]:
    """Asks whose every action the operator declined: kept, not 'done' and not 'missed'."""
    by_ask: dict[int, list[bool]] = {}
    for r in _mappings(run.get("actions")):
        by_ask.setdefault(_int(r.get("ask")), []).append(r.get("outcome") == OutcomeCode.DECLINED.value)
    return {ask for ask, declined in by_ask.items() if all(declined)}


def _check_line(run: Mapping[str, Any], status: CheckStatus | None) -> str | None:
    asks = run.get("asks") if isinstance(run.get("asks"), list) else []
    declined = _declined_asks(run)
    n = len(asks) - len([i for i in declined if i < len(asks)])
    bad = [i for i in _ints(run.get("unsatisfied")) if i < len(asks) and i not in declined]
    if status is CheckStatus.SATISFIED:
        return CHECK_SATISFIED_ALL.format(n=n) if n > 1 else CHECK_SATISFIED_ONE
    if status is CheckStatus.PARTIAL:
        missing = _asks_text(run, bad)
        if missing and n:
            return CHECK_PARTIAL.format(k=max(0, n - len(bad)), n=n, missing=missing)
        named = _missed_text(run)
        return CHECK_PARTIAL_NAMED.format(missing=named) if named else "Checked against your note: partly done."
    if status is CheckStatus.UNSATISFIED:
        missing = _asks_text(run, bad or [i for i in range(len(asks)) if i not in declined])
        return CHECK_UNSATISFIED.format(missing=missing) if missing else CHECK_UNSATISFIED_BARE
    if status is CheckStatus.UNCHECKED:
        return CHECK_UNCHECKED
    return None


def _missed_text(run: Mapping[str, Any]) -> str:
    """The slides / themes the note named outright and the run left alone."""
    parts = []
    slides = _ints(run.get("missed_slides"))
    if slides:
        parts.append(_slide_word(slides))
    themes = [t for t in (clean(x, 30) for x in (run.get("missed_themes") or []) if isinstance(x, str)) if t]
    if themes:
        parts.append("theme " + ", ".join(quote(t, 30) for t in themes[:2]))
    return "; ".join(parts)


@dataclass(frozen=True)
class ChangesBlock:
    """The Changes block plus whether the caller must send it as its OWN message
    because even the shortest form does not fit beside the caption (§5.9 step 7)."""

    text: str
    standalone: bool = False


@dataclass(frozen=True)
class _Parts:
    header: str
    notes: tuple[str, ...]  # clipped line, renumber line
    ok: tuple[Line, ...]
    bad: tuple[Line, ...]  # ✗ ? – lines, not-done first
    soft: tuple[str, ...]
    check: str | None
    untouched: str | None
    body: tuple[str, ...] = ()  # a fallback run's body instead of ok/bad/untouched

    def assemble(self, level: int) -> str:
        """level 0 full; 1 ✓ lines collapsed; 2 also drop soft + untouched; 3 header + check."""
        out = [_cut(self.header, CHANGE_LINE_MAX)]
        if level >= 3:
            if self.check:
                out.append(_cut(self.check, CHANGE_LINE_MAX))
            return "\n".join(out)
        out += [_cut(n, CHANGE_LINE_MAX) for n in self.notes]
        if self.body:
            out += [_cut(b, CHANGE_LINE_MAX) for b in self.body]
            return "\n".join(out)
        ok = self.ok
        if level >= 1 and len(ok) > 1:
            ok = (_collapse(ok),)
        out += [line.rendered for line in ok]
        bad = list(self.bad)
        not_done = [b for b in bad if b.mark is not Mark.NEUTRAL]
        neutral = [b for b in bad if b.mark is Mark.NEUTRAL]
        shown = not_done[:MAX_NOT_DONE_LINES]
        out += [b.rendered for b in shown]
        if len(not_done) > MAX_NOT_DONE_LINES:
            out.append(MORE_NOT_DONE.format(n=len(not_done) - MAX_NOT_DONE_LINES))
        out += [b.rendered for b in neutral]
        if level < 2:
            out += [_cut(s, CHANGE_LINE_MAX) for s in self.soft]
        if self.check:
            out.append(_cut(self.check, CHANGE_LINE_MAX))
        if level < 2 and self.untouched:
            out.append(_cut(self.untouched, CHANGE_LINE_MAX))
        return "\n".join(out)


def _collapse(ok: Sequence[Line]) -> Line:
    slides = sorted({line.slide for line in ok if line.slide})
    names = [line.short for line in ok if not line.slide and line.short]
    what = "; ".join(p for p in (_slide_word(slides) if slides else "", ", ".join(dict.fromkeys(names))) if p)
    return Line(Mark.OK, COLLAPSED_OK.format(k=len(ok), what=what))


def _unchanged_line(run: Mapping[str, Any], ctx: _Ctx, any_change: bool) -> str | None:
    if not ctx.deck or ctx.num.reordered:
        return None
    changed = set(_ints(run.get("changed"))) | set(_ints(run.get("added")))
    n = len(ctx.deck)
    if not changed and not ctx.num.removed:
        return UNTOUCHED_NONE_CHANGED if any_change else NOTHING_CHANGED
    rest = [i for i in range(1, n + 1) if i not in changed]
    if not rest:
        return None  # every slide changed
    return UNTOUCHED_SOME.format(which=_slide_word(rest))


def build_parts(
    run: Any, *, slides: Sequence[Mapping[str, Any]] | None = None, themes: Sequence[str] = (),
) -> _Parts | None:
    """The pieces of a run's Changes block, or None when it has nothing to say (not a
    run, an ask-the-operator run, or a run in which nothing happened and nothing was refused)."""
    if not isinstance(run, Mapping) or run.get("ask_operator"):
        return None
    removed, added = _ints(run.get("removed")), _ints(run.get("added"))
    reordered = bool(run.get("reordered"))
    num = Numbering(frozenset(removed), frozenset(added), reordered)
    deck = list(slides) if slides else None
    then = len(deck) - len(added) + len(removed) if deck else None
    ctx = _Ctx(num, deck, tuple(themes), then if then and then > 0 else None)
    status = _status_of(run)
    fallback = bool(run.get("fallback"))
    header = _header(run, status)
    notes: list[str] = []
    if run.get("clipped"):
        notes.append(CLIPPED_LINE.format(limit=MAX_NOTE_CHARS))
    check = _check_line(run, status)

    if fallback:
        has_note = isinstance(run.get("note"), str) and bool(run["note"].strip())
        cause = FALLBACK_CAUSE_LINES.get(str(run.get("cause")), "")
        body = [cause] if cause else []
        body.append(FALLBACK_COMPARE_LINE)
        if check:
            body.append(check)
        elif has_note and run.get("cause") != "overflow":
            body.append(FALLBACK_UNCHECKED_LINE)
        return _Parts(header, tuple(notes), (), (), (), None, None, tuple(body))

    if num.reordered:
        notes.append(REORDERED_LINE)
    elif num.moved:
        notes.append(RENUMBER_LINE)

    ok: list[Line] = []
    bad: list[Line] = []
    rows = _mappings(run.get("actions"))
    applied_keys = {
        (r.get("op"), _int(r.get("slide"))) for r in rows if r.get("outcome") == OutcomeCode.APPLIED.value
    }
    for r in rows:
        op, slide = str(r.get("op")), _int(r.get("slide"))
        if r.get("outcome") == OutcomeCode.UNCHANGED.value and (op, slide) in applied_keys:
            continue  # a duplicate planned action; the first one did the work (T301 tester)
        row = resolve(r.get("outcome"), op, r.get("reason"))
        line = _row_line(ctx, row, op, slide, name=clean(run.get("theme"), 30) if op == Op.SET_THEME else "")
        if line is not None:
            (ok if line.mark is Mark.OK else bad).append(line)
    for d in _mappings(run.get("dropped")):
        try:
            reason = Reason(d.get("reason"))
        except ValueError:
            continue
        line = _row_line(ctx, REASON_COPY[reason], str(d.get("op")), _int(d.get("slide")))
        if line is not None:
            bad.append(line)

    ok = _ordered(ok)
    bad = _ordered(_dedupe(bad))
    soft: list[str] = []
    unaddressed = [clean(a, ASK_QUOTE_MAX) for a in run.get("unaddressed") or [] if isinstance(a, str)]
    unaddressed = [a for a in unaddressed if a]
    if unaddressed:
        shown = "; ".join(quote(a, ASK_QUOTE_MAX) for a in unaddressed[:MAX_QUOTED_ASKS])
        if len(unaddressed) > MAX_QUOTED_ASKS:
            shown += "; " + MORE_ASKS.format(n=len(unaddressed) - MAX_QUOTED_ASKS)
        soft.append(SOFT_UNADDRESSED.format(asks=shown))

    if not (ok or bad or soft):
        return None
    any_change = bool(
        run.get("changed") or run.get("added") or removed or reordered
        or run.get("caption_changed") or run.get("theme")
    )
    return _Parts(
        header, tuple(notes), tuple(ok), tuple(bad), tuple(soft), check,
        _unchanged_line(run, ctx, any_change),
    )


def _dedupe(lines: Iterable[Line]) -> list[Line]:
    seen: set[str] = set()
    out = []
    for line in lines:
        if line.text not in seen:
            seen.add(line.text)
            out.append(line)
    return out


def _ordered(lines: Iterable[Line]) -> list[Line]:
    """By slide number, then the lines that are not about one slide (stable)."""
    lines = list(lines)
    return sorted(lines, key=lambda line: (line.slide == 0, line.slide)) if lines else []


def fit_changes(
    run: Any, *, slides: Sequence[Mapping[str, Any]] | None = None, themes: Sequence[str] = (),
    budget: int = CHANGES_MAX_CHARS,
) -> ChangesBlock:
    """The Changes block within ``budget`` characters (never more than ``CHANGES_MAX_CHARS``).

    Squeezed in this order until it fits (§5.9): every line cut at ``CHANGE_LINE_MAX`` with
    an ellipsis at a word; ✓ lines collapsed into one; the soft "may have missed" and the
    unchanged lines dropped. Header, check line and ✗/? lines survive all of those. Only
    when the budget is under ``LAST_RESORT_BUDGET`` is the block cut to header + check line.
    If the squeezed block still does not fit, ``standalone`` is True and the block (squeezed
    to ``CHANGES_MAX_CHARS`` only) is returned for the caller to send as its own message,
    so a not-done line is never dropped just because the caption is long."""
    parts = build_parts(run, slides=slides, themes=themes)
    if parts is None:
        return ChangesBlock("")
    budget = min(budget, CHANGES_MAX_CHARS)
    for cap, standalone in ((budget, False), (CHANGES_MAX_CHARS, True)):
        for level in (0, 1, 2) if cap >= LAST_RESORT_BUDGET else (3,):
            text = parts.assemble(level)
            if len(text) <= cap:
                return ChangesBlock(text, standalone)
    return ChangesBlock(parts.assemble(2), standalone=True)  # over 1200 even squeezed: not reachable (lines are capped)


def render_changes(
    run: Any, *, slides: Sequence[Mapping[str, Any]] | None = None, themes: Sequence[str] = (),
    budget: int = CHANGES_MAX_CHARS,
) -> str:
    """``fit_changes(...).text`` for callers that do not care about the standalone flag
    (the dashboard, which has no 4096 limit to share)."""
    return fit_changes(run, slides=slides, themes=themes, budget=budget).text


def preview_budget(rest_len: int, limit: int = TELEGRAM_TEXT_LIMIT) -> int:
    """Characters the Changes block may use beside ``rest_len`` characters of caption + stats."""
    return min(CHANGES_MAX_CHARS, limit - rest_len - PREVIEW_SEPARATOR_ALLOWANCE)


# --------------------------------------------------------------------------
# Confirmation prompt (spec §3)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfirmView:
    text: str
    yes: str
    no: str
    what: str  # "slide 4" / "slides 2, 3" / "3 slides" / "the whole deck" - for the answered edit
    rebuild: bool


def _headline_at(slides: Sequence[Mapping[str, Any]] | None, number: int) -> str:
    if not slides or not 1 <= number <= len(slides) or not isinstance(slides[number - 1], Mapping):
        return ""
    return clean(slides[number - 1].get("headline"), HEADLINE_QUOTE_MAX)


def _what(held: Sequence[Action]) -> str:
    numbers = sorted({a.slide for a in held if a.op == Op.REMOVE_SLIDE and a.slide > 0})
    if not numbers:
        return "the slide"
    if len(numbers) > MAX_CONFIRM_SLIDE_LIST:
        return f"{len(numbers)} slides"
    return ("slide " if len(numbers) == 1 else "slides ") + ", ".join(map(str, numbers))


def _also(rest: Sequence[Action]) -> str:
    phrases = [_ALSO_DOING.get(a.op, "an edit").format(n=a.slide) for a in rest]
    shown = phrases[:MAX_ALSO_DOING]
    text = ", ".join(shown)
    if len(phrases) > MAX_ALSO_DOING:
        text += ", " + MORE_ASKS.format(n=len(phrases) - MAX_ALSO_DOING)
    return text


def confirm_view(
    held: Sequence[Action], rest: Sequence[Action] = (), *,
    slides: Sequence[Mapping[str, Any]] | None = None, note: str | None = None,
) -> ConfirmView:
    """The Yes/No question for the destructive actions in a plan (§3.1-3.2)."""
    if any(a.op == Op.FULL_RECOMPOSE for a in held):
        lines = [CONFIRM_REBUILD_HEAD]
        if isinstance(note, str) and note.strip():
            lines.append("Your note: " + quote(note, NOTE_QUOTE_MAX))
        return ConfirmView("\n".join(lines), BUTTON_YES_REBUILD, BUTTON_NO_KEEP_DECK, "the whole deck", True)
    what = _what(held)
    numbers = sorted({a.slide for a in held if a.slide > 0})
    lines = [CONFIRM_REMOVE_HEAD.format(what=what)]
    for n in numbers:
        headline = _headline_at(slides, n)
        lines.append(f"Slide {n} \"{headline}\"" if headline else f"Slide {n}")
    if rest:
        lines.append(f"Also doing: {_also(rest)}.")
    many = len(numbers) > 1
    return ConfirmView(
        "\n".join(lines),
        f"{BUTTON_YES_REMOVE} {len(numbers)}" if many else BUTTON_YES_REMOVE,
        BUTTON_NO_KEEP_THEM if many else BUTTON_NO_KEEP_IT,
        what, False,
    )


def confirm_view_for(item: Any) -> ConfirmView | None:
    """The Yes/No for an item as its stored plan asks it (None when nothing is pending or
    the plan is unreadable). One builder for the notifier-less surfaces: the answered-edit
    in the bot and the dashboard's pending-plan card."""
    request = getattr(item, "revision_request", None) or {}
    pc = pending_confirm(request)
    if pc is None:
        return None
    actions = actions_from_plan(pc["plan"])
    held = confirm_items(actions)
    if not held:
        return None
    rest = [a for a in actions if a not in held]
    return confirm_view(held, rest, slides=getattr(item, "slides", None), note=request.get("note"))


def confirm_answered_text(
    result: ConfirmResult, view: ConfirmView | None, item_id: int, *, answered: bool | None = None,
) -> str | None:
    """What the confirm message is edited to once answered (buttons removed), or None to
    leave it. ``view`` is the question as it was asked (None when it can't be rebuilt)."""
    if result is ConfirmResult.ALREADY_ANSWERED and view is not None and answered is not None:
        # A second tap must not overwrite the first tap's (more specific) text with a vaguer one.
        result = ConfirmResult.YES if answered else ConfirmResult.NO_CONTINUE
    if result in CONFIRM_EDITS_STATIC:
        return CONFIRM_EDITS_STATIC[result]
    if view is None:
        return None
    if result is ConfirmResult.YES:
        return "Rebuilding the deck…" if view.rebuild else f"Removing {view.what}…"
    if result is ConfirmResult.NO_CONTINUE:
        return f"Kept {view.what}. Applying the rest…"
    if result is ConfirmResult.NO_NOTHING_LEFT:
        return f"Kept {view.what}. Item {item_id} is unchanged — the preview above still stands."
    return None


# --------------------------------------------------------------------------
# Ask the operator (spec §4)
# --------------------------------------------------------------------------


def _is_closing(slide: Any) -> bool:
    return isinstance(slide, Mapping) and slide.get("type") in CLOSING_TYPES


def _slide_list(slides: Sequence[Mapping[str, Any]]) -> list[str]:
    n = len(slides)
    first_closing = n
    while first_closing > 0 and _is_closing(slides[first_closing - 1]):
        first_closing -= 1
    lines = []
    for i in range(first_closing):
        s = slides[i]
        label = clean(s.get("headline") or s.get("type") or "", HEADLINE_QUOTE_MAX) if isinstance(s, Mapping) else ""
        lines.append(f"{i + 1} \"{label}\"")
    lines = lines[:SLIDE_LIST_MAX_LINES]
    if first_closing < n:
        span = str(first_closing + 1) if first_closing + 1 == n else f"{first_closing + 1}–{n}"
        lines.append(ASK_CLOSING_LINE.format(span=span))
    return lines


def _examples(cause: AskCause, slides: Sequence[Mapping[str, Any]], themes: Sequence[str]) -> list[str]:
    """Phrasings built from THIS deck so a pasted example works."""
    n = len(slides)
    body = [i + 1 for i in range(n) if not _is_closing(slides[i])]
    movable = [i for i in body if isinstance(slides[i - 1], Mapping) and slides[i - 1].get("type") != "hook"]
    first = (body[1] if len(body) > 1 else body[0]) if body else 1
    if cause is AskCause.WHICH_THEME:
        return [f"\"use the {clean(themes[0], 30)} theme\""] if themes else []
    if cause is AskCause.EXPLICIT_NEEDED:
        out = [f"\"remove slide {movable[-1]}\""] if movable else []
        if len(movable) > 1:
            out.append(f"\"swap slides {movable[0]} and {movable[1]}\"")
        return out or [f"\"slide {first} shorter\""]
    out = [f"\"slide {first} shorter\""]
    typed = ""
    for i in body:
        slide_type = clean(slides[i - 1].get("type"), 20) if isinstance(slides[i - 1], Mapping) else ""
        if i != first and slide_type and slide_type not in ("hook", "point"):
            typed = slide_type
            break
    if typed == "kpi":
        out.append("\"the kpi slide: check the number\"")
    elif typed:
        out.append(f"\"the {typed} slide is too wordy\"")
    else:
        out.append("\"make the caption more casual\"")
    return out


def render_ask_operator(
    item_id: int, dropped: Iterable[Any], slides: Sequence[Mapping[str, Any]] | None,
    themes: Sequence[str] = (), *, plan_failed: bool = False,
) -> str:
    """The "I couldn't tell what to change" message. Cause line from the drop reasons,
    the real slide list, examples that exist in this deck. NEVER the operator's note."""
    dropped = list(dropped)
    deck = [s for s in (slides or []) if isinstance(s, Mapping)]
    cause = ask_cause(dropped)
    themes_text = _themes_text(themes)
    ctx = _Ctx(Numbering(), deck, tuple(themes), len(deck) or None)
    specific: list[str] = []
    for d in _mappings([_as_row(x) for x in dropped]):
        reason = _reason_of(d)
        if reason in _ASK_SPECIFIC_REASONS:
            line = _row_line(ctx, REASON_COPY[reason], str(d.get("op")), _int(d.get("slide")))
            if line is not None and line.text not in specific:
                specific.append(line.text)
    cause_line = ASK_CAUSE_LINES[cause].format(themes_clause=f" Available: {themes_text}." if themes_text else "")
    if cause is AskCause.VAGUE and plan_failed:
        cause_line = ASK_PLAN_FAILED_LINE
    head = [ASK_NOTHING_TOUCHED if specific and cause is AskCause.VAGUE else cause_line, *specific]
    lines = [*head]
    if deck:
        lines.append(ASK_SLIDES_HEAD.format(id=item_id))
        lines += _slide_list(deck)
        examples = _examples(cause, deck, themes)
        if examples:
            lines.append("Try e.g. " + " or ".join(examples) + ".")
    lines.append(ASK_EXIT)
    return "\n".join(lines)


def _as_row(d: Any) -> Mapping[str, Any]:
    if isinstance(d, Mapping):
        return d
    return {"op": getattr(d, "op", ""), "slide": getattr(d, "slide", 0), "reason": getattr(d, "reason", "")}


# --------------------------------------------------------------------------
# One run, for a history list (the dashboard)
# --------------------------------------------------------------------------

RUN_ASKED_SUMMARY = "Asked what to change; nothing was touched."
RUN_EMPTY_SUMMARY = "Nothing to report."


def run_summary(run: Any, *, slides: Sequence[Mapping[str, Any]] | None = None, themes: Sequence[str] = ()) -> str:
    """A run as plain text for a history list: its Changes block, or one line saying why
    there is none. Pass ``slides`` only for the run that produced the deck on screen
    (older runs' headlines and numbering cannot be recovered)."""
    if isinstance(run, Mapping) and run.get("ask_operator"):
        return RUN_ASKED_SUMMARY
    return render_changes(run, slides=slides, themes=themes) or RUN_EMPTY_SUMMARY


# --------------------------------------------------------------------------
# Small renderers the surfaces share
# --------------------------------------------------------------------------


def toast(text: str) -> str:
    """A callback toast within the platform cap (a longer one is rejected by Telegram)."""
    return _cut(text, TOAST_MAX)
