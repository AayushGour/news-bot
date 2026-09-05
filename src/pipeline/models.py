"""Domain types and the pipeline state machine vocabulary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Status(StrEnum):
    INGESTED = "ingested"
    TRIAGED = "triaged"
    EXTRACTED = "extracted"
    RESEARCHED = "researched"
    SYNTHESIZED = "synthesized"
    COMPOSED = "composed"
    RENDERED = "rendered"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    # terminals
    DROPPED = "dropped"
    REJECTED = "rejected"
    FAILED = "failed"


#: Statuses from which nothing further happens, ever.
TERMINAL: frozenset[Status] = frozenset(
    {Status.PUBLISHED, Status.DROPPED, Status.REJECTED, Status.FAILED}
)

#: Statuses the worker must never advance.
#:
#: ``AWAITING_APPROVAL`` is terminal *for the worker only* — the approval bot is
#: the sole thing that moves an item out of it. Keeping it here rather than
#: relying on a check inside the worker means the human gate is enforced by the
#: state machine itself and cannot be lost to a future refactor.
WORKER_HALTS: frozenset[Status] = TERMINAL | frozenset({Status.AWAITING_APPROVAL})


@dataclass(slots=True)
class ResearchNote:
    question: str
    claim: str
    detail: str
    confidence: str
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "claim": self.claim,
            "detail": self.detail,
            "confidence": self.confidence,
            "sources": list(self.sources),
        }

    @classmethod
    def from_dict(cls, d: dict) -> ResearchNote:
        return cls(
            question=d.get("question", ""),
            claim=d.get("claim", ""),
            detail=d.get("detail", ""),
            confidence=d.get("confidence", "low"),
            sources=list(d.get("sources", [])),
        )


@dataclass(slots=True)
class Item:
    id: int
    source: str  # 'channel' | 'dm'
    status: Status

    source_chat_id: int | None = None
    source_msg_id: int | None = None
    created_at: str | None = None

    raw_text: str = ""
    raw_media_paths: list[str] = field(default_factory=list)

    attempts: int = 0
    next_attempt_at: str | None = None
    last_error: str | None = None

    triage_score: int | None = None
    triage_reason: str | None = None

    extracted: dict = field(default_factory=dict)
    research: list[dict] = field(default_factory=list)
    brief: str | None = None
    slides: list[dict] = field(default_factory=list)
    caption: str | None = None
    regen_note: str | None = None
    theme: str | None = None

    rendered_paths: list[str] = field(default_factory=list)
    media_urls: list[str] = field(default_factory=list)

    approval_msg_id: int | None = None
    ig_child_ids: list[str] = field(default_factory=list)
    ig_carousel_id: str | None = None
    ig_post_id: str | None = None
    published_at: str | None = None

    @property
    def source_domains(self) -> list[str]:
        """Distinct domains that fed the research, for the approval preview.

        This is what gives the operator a chance to spot a relevance failure
        before it reaches Instagram.
        """
        from urllib.parse import urlparse

        seen: list[str] = []
        for note in self.research:
            for url in note.get("sources", []):
                host = urlparse(url).netloc
                if host and host not in seen:
                    seen.append(host)
        return seen
