"""Send an item back to an earlier stage, clearing what that stage will redo.

Requeuing by hand means remembering which columns hold stale work. Miss one and
the item carries old output into a fresh stage, which looks like a model fault
and is not: item 26 composed against research notes written before repo URLs
were captured, and produced eight links nothing could verify.

Each stage therefore declares what it produces. Requeuing to a stage clears its
own outputs and every later stage's, so an item can never advertise a status
whose inputs have already been wiped.
"""

from __future__ import annotations

from .models import Status

#: A status names the work already finished, and the worker's stage map keys
#: the NEXT stage off it — Status.INGESTED runs extract, Status.EXTRACTED runs
#: triage, and so on. So requeuing to a status must clear what the stage that
#: runs *from* it produces, not that status's own output. Clearing `extracted`
#: on the way to EXTRACTED would have run triage with the vision descriptions
#: already wiped, which is exactly the blind triage the map's comment warns
#: about. Ordered earliest first — requeuing clears from that stage down.
STAGE_OUTPUTS: list[tuple[Status, dict]] = [
    (Status.INGESTED, {"extracted": {}}),
    (Status.EXTRACTED, {"triage_score": None, "triage_reason": None}),
    (Status.TRIAGED, {"research": [], "intent": None, "confidence": None,
                      # Fresh research means the old gaps are unknown again.
                      "gaps": []}),
    (Status.RESEARCHED, {"brief": None}),
    (Status.SYNTHESIZED, {"slides": [], "caption": None, "theme": None}),
    (Status.COMPOSED, {"rendered_paths": []}),
    (Status.RENDERED, {"approval_msg_id": None}),
    # upload runs from APPROVED, so the urls it writes belong to that stage.
    (Status.APPROVED, {"media_urls": []}),
    # An Instagram container has its caption and image URLs baked in when it is
    # created. Reusing one after a recompose publishes the PREVIOUS deck — the
    # reuse is a double-post guard, and without clearing these a requeue turns
    # it into a stale-post guarantee.
    # ig_post_id is cleared deliberately: publish_carousel returns early when
    # it is set, so leaving it would make a redo silently republish nothing.
    # The item's `publish_log` is NOT listed here and never is — it is the only
    # record that survives a requeue, and the dashboard reads it to warn that
    # approving again adds a second carousel to a live account.
    (Status.PUBLISHING, {"ig_child_ids": [], "ig_carousel_id": None,
                         "ig_post_id": None, "published_at": None}),
]

#: Stage name -> position in the pipeline, for callers offering a choice.
TARGETS: dict[str, int] = {s.value: i for i, (s, _) in enumerate(STAGE_OUTPUTS)}

#: Cleared on every requeue regardless of target: a requeue is a fresh attempt,
#: not the continuation of a failing one.
_ALWAYS_CLEARED = {
    "attempts": 0,
    "last_error": None,
    "next_attempt_at": None,
    "regen_note": None,
    "question": None,
    "answer": None,
    "resume_status": None,
}


def fields_to_clear(target: Status) -> dict:
    """Everything the target stage and all later stages would regenerate."""
    if target.value not in TARGETS:
        raise ValueError(f"{target.value} is not a requeueable stage")
    cleared: dict = {}
    for _, outputs in STAGE_OUTPUTS[TARGETS[target.value]:]:
        cleared.update(outputs)
    cleared.update(_ALWAYS_CLEARED)
    return cleared


async def requeue(db, item_id: int, target: Status, detail: str = "requeue") -> dict:
    """Move an item back to ``target``, clearing downstream work.

    Returns the fields cleared, so a caller can report what it did.
    """
    cleared = fields_to_clear(target)
    # transition() writes fields and status in one statement, so a crash
    # mid-requeue cannot leave an item pointing at a stage whose inputs are
    # already gone.
    await db.transition(item_id, target, cleared, detail=detail)
    return cleared
