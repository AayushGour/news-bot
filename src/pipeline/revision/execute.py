"""Run one round of validated actions against a deck (T301).

Pure orchestration of the executors that already exist (``text_edit``, ``fetch``,
``structural``): this module decides ORDER, threads state between them and records
one ``Outcome`` per action. It never calls a model or the web itself.

Order (``model.execution_order``): fetch -> image -> text -> structural, a reorder
last. Two refinements, both forced by stable ids (``Action.slide_id`` = index in
the deck the plan was made against):

* the text/image executors index their ``deck`` argument BY STABLE ID, so every
  content change (an edit, a new picture) is written back by id, and a removal,
  an insertion or a reorder is applied only AFTER the last content call;
* ``add_slide`` is a text op (a model call) but its placement is structural, so the
  new slide waits in ``_inserts`` and is placed after the removals AND the reorder:
  it is anchored to the slide it follows by stable id, so it lands right after that
  slide wherever the reorder moved it (``structural.insert_slide`` also handles an
  anchor removed in the same plan).

New research notes found by ``research`` / ``fix_fact`` reach LATER text edits as
priority material (``fetch.priority_material``) and the integrity gates see them
as evidence. The operator's RAW note is the ``note=`` of every text executor
(D276); ``action.instruction`` is model-written and never backs a figure.

Faults: ``Retryforever`` (ConfigFault, EnginesBlocked, a timeout) propagates - the
revision is deferred (rule 17). A ``Retryable`` that is not infrastructure (the
model returned an unusable completion twice) costs that ONE action, not the item.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ..errors import Retryable, Retryforever
from ..integrity import material_text
from ..models import Item
from ..stages.compose import research_source_urls
from . import fetch, structural
from .model import Action, Op, Outcome, OutcomeCode, execution_order
from .structural import Deck
from .text_edit import add_slide, edit_slide, rewrite_caption

log = logging.getLogger(__name__)

#: Actions that need the open web. Skipped (not run) when search has been blocked
#: for longer than ``revise_engines_blocked_max_s`` (see ``stages/revise``).
SEARCH_OPS = frozenset({Op.RESEARCH.value, Op.FIX_FACT.value, Op.REPLACE_IMAGE.value})

#: ``Outcome.reason`` codes this module adds (closed; T302/T303 map them to copy).
REASON_SEARCH_BLOCKED = "search_blocked"  # search engines were blocked past the cap; not attempted
REASON_MODEL_UNUSABLE = "model_unusable"  # the model's reply was unusable twice; kept as is
REASON_GLOBAL_OP = "global_op"  # full_recompose never runs inside a round


@dataclass
class RoundResult:
    deck: Deck
    caption: str
    theme_pin: str | None
    new_notes: list[dict] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    #: Local stable ids the plan targeted (an add_slide's anchor is NOT one: it must stay identical).
    targeted: set[int] = field(default_factory=set)


class _Round:
    def __init__(
        self, *, item: Item, deck: Deck, caption: str, theme_pin: str | None, note: str,
        llm: Any, http: Any, settings: Any, themes: Sequence[str], skip_search: bool,
    ) -> None:
        self.item, self.llm, self.http, self.settings = item, llm, http, settings
        self.deck, self.caption, self.theme_pin = deck, caption, theme_pin
        self.note, self.themes, self.skip_search = note, list(themes), skip_search
        self.new_notes: list[dict] = []
        self.outcomes: list[Outcome] = []
        self.targeted: set[int] = set()
        self._inserts: list[tuple[Action, dict]] = []
        self.is_dm = item.source == "dm"

    # ----------------------------------------------------------- shared state

    def view(self) -> list[dict]:
        """The deck as a list INDEXED BY STABLE ID (what the executors expect).
        Valid because no removal/insertion/reorder runs before the last content call."""
        by_id = self.deck.by_id()
        return [by_id.get(i, {}) for i in range(self.deck.next_id)]

    def put(self, slide_id: int, slide: dict) -> None:
        pos = self.deck.position(slide_id)
        if pos is None:
            return
        slides = list(self.deck.slides)
        slides[pos] = slide
        self.deck = replace(self.deck, slides=tuple(slides))

    def ctx(self) -> Item:
        """The item as the executors should see it NOW: research incl. this run's
        new notes, the working caption/slides/pin."""
        return replace(
            self.item, research=[*(self.item.research or []), *self.new_notes],
            caption=self.caption, slides=self.deck.as_list(), theme_pin=self.theme_pin,
        )

    def material(self) -> str:
        if self.new_notes:
            return fetch.priority_material(self.new_notes, self.item)
        return material_text(self.item)

    def source_urls(self) -> list[str]:
        return research_source_urls([*(self.item.research or []), *self.new_notes])

    def done(self, outcome: Outcome) -> None:
        self.outcomes.append(outcome)

    # --------------------------------------------------------------- handlers

    async def _research(self, a: Action) -> None:
        notes, out = await fetch.research(self.llm, self.ctx(), a, self.settings, self.http)
        self.new_notes += notes
        self.done(out)

    async def _fix_fact(self, a: Action) -> None:
        slide, notes, out = await fetch.fix_fact(
            self.llm, self.ctx(), self.view(), a, self.note, self.settings, self.http
        )
        self.new_notes += notes
        if slide is not None:
            self.put(a.slide_id, slide)
        self.done(out)

    async def _replace_image(self, a: Action) -> None:
        slide, out = await fetch.replace_image(self.ctx(), self.view(), a, self.settings, self.http)
        if slide is not None:
            self.put(a.slide_id, slide)
        self.done(out)

    async def _remove_image(self, a: Action) -> None:
        step = structural.remove_image(
            self.deck, a, is_dm=self.is_dm, source_urls=self.source_urls()
        )
        self.deck = step.deck
        self.done(step.outcome)

    async def _edit_slide(self, a: Action) -> None:
        slide, out = await edit_slide(
            self.llm, self.ctx(), self.view(), a, self.material(), self.note, self.settings
        )
        if slide is not None:
            self.put(a.slide_id, slide)
        self.done(out)

    async def _add_slide(self, a: Action) -> None:
        slide, out = await add_slide(
            self.llm, self.ctx(), self.view(), a, self.material(), self.note, self.settings
        )
        if slide is None:
            self.done(out)
        else:
            self._inserts.append((a, slide))  # its outcome is the insertion's (below)

    async def _rewrite_caption(self, a: Action) -> None:
        credit = "" if self.is_dm else getattr(self.settings, "source_credit", "")
        caption, out = await rewrite_caption(
            self.llm, self.ctx(), a, self.material(), credit, self.note, self.settings
        )
        if caption is not None:
            self.caption = caption
        self.done(out)

    async def _remove_slide(self, a: Action) -> None:
        step = structural.remove_slide(
            self.deck, a, is_dm=self.is_dm, source_urls=self.source_urls()
        )
        self.deck = step.deck
        self.done(step.outcome)

    async def _reorder(self, a: Action) -> None:
        step = structural.reorder(self.deck, a, is_dm=self.is_dm, source_urls=self.source_urls())
        self.deck = step.deck
        self.done(step.outcome)

    async def _set_theme(self, a: Action) -> None:
        step = structural.set_theme(
            self.ctx(), a, slides=self.deck.slides, themes=self.themes or None
        )
        if step.outcome.code is OutcomeCode.APPLIED and step.item is not None:
            self.theme_pin = step.item.theme_pin
        self.done(step.outcome)

    async def _full_recompose(self, a: Action) -> None:
        # A whole-deck rebuild is the stage's fallback path, never a round (D253).
        self.done(Outcome(a.op, OutcomeCode.DROPPED_INVALID, a.slide, a.ask, REASON_GLOBAL_OP))

    async def flush_inserts(self) -> None:
        """Place the waiting new slides (after the removals and the reorder)."""
        pending, self._inserts = self._inserts, []
        for a, slide in pending:
            step = structural.insert_slide(
                self.deck, a, slide, is_dm=self.is_dm, source_urls=self.source_urls()
            )
            self.deck = step.deck
            self.done(step.outcome)

    # ----------------------------------------------------------------- driver

    async def run(self, actions: Sequence[Action]) -> RoundResult:
        for a in execution_order(actions):
            if a.slide_id is not None and a.op != Op.ADD_SLIDE.value:
                self.targeted.add(a.slide_id)
            if self.skip_search and a.op in SEARCH_OPS:
                log.info("revision: %s skipped, search has been blocked past the cap", a.op)
                self.done(Outcome(a.op, OutcomeCode.NOT_FOUND, a.slide, a.ask, REASON_SEARCH_BLOCKED))
                continue
            try:
                # A handler records its outcome only after its last await, so an
                # exception here has left nothing half-recorded.
                await _HANDLERS[a.op](self, a)
            except Retryforever:
                raise
            except Retryable as exc:
                # Type only: exception text can echo prompt content (rule 15).
                log.warning("revision: %s on slide %s failed (%s); kept as is", a.op, a.slide, type(exc).__name__)
                self.done(Outcome(a.op, OutcomeCode.UNCHANGED, a.slide, a.ask, REASON_MODEL_UNUSABLE))
        await self.flush_inserts()
        return RoundResult(
            self.deck, self.caption, self.theme_pin, self.new_notes, self.outcomes, self.targeted
        )


#: op -> handler. Complete by construction: asserted against ACTION_SPECS below, so a
#: new op cannot be added to the registry without an executor here (rule 3).
_HANDLERS: dict[str, Callable[[_Round, Action], Awaitable[None]]] = {
    Op.RESEARCH.value: _Round._research,
    Op.FIX_FACT.value: _Round._fix_fact,
    Op.REPLACE_IMAGE.value: _Round._replace_image,
    Op.REMOVE_IMAGE.value: _Round._remove_image,
    Op.EDIT_SLIDE.value: _Round._edit_slide,
    Op.ADD_SLIDE.value: _Round._add_slide,
    Op.REWRITE_CAPTION.value: _Round._rewrite_caption,
    Op.REMOVE_SLIDE.value: _Round._remove_slide,
    Op.REORDER.value: _Round._reorder,
    Op.SET_THEME.value: _Round._set_theme,
    Op.FULL_RECOMPOSE.value: _Round._full_recompose,
}


def _assert_registry_complete() -> None:
    from .model import ACTION_SPECS

    if set(_HANDLERS) != set(ACTION_SPECS):
        raise RuntimeError(
            f"revision executors out of step with ACTION_SPECS: {sorted(set(_HANDLERS) ^ set(ACTION_SPECS))}"
        )


_assert_registry_complete()


async def execute_round(
    actions: Sequence[Action], *, item: Item, deck: Deck, caption: str, theme_pin: str | None,
    note: str, llm: Any, http: Any, settings: Any, themes: Sequence[str],
    skip_search: bool = False,
) -> RoundResult:
    """Run ``actions`` (already validated, grounded and confirmed) against ``deck``.

    ``item`` supplies the research, raw text and source; its ``slides``/``caption``/
    ``theme_pin`` are NOT read (the working copies are the arguments). Nothing the
    caller passed is mutated."""
    return await _Round(
        item=item, deck=deck, caption=caption, theme_pin=theme_pin, note=note, llm=llm,
        http=http, settings=settings, themes=themes, skip_search=skip_search,
    ).run(actions)
