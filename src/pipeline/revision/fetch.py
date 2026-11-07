"""Fetch executors for the revision package (T299): ``research``, ``fix_fact``,
``replace_image``. These are the revision steps that touch the open web, so the
rules here are about what the web may and may not do.

  * ``research`` is ``stages.research._research_one`` reused as-is: the same
    search, the same relevance gate, the same note writer. The note is stamped
    ``origin: "revision"``. Whatever provenance check lands in
    ``_research_one`` (D232/T265, not built at the time of writing) is inherited
    by calling it, not restated here.
  * ``fix_fact`` is research, then ``text_edit.edit_slide`` with the new notes as
    PRIORITY material and the operator's RAW note as ``note``. Figure backing
    therefore still comes only from non-model material (D276): an invented
    figure is rejected exactly as in a plain edit.
  * ``replace_image`` searches, then downloads through
    ``safe_download.download_public_image`` (SSRF, redirect, size, decode
    guards) into ``media/researched/<item id>/`` -- the directory retention
    already purges (D228) -- under a name made of random hex and the decoded
    format, never of anything remote. The image gate (T223) is not built, so an
    applied image says so: ``reason == REASON_IMAGE_UNCHECKED``. It is never
    silently treated as vetted.

Contracts shared with ``text_edit``: inputs are never mutated; a ``None`` result
slide means "keep the original"; infrastructure faults (``Retryforever``,
``ConfigFault``, ``EnginesBlocked``) propagate and are never an outcome (rule
17) -- an outage defers, it is never ``NOT_FOUND``; ``Outcome.reason`` is a closed
code, never exception text (rule 15); web text reaches a model only through
``_research_one`` (existing) and ``text_edit``'s data blocks (D260).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import re
import secrets
from dataclasses import replace
from pathlib import Path

from ..deck_shape import CLOSING_TYPES
from ..integrity import material_text
from ..retention import contained
from ..stages.compose import NOTE_ORIGIN_KEY, NOTE_ORIGIN_REVISION
from ..safe_download import Downloaded, Refusal, download_public_image
from ..search import image_search
from ..stages.research import FAIL_OPEN_KEY, _research_one
from .model import (
    HOOK_TYPE,
    IMAGE_KEY,
    IMAGE_MODE_KEY,
    MAX_QUERY_CHARS,
    Action,
    Op,
    Outcome,
    OutcomeCode,
    Reason,
)
from .text_edit import edit_slide, target_index

log = logging.getLogger(__name__)

#: Note key + value marking a note written by a revision (not the original run).
ORIGIN_KEY = NOTE_ORIGIN_KEY
ORIGIN_REVISION = NOTE_ORIGIN_REVISION
#: A note the research writer itself reports as not answering the question.
LOW_CONFIDENCE = "low"

#: ``Outcome.reason`` strings. Closed; T303 maps them to operator copy.
REASON_NO_RESULTS = "no_results"  # search found nothing, or every page was irrelevant
REASON_NOT_ANSWERED = "not_answered"  # the note writer said the pages do not answer it
REASON_SOURCE_UNVERIFIED = "source_unverified"  # relevance gate broke; note rests on one unvetted page
REASON_NO_CANDIDATES = "no_candidates"  # image search returned nothing
REASON_NO_USABLE_IMAGE = "no_usable_image"  # every candidate was refused (guards) or unfetchable
REASON_SAME_IMAGE = "same_image"  # the only usable candidate is the picture already there (D274)
REASON_IMAGE_UNCHECKED = "image_unchecked"  # applied, but the T223 image gate has not vetted it
REASON_MISSING_QUERY = Reason.MISSING_ARG.value
REASON_BAD_SLIDE = Reason.OUT_OF_RANGE.value
REASON_CLOSING = Reason.CLOSING_SLIDE.value

#: Editorial: image candidates tried per replace_image (the research stage's own 6).
IMAGE_CANDIDATES = 6
#: Editorial: characters of the item's text given to the relevance gate as context.
SUBJECT_CONTEXT_CHARS = 200
#: Editorial default image modes (compose.IMAGE_MODES) for a slide that had no image.
DEFAULT_IMAGE_MODE = "inset"
HOOK_IMAGE_MODE = "background"
#: Fixed instruction for fix_fact's edit step. Code-written: no model or web text.
FIX_FACT_INSTRUCTION = (
    "Correct the claim on this slide using the NEW RESEARCH in the material. "
    "Change only what the new research contradicts or updates; keep everything else word for word."
)
NEW_RESEARCH_HEADING = "NEW RESEARCH (priority):"
EXISTING_MATERIAL_HEADING = "EXISTING MATERIAL:"

_WHITESPACE = re.compile(r"\s+")


def _out(action: Action, code: OutcomeCode, reason: str = "") -> Outcome:
    return Outcome(op=action.op, code=code, slide=action.slide, ask=action.ask, reason=reason)


def _clip(text: str, limit: int) -> str:
    return _WHITESPACE.sub(" ", text or "").strip()[:limit]


# --------------------------------------------------------------------------
# research
# --------------------------------------------------------------------------


def _subject(item, query: str) -> str:
    """What the relevance gate judges pages against: the query plus the first words
    of the story, so a generic query cannot admit a page about a different thing
    (the mouse-cursor defence). Single line, capped."""
    context = _clip(item.raw_text or "", SUBJECT_CONTEXT_CHARS)
    return f"{query} (about: {context})" if context else query


async def research(llm, item, action: Action, settings, http) -> tuple[list[dict], Outcome]:
    """New research for ``action.query``. Returns ``(notes, Outcome)``.

    ``notes`` is empty unless the outcome is ``APPLIED``. ``NOT_FOUND`` covers an
    empty search, every page judged irrelevant, and a writer that said the pages
    do not answer the question. A note built from the relevance gate's fail-open
    path (one page the gate never cleared) is refused as ``UNAVAILABLE``: nothing
    downstream (``material_text``) counts such a note either. The caller threads
    the returned notes into later text edits in the same run."""
    query = _clip(action.query, MAX_QUERY_CHARS)
    if not query:
        return [], _out(action, OutcomeCode.DROPPED_INVALID, REASON_MISSING_QUERY)
    # Not caught: Retryforever / ConfigFault / EnginesBlocked / a model fault are
    # infrastructure, and an outage must defer the revision, not report "not found".
    note = await _research_one(0, query, _subject(item, query), item, llm, http, settings)
    if not note:
        return [], _out(action, OutcomeCode.NOT_FOUND, REASON_NO_RESULTS)
    if note.get(FAIL_OPEN_KEY):
        return [], _out(action, OutcomeCode.UNAVAILABLE, REASON_SOURCE_UNVERIFIED)
    if note.get("confidence") == LOW_CONFIDENCE or not str(note.get("claim", "")).strip():
        return [], _out(action, OutcomeCode.NOT_FOUND, REASON_NOT_ANSWERED)
    return [{**note, ORIGIN_KEY: ORIGIN_REVISION}], _out(action, OutcomeCode.APPLIED)


# --------------------------------------------------------------------------
# fix_fact
# --------------------------------------------------------------------------


def priority_material(notes: list[dict], item) -> str:
    """The new notes first (the edit prompt packs from the front), then the
    item's existing material."""
    lines = [NEW_RESEARCH_HEADING]
    for n in notes:
        lines.append(str(n.get("claim", "")).strip())
        if str(n.get("detail", "")).strip():
            lines.append(str(n["detail"]).strip())
    return "\n".join([*lines, "", EXISTING_MATERIAL_HEADING, material_text(item)])


async def fix_fact(
    llm, item, deck: list[dict], action: Action, note: str, settings, http,
) -> tuple[dict | None, list[dict], Outcome]:
    """Research the claim, then correct the slide from what was found.

    Returns ``(new_slide | None, new_notes, Outcome)``. ``new_notes`` are returned
    even when the edit step fails -- the research was paid for and later edits in
    the same run may use it. The outcome is the research's when research found
    nothing, otherwise the edit's (``APPLIED``, ``UNCHANGED`` = the slide already
    matched the new research, ``REJECTED_INTEGRITY``...), always under
    ``Op.FIX_FACT``. ``note`` is the operator's RAW note."""
    idx = target_index(deck, action)
    if idx is None:
        return None, [], _out(action, OutcomeCode.DROPPED_INVALID, REASON_BAD_SLIDE)
    if deck[idx].get("type") in CLOSING_TYPES:
        return None, [], _out(action, OutcomeCode.DROPPED_INVALID, REASON_CLOSING)
    query = _clip(action.query or str(deck[idx].get("headline", "")), MAX_QUERY_CHARS)
    notes, found = await research(
        llm, item, replace(action, op=Op.RESEARCH.value, slide=0, query=query), settings, http,
    )
    if not notes:
        return None, [], replace(found, op=action.op, slide=action.slide)
    edit = Action(
        op=Op.EDIT_SLIDE.value, ask=action.ask, slide=action.slide, instruction=FIX_FACT_INSTRUCTION,
    )
    slide, edited = await edit_slide(
        llm, item, deck, edit, priority_material(notes, item), note, settings,
    )
    return slide, notes, replace(edited, op=action.op)


# --------------------------------------------------------------------------
# replace_image
# --------------------------------------------------------------------------


def _current_digest(slide: dict, media_root: Path) -> str | None:
    """sha256 of the picture on the slide now, if it is a file inside media_dir
    (a path from anywhere else is never opened)."""
    raw = slide.get(IMAGE_KEY)
    path = contained(raw, media_root) if isinstance(raw, str) and raw else None
    if path is None or not path.is_file():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _save(target_dir: Path, image: Downloaded) -> Path:
    """Write the verified bytes under a name that is random hex + the decoded
    format's suffix. ``x`` mode: never overwrites."""
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"rev_{secrets.token_hex(8)}{image.suffix}"
    with open(path, "xb") as fh:
        fh.write(image.blob)
    return path


async def replace_image(
    item, deck: list[dict], action: Action, settings, http,
) -> tuple[dict | None, Outcome]:
    """Put a different picture, found by ``action.query``, on slide ``action.slide``.

    Returns ``(new_slide | None, Outcome)``; ``deck`` is not mutated. ``APPLIED``
    carries ``REASON_IMAGE_UNCHECKED`` until the T223 gate exists. ``NOT_FOUND``:
    no candidates, or none usable. ``UNCHANGED``: the only usable candidate is the
    picture already on the slide (byte-identical), D274. A search outage propagates
    (``Retryforever``); a dead or hostile IMAGE host is just an unusable candidate."""
    idx = target_index(deck, action)
    if idx is None:
        return None, _out(action, OutcomeCode.DROPPED_INVALID, REASON_BAD_SLIDE)
    slide = deck[idx]
    if slide.get("type") in CLOSING_TYPES:
        return None, _out(action, OutcomeCode.DROPPED_INVALID, REASON_CLOSING)
    query = _clip(action.query, MAX_QUERY_CHARS)
    if not query:
        return None, _out(action, OutcomeCode.DROPPED_INVALID, REASON_MISSING_QUERY)

    candidates = await image_search(http, settings.searxng_url, query)
    if not candidates:
        return None, _out(action, OutcomeCode.NOT_FOUND, REASON_NO_CANDIDATES)

    media_root = Path(settings.media_dir)
    current = await asyncio.to_thread(_current_digest, slide, media_root)
    same = False
    for candidate in candidates[:IMAGE_CANDIDATES]:
        got = await download_public_image(http, str(candidate.get("url", "")))
        if isinstance(got, Refusal):
            log.info("revision image candidate refused: %s", got.value)
            continue
        if hashlib.sha256(got.blob).hexdigest() == current:
            same = True
            continue
        target_dir = media_root / "researched" / str(int(item.id))
        path = await asyncio.to_thread(_save, target_dir, got)
        new = copy.deepcopy(slide)
        new[IMAGE_KEY] = str(path)
        if not new.get(IMAGE_MODE_KEY):
            new[IMAGE_MODE_KEY] = HOOK_IMAGE_MODE if new.get("type") == HOOK_TYPE else DEFAULT_IMAGE_MODE
        log.info("revision image for item %s saved (%dx%d %s)", item.id, got.width, got.height, got.fmt)
        return new, _out(action, OutcomeCode.APPLIED, REASON_IMAGE_UNCHECKED)
    if same:
        return None, _out(action, OutcomeCode.UNCHANGED, REASON_SAME_IMAGE)
    return None, _out(action, OutcomeCode.NOT_FOUND, REASON_NO_USABLE_IMAGE)

