"""T298 D1 audit: every slide type in real decks survives an identity-ish edit.

Cases are frozen in tests/fixtures/t298_real_decks.json (real decks, local paths
replaced by placeholders; rebuilt only by scripts/build_t298_fixture.py). The tests
never read the live database. For each real slide: reply = the original minus the
fields the model never sees, headline tweaked -> APPLIED; reply = the original
unchanged -> UNCHANGED. Pre-existing flaws in the stored slide (legacy headline-only
slides, a body CTA, unbacked payloads) must not be blamed on the edit.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.models import Item, Status
from pipeline.revision import text_edit as te
from pipeline.revision.model import Action, OutcomeCode
from pipeline.deck_shape import LEGACY_SLIDE_TYPES
from pipeline.rich_text import migrate_slide
from pipeline.stages.compose import REQUIRED_BODY_FIELDS

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "t298_real_decks.json"
DECKS = json.loads(FIXTURE.read_text())


TYPES = sorted(set(REQUIRED_BODY_FIELDS) - set(LEGACY_SLIDE_TYPES))


def _cases(kind):
    # T362: the fixture predates "notes"; its legacy "code" slides are edited as notes.
    return [
        (d["id"], d["slides"], i) for d in DECKS for i in d["idx"]
        if migrate_slide(d["slides"][i]).get("type") == kind
    ]


def test_fixture_covers_every_slide_type():
    assert {k for k in TYPES if not _cases(k)} == set()


@pytest.mark.parametrize("kind", TYPES)
async def test_identity_ish_edit_of_real_slides_applies(fake_llm, kind):
    cases = _cases(kind)
    assert cases, f"fixture has no {kind} slide"
    for iid, slides, idx in cases:
        it = Item(id=iid, source="channel", status=Status.COMPOSED, raw_text="", caption="", research=[],
                  slides=copy.deepcopy(slides))
        raw = copy.deepcopy(it.slides[idx])
        orig = migrate_slide(raw)
        material = json.dumps(orig, ensure_ascii=False)  # every figure/quote already "in the source"
        model_view = {k: v for k, v in orig.items() if k not in te._UNSENT_FIELDS}
        act = Action(op="edit_slide", ask=0, slide=idx + 1, instruction="tweak the headline")

        fake_llm.queue(model_view | {"headline": f"{orig['headline']}!"})
        new, out = await te.edit_slide(fake_llm, it, it.slides, act, material)
        assert out.code is OutcomeCode.APPLIED, (kind, iid, idx, out.reason)
        assert new["headline"].endswith("!") and new["type"] == kind
        for k in te._UNSENT_FIELDS:
            assert (k in new) == (k in orig) and new.get(k) == orig.get(k), (kind, iid, idx, k)
        assert it.slides[idx] == raw

        fake_llm.queue(model_view)
        new, out = await te.edit_slide(fake_llm, it, it.slides, act, material)
        assert new is None and out.code is OutcomeCode.UNCHANGED, (kind, iid, idx, out.reason)
