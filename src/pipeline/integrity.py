"""Checks a deck has to pass before anything renders it.

The compose prompt asks for grounded slides, and mostly gets them. This is the
part that does not depend on asking.

The failure that motivated it: item 116 requested "the psychology of
narcissistic people and how they affect kids in their childhood which turns
into illness as adults". Research returned eight general "what is NPD"
articles and nothing about developmental outcomes, which the coverage gate
correctly caught. /post then built a deck anyway, compose found a hole it had
not been told about, and filled it with a thesis — "What the research doesn't
show", "The gap is the finding", and a caption asserting "that research simply
hasn't been done yet".

That claim is false, and more importantly it is not a claim the pipeline is in
any position to make: it searched a handful of pages and found nothing, which
says something about the search and nothing about the literature. Presenting
your own failure to find something as evidence that it does not exist is the
one error here that actively misinforms a reader, so it is the one checked in
code rather than requested in a prompt.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

#: Ways a deck can claim something is not there. Deliberately about absence of
#: EVIDENCE — "experts disagree" or "the study found no effect" are findings a
#: source can support, and are not matched here.
ABSENCE_CLAIMS = (
    re.compile(r"\b(research|stud(?:y|ies)|data|evidence|literature)\b[^.]{0,40}"
               r"\b(doesn'?t|does not|hasn'?t|has not|never|no longer)\b"
               r"[^.]{0,20}\b(exist|been done|show|been conducted|been published)\b",
               re.IGNORECASE),
    re.compile(r"\bno\s+(longitudinal\s+)?(research|studies|data|evidence)\b"
               r"[^.]{0,30}\b(exist|available|found|published)\b", re.IGNORECASE),
    re.compile(r"\b(this|that|the)\s+research\s+(simply\s+)?"
               r"(hasn'?t|has not|doesn'?t|does not)\b", re.IGNORECASE),
    re.compile(r"\bthe\s+(gap|absence|silence)\s+is\s+the\s+(finding|story|point)\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+the\s+(research|data|evidence)\s+doesn'?t\s+show\b", re.IGNORECASE),
)


def absence_claims(text: str) -> list[str]:
    """The absence-of-evidence assertions in ``text``, if any."""
    found: list[str] = []
    for pattern in ABSENCE_CLAIMS:
        found.extend(match.group(0).strip() for match in pattern.finditer(text or ""))
    return found


def unsupported_absence(deck_text: str, notes: list[dict]) -> list[str]:
    """Absence claims the research does not actually support.

    A source may genuinely report that something has not been studied, and a
    deck is entitled to repeat that. What it may not do is reach the
    conclusion itself because its own search came back thin — so the claim is
    allowed only when the same kind of statement appears in a note.
    """
    claimed = absence_claims(deck_text)
    if not claimed:
        return []
    supported = absence_claims(" ".join(
        f"{n.get('claim', '')} {n.get('detail', '')}"
        for n in notes if isinstance(n, dict)
    ))
    return [] if supported else claimed


#: A slide whose content is mostly about what could not be found. One such
#: line is honest; a deck built out of them is not worth a reader's swipe.
GAP_PHRASES = re.compile(
    r"\b(the\s+)?(provided\s+)?sources?\s+(do|does)\s+not\b"
    r"|\bno\s+(provided\s+)?sources?\b"
    r"|\bnot\s+(covered|addressed|available)\s+(here|in\s+(these|the)\s+sources?)\b"
    r"|\bthese\s+sources?\s+(do|does)\s+not\b",
    re.IGNORECASE,
)

#: At most this many slides may be about absent material.
MAX_GAP_SLIDES = 1


def gap_slides(slides: list[dict]) -> list[int]:
    """1-based positions of slides that are mostly about what is missing."""
    found = []
    for index, slide in enumerate(slides, start=1):
        body = " ".join([
            str(slide.get("headline", "")), str(slide.get("sub", "")),
            " ".join(map(str, slide.get("bullets", []) or [])),
        ])
        if GAP_PHRASES.search(body):
            found.append(index)
    return found


def check_deck(slides: list[dict], notes: list[dict], caption: str = "") -> list[str]:
    """Every integrity problem in this deck, as human-readable strings.

    Empty means it may render.
    """
    problems: list[str] = []

    text = " ".join(
        " ".join([
            str(s.get("headline", "")), str(s.get("sub", "")),
            str(s.get("quote", "")), " ".join(map(str, s.get("bullets", []) or [])),
        ]) for s in slides
    ) + " " + (caption or "")

    for claim in unsupported_absence(text, notes):
        problems.append(
            f"claims absence of evidence that no source supports: {claim!r}"
        )

    if not slides:
        problems.append("deck has no slides")

    # Naming a gap once is honest. Item 116's rebuild spent two of seven
    # slides on it — one headed "Childhood exposure effects" whose bullets
    # were a single sentence about the sources not covering childhood
    # exposure — which is a deck about its own shortfall.
    gaps_at = gap_slides(slides)
    if len(gaps_at) > MAX_GAP_SLIDES:
        problems.append(
            f"{len(gaps_at)} slides are about missing material (positions "
            f"{gaps_at}); at most {MAX_GAP_SLIDES} may be"
        )

    return problems
