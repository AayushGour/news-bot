"""Failure classification.

Treating all failures alike is how retry budget gets burned on errors that can
never succeed. Every stage raises one of these so the worker knows what to do.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base for everything this package raises deliberately."""


class Survivable(PipelineError):
    """Partial failure the stage absorbs; the item continues with less data.

    Example: one of five researchers dies, or one URL 404s. Raising this is
    unusual — stages normally just record the problem and carry on.
    """


class Retryable(PipelineError):
    """Transient failure. Back off and try again, up to ``max_attempts``."""


class Retryforever(Retryable):
    """Infrastructure is down, and it is not this item's fault.

    Ollama being unreachable should not consume an item's retry budget — the
    item is fine, the service is not. Backs off without incrementing attempts.
    """


class Terminal(PipelineError):
    """Will never succeed without human intervention. Do not retry.

    Example: an Instagram 4xx, an expired token, a rate limit. Retrying these
    three times only delays the alert the operator actually needs.
    """


class Recompose(PipelineError):
    """Rendered output did not fit; send the item back for new slide copy.

    Carries which slide failed so the recompose prompt can be specific.
    """

    def __init__(self, slide_index: int, reason: str) -> None:
        super().__init__(f"slide {slide_index}: {reason}")
        self.slide_index = slide_index
        self.reason = reason
