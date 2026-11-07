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

    ``retry_after_s`` lets a raiser pick its own delay (e.g. a provider
    throttle that needs an hour, not a minute); ``None`` means the worker's
    default INFRA_BACKOFF_S.
    """

    def __init__(self, *args: object, retry_after_s: int | None = None) -> None:
        super().__init__(*args)
        self.retry_after_s = retry_after_s


class PlatformThrottled(Retryforever):
    """Instagram is throttling or blocking the account (T287).

    A Retryforever (no attempt spent, long ``retry_after_s``) that the publish
    path can tell apart from pacing defers, to alert the operator once.
    """


class ConfigFault(Retryforever):
    """A misconfiguration or account fault: this item is fine, the setup is not.

    A model name Ollama does not have, an OpenRouter slug that no longer has
    endpoints. It fails identically for every item until the operator edits
    config, so it must never spend an item's attempts (T259, D235). A subclass
    of Retryforever so the worker defers rather than fails, and so anything
    that handles Retryforever still handles it; the LLM client additionally
    tries the configured spare model and alerts the operator once.
    """


class EnginesBlocked(Retryforever):
    """A search query came back empty AND an engine reported it is suspended or
    blocked (T260). Distinct from a dead SearXNG because an empty result is
    ambiguous here: another researcher may have found plenty. Fan-out callers
    count it as an empty query and defer only when nothing at all came back;
    single-query callers (enumeration) defer on it directly.
    """


class RateLimited(Retryforever):
    """The provider is throttling us.

    A subclass of Retryforever so anything that does not handle it explicitly
    still defers the item rather than failing it. The LLM client catches it to
    retry and then fall back to a local model.
    """


class BadCompletion(Retryable):
    """The provider answered, but the answer is unusable.

    Empty content, no choices, or prose where a schema was required. These are
    the model misbehaving rather than the request being wrong, so they are
    worth retrying and worth falling back to a different model — unlike a 4xx,
    which will fail identically forever.

    A type rather than a message match: the first version tested for the string
    "unparseable JSON" and an empty completion, arriving a day later, walked
    straight past it.
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
