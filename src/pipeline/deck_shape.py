"""Deck-shape constants shared by compose and the revision package.

A leaf module (imports nothing from the pipeline) so that ``revision`` can know
what a deck is allowed to look like without importing the whole composer.
``compose`` re-exports every name here, so ``compose.MAX_SLIDES`` and friends
keep working unchanged.
"""

from __future__ import annotations

MIN_SLIDES = 3
MAX_SLIDES = 10  # Instagram carousel hard maximum, and Telegram album maximum.

SLIDE_TYPES = [
    "hook", "point", "facts", "kpi", "chart", "code", "flow", "compare",
    "quote", "photo", "repo", "links", "takeaway", "sources", "follow",
]

#: The tail of every deck, in the order they must appear. A deck ending on a
#: body slide has no attribution, so one of these is guaranteed, not requested.
CLOSING_TYPES = ("links", "sources", "follow")
