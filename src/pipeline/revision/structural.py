"""Structural revision ops (T297, D250/D256/D267): deterministic, no LLM.

``remove_slide``, ``reorder``, ``set_theme``, ``remove_image`` and the
placement half of ``add_slide`` (``insert_slide``; the model writes the slide,
this module decides where it goes and whether the deck may take it).

Every function is pure: it works on a copy and returns a ``Step``; the input
deck / item is never touched. Every refusal is a closed ``OutcomeCode``, never a
silent clamp (design section 5).

Stable ids: ``Deck`` carries each slide's id, the 0-based index it had in the
deck the plan was made against (``Action.slide_id``). Removing slide 2 leaves
the id of slide 5 alone, so an edit planned for slide 5 still finds it. Slides
the invariant pass adds get fresh ids above every id ever issued.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from ..deck_shape import CLOSING_TYPES, MAX_SLIDES, MIN_SLIDES
from ..models import Item
from ..stages.compose import REQUIRED_BODY_FIELDS, drop_duplicate_ctas, ensure_closing_slide
from ..stages.render import TEXT_ONLY_THEMES, available_themes
from .model import (
    HOOK_TYPE,
    IMAGE_KEY,
    IMAGE_MODE_KEY,
    PHOTO_TYPE,
    Action,
    Op,
    Outcome,
    OutcomeCode,
    Reason,
    normalise_order,
)

log = logging.getLogger(__name__)

#: ``Outcome.reason`` strings. Closed; T303 maps them to operator copy.
WARN_TEXT_ONLY_THEME = "text_only_theme_with_photo"  # applied, but photo slides lose their picture
REASON_MIN_SLIDES = "min_slides"
REASON_MAX_SLIDES = "max_slides"
REASON_HOOK = "hook_slide"
REASON_BODYLESS = "bodyless"
REASON_CTA_DROPPED = "cta_dropped"  # the CTA pass would have deleted a slide nobody asked to delete
REASON_CLOSING_CLAMP = "closing_clamp"  # adding a closing slide would have trimmed the body
REASON_ALREADY_REMOVED = "already_removed"
REASON_NO_IMAGE = Reason.NO_IMAGE.value
REASON_CLOSING = Reason.CLOSING_SLIDE.value
REASON_TARGET_REMOVED = Reason.TARGET_REMOVED.value
REASON_BAD_THEME = Reason.BAD_THEME.value
REASON_NOOP = Reason.NOOP_REORDER.value


@dataclass(frozen=True)
class Deck:
    """Slides plus their stable ids. Treat the slide dicts as read-only."""

    slides: tuple[dict, ...]
    ids: tuple[int, ...]
    #: First id never yet issued; new slides take ``next_id`` upward.
    next_id: int

    @classmethod
    def of(cls, slides: Sequence[dict]) -> Deck:
        """Ids are positions in ``slides``. Deep-copies, so later edits to the caller's list can't leak in."""
        return cls(tuple(copy.deepcopy(list(slides))), tuple(range(len(slides))), len(slides))

    def __len__(self) -> int:
        return len(self.slides)

    def position(self, slide_id: int | None) -> int | None:
        return self.ids.index(slide_id) if slide_id in self.ids else None

    def by_id(self) -> dict[int, dict]:
        """``{stable id: slide}`` -- the shape ``check.untouched_invariant`` takes as ``after``."""
        return dict(zip(self.ids, self.slides, strict=True))

    def as_list(self) -> list[dict]:
        """A fresh list of copies for handing to compose-style code."""
        return copy.deepcopy(list(self.slides))


@dataclass(frozen=True)
class Step:
    """Result of one op. ``deck`` is the input Deck object when nothing changed."""

    outcome: Outcome
    #: None only from ``set_theme`` (it never touches slides); every deck op always sets it.
    deck: Deck | None
    item: Item | None = None


def _out(action: Action, code: OutcomeCode, reason: str = "") -> Outcome:
    return Outcome(op=action.op, code=code, slide=action.slide, ask=action.ask, reason=reason)


def _is_closing(slide: dict) -> bool:
    return slide.get("type") in CLOSING_TYPES


def _finalize(
    pairs: list[tuple[int, dict]], next_id: int, *, is_dm: bool, source_urls: Sequence[str],
) -> tuple[Deck, set[int]]:
    """Re-run compose's closing guarantees on (id, slide) pairs, keeping ids.

    The compose functions return the SAME dict objects they were given, so
    identity maps survivors back to their ids; a dict they created is new and
    gets a fresh id. Reused, not reimplemented (house rule 3). Also returns the
    ids ``drop_duplicate_ctas`` removed, so a refusal can name its cause.
    """
    by_obj = {id(s): sid for sid, s in pairs}
    kept = drop_duplicate_ctas([s for _, s in pairs])
    cta_dropped = {sid for sid, s in pairs if not any(s is k for k in kept)}
    slides = ensure_closing_slide(kept, is_dm, list(source_urls))
    ids: list[int] = []
    for s in slides:
        sid = by_obj.get(id(s))
        if sid is None:
            sid, next_id = next_id, next_id + 1
        ids.append(sid)
    return Deck(tuple(slides), tuple(ids), next_id), cta_dropped


def _commit(
    deck: Deck, action: Action, pairs: list[tuple[int, dict]], *, is_dm: bool, source_urls: Sequence[str],
    issued: int = 0,
) -> Step:
    """Finalize, then refuse (returning the ORIGINAL deck) if an invariant broke.

    `pairs` is exactly what the op meant to keep, so every id in it must come
    out the other side: the closing pass may add slides but never delete or
    trim one the operator did not ask to lose (no silent clamp, design s.5).
    """
    new, cta_dropped = _finalize(pairs, deck.next_id + issued, is_dm=is_dm, source_urls=source_urls)
    lost = {sid for sid, _ in pairs} - set(new.ids)
    if lost:
        reason = REASON_CTA_DROPPED if lost & cta_dropped else REASON_CLOSING_CLAMP
        return Step(_out(action, OutcomeCode.REFUSED_INVARIANT, reason), deck)
    if len(new) < MIN_SLIDES:
        return Step(_out(action, OutcomeCode.REFUSED_INVARIANT, REASON_MIN_SLIDES), deck)
    if len(new) > MAX_SLIDES:
        return Step(_out(action, OutcomeCode.REFUSED_INVARIANT, REASON_MAX_SLIDES), deck)
    return Step(_out(action, OutcomeCode.APPLIED), new)


def _pairs(deck: Deck) -> list[tuple[int, dict]]:
    return list(zip(deck.ids, copy.deepcopy(list(deck.slides)), strict=True))


def _missing(deck: Deck, action: Action) -> Step | None:
    """NOT_FOUND / UNCHANGED for a target that is not in the deck, else None."""
    sid = action.slide_id
    if sid is None or sid < 0 or sid >= deck.next_id:
        return Step(_out(action, OutcomeCode.NOT_FOUND), deck)
    if sid not in deck.ids:
        return Step(_out(action, OutcomeCode.UNCHANGED, REASON_ALREADY_REMOVED), deck)
    return None


def remove_slide(deck: Deck, action: Action, *, is_dm: bool, source_urls: Sequence[str]) -> Step:
    """Delete the slide with ``action.slide_id``. Refused for the hook, a
    closing slide, or if the deck would fall below ``MIN_SLIDES`` (``_commit``)."""
    if (gone := _missing(deck, action)) is not None:
        return gone
    pos = deck.position(action.slide_id)
    slide = deck.slides[pos]
    if _is_closing(slide):
        return Step(_out(action, OutcomeCode.REFUSED_INVARIANT, REASON_CLOSING), deck)
    if pos == 0 and slide.get("type") == HOOK_TYPE:
        return Step(_out(action, OutcomeCode.REFUSED_INVARIANT, REASON_HOOK), deck)
    pairs = [p for i, p in enumerate(_pairs(deck)) if i != pos]
    return _commit(deck, action, pairs, is_dm=is_dm, source_urls=source_urls)


def reorder(deck: Deck, action: Action, *, is_dm: bool, source_urls: Sequence[str]) -> Step:
    """Apply ``action.order`` (1-based plan numbers = stable id + 1), skipping
    removed slides. Slides the order omits keep their relative place after
    those it lists. The hook stays first and closing slides stay last."""
    seen: dict[int, None] = {}
    for n in action.order:
        pos = deck.position(n - 1)
        if pos is not None:
            seen.setdefault(pos + 1)  # current 1-based position
    rest = [p for p in range(1, len(deck) + 1) if p not in seen]
    final = normalise_order(list(seen) + rest, list(deck.slides))
    if final == list(range(1, len(deck) + 1)):
        return Step(_out(action, OutcomeCode.UNCHANGED, REASON_NOOP), deck)
    pairs = _pairs(deck)
    return _commit(deck, action, [pairs[p - 1] for p in final], is_dm=is_dm, source_urls=source_urls)


def remove_image(deck: Deck, action: Action, *, is_dm: bool, source_urls: Sequence[str]) -> Step:
    """Drop the image (and its mode) from one slide; unchanged if it has none.
    Refused when the image is all the slide has (e.g. a ``photo`` slide)."""
    if (gone := _missing(deck, action)) is not None:
        if gone.outcome.code is OutcomeCode.UNCHANGED:  # removed earlier in this plan
            return Step(_out(action, OutcomeCode.NOT_FOUND, REASON_TARGET_REMOVED), deck)
        return gone
    pos = deck.position(action.slide_id)
    if not deck.slides[pos].get(IMAGE_KEY):
        return Step(_out(action, OutcomeCode.UNCHANGED, REASON_NO_IMAGE), deck)
    pairs = _pairs(deck)
    slide = pairs[pos][1]
    slide.pop(IMAGE_KEY, None)
    slide.pop(IMAGE_MODE_KEY, None)
    if not any(slide.get(f) for f in REQUIRED_BODY_FIELDS.get(slide.get("type"), ())):
        return Step(_out(action, OutcomeCode.REFUSED_INVARIANT, REASON_BODYLESS), deck)
    return _commit(deck, action, pairs, is_dm=is_dm, source_urls=source_urls)


def insert_slide(
    deck: Deck, action: Action, new_slide: dict, *, is_dm: bool, source_urls: Sequence[str],
) -> Step:
    """Place a model-written ``new_slide`` AFTER ``action.slide_id`` (add_slide).

    Refused at ``MAX_SLIDES``. A target that was removed in this plan is
    replaced by the nearest earlier surviving slide; a closing slide as target
    means 'at the end of the body' (the closing pass re-partitions body | tail). The new slide's id is ``Step.deck.ids`` entry
    not present in the input deck.
    """
    if len(deck) >= MAX_SLIDES:
        return Step(_out(action, OutcomeCode.REFUSED_INVARIANT, REASON_MAX_SLIDES), deck)
    if new_slide.get("type") in CLOSING_TYPES:
        return Step(_out(action, OutcomeCode.REFUSED_INVARIANT, REASON_CLOSING), deck)
    sid = action.slide_id
    if sid is None or sid < 0 or sid >= deck.next_id:
        return Step(_out(action, OutcomeCode.NOT_FOUND), deck)
    pos = deck.position(sid)
    if pos is not None:
        at = pos + 1
    else:  # target removed in this plan: after the nearest earlier surviving id
        earlier = [i for i, i_id in enumerate(deck.ids) if i_id < sid]
        at = (earlier[-1] + 1) if earlier else 1
    # a position inside/after the tail needs no clamp: ensure_closing_slide partitions body | closing
    pairs = _pairs(deck)
    pairs.insert(at, (deck.next_id, copy.deepcopy(new_slide)))
    return _commit(deck, action, pairs, is_dm=is_dm, source_urls=source_urls, issued=1)


def set_theme(
    item: Item, action: Action, *, slides: Sequence[dict] | None = None, themes: Sequence[str] | None = None,
) -> Step:
    """Pin ``action.theme`` on a COPY of ``item`` (``theme_pin``, D256).

    ``themes`` defaults to ``render.available_themes()``. A text-only theme on a
    deck with a photo slide is honoured, with ``WARN_TEXT_ONLY_THEME`` as the
    outcome reason. The returned ``Step.item`` is the copy; persisting it is the
    stage's job. ``Step.deck`` is None (this op never touches slides).
    """
    names = list(available_themes() if themes is None else themes)
    wanted = (action.theme or "").strip().lower()
    chosen = next((n for n in names if n.lower() == wanted), None) if wanted else None
    if chosen is None:
        return Step(_out(action, OutcomeCode.DROPPED_INVALID, REASON_BAD_THEME), None, item)
    if item.theme_pin == chosen:
        return Step(_out(action, OutcomeCode.UNCHANGED), None, item)
    deck_slides = item.slides if slides is None else slides
    warn = chosen in TEXT_ONLY_THEMES and any(s.get("type") == PHOTO_TYPE for s in deck_slides or ())
    return Step(
        _out(action, OutcomeCode.APPLIED, WARN_TEXT_ONLY_THEME if warn else ""),
        None,
        replace(item, theme_pin=chosen),
    )


#: Op -> executor for the deck-shaped structural ops, so T301 dispatches without an if-ladder.
DECK_OPS: dict[str, Callable[..., Step]] = {
    Op.REMOVE_SLIDE.value: remove_slide,
    Op.REORDER.value: reorder,
    Op.REMOVE_IMAGE.value: remove_image,
}
