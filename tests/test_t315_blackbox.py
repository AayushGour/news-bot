"""T315 blackbox: swap/move/remove-image slides are moved, not edited (D283).

Public API only (plan_revision / ground / parse_rules), fake LLM, frozen real decks
from tests/fixtures/t295_real_decks.json. No data/app.db access.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pipeline.revision.model import RULES_OPS, Reason, parse_rules
from pipeline.revision.plan import PlanFailed, plan_revision
from pipeline.stages.render import available_themes

ROOT = Path(__file__).resolve().parents[1]
DECKS = json.loads((ROOT / "tests/fixtures/t295_real_decks.json").read_text())
KEYS = list(DECKS)
THEMES = available_themes()
CAPTION = "A caption #ai"


class Fake:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    async def good(self, *a, **k):
        self.calls += 1
        return self.answer


def edit(slide, ask=0):
    return {"op": "edit_slide", "ask": ask, "slide": slide, "instruction": "tighten", "query": "",
            "theme": "", "order": []}


CAP = {"op": "rewrite_caption", "ask": 0, "slide": 0, "instruction": "casual", "query": "", "theme": "", "order": []}
RMI = lambda s: {"op": "remove_image", "ask": 0, "slide": s, "instruction": "", "query": "", "theme": "", "order": []}  # noqa: E731


def surviving(note, deck, model_actions, **kw):
    ans = {"asks": ["do it"], "global": False, "actions": model_actions}
    llm = Fake(ans)
    try:
        r = asyncio.run(plan_revision(llm, note, deck, CAPTION, [], THEMES, **kw))
    except PlanFailed:
        return None, llm
    return {(a.op, a.slide) for a in r.actions if a.op not in RULES_OPS}, llm


def rules_ops(note, deck):
    acts, _ = parse_rules(note, deck, THEMES)
    return sorted(a.op for a in acts)


# deck "wide": 10 slides  hook facts flow quote photo point point point sources follow
WIDE = [dict(x, image="img/x.jpg") if i == 4 else dict(x) for i, x in enumerate(DECKS[KEYS[1]])]
# (note, model over-edits, expected surviving model ops)
E = lambda *s: {("edit_slide", x) for x in s}  # noqa: E731
CASES = [
    ("swap slides 2 and 4 and make the caption casual", [edit(2), edit(4), CAP], {("rewrite_caption", 0)}),
    ("move slide 3 to the end and shorten slide 5", [edit(3), edit(5)], E(5)),
    ("swap slides 2 and 4, then shorten slide 2", [edit(2), edit(4)], E(2)),
    ("swap slides 2 and 4 and make it punchier", [edit(2), edit(4)], E(2, 4)),
    ("move slide 3 to slide 6 and tighten both", [edit(3), edit(6)], E(3)),
    ("move slide 3 after slide 6, make it shorter", [edit(3), edit(6)], E(3)),
    ("tighten the quote slide, swap slides 3 and 5", [edit(3), edit(4), edit(5)], E(4)),
    ("swap slides 2 and 3 and rewrite the flow slide", [edit(2), edit(3)], E(3)),
    ("remove the image on slide 5 and shorten slide 5", [RMI(5), edit(5)], {("remove_image", 5), ("edit_slide", 5)}),
    ("move slide 2 to the start and tighten slide 4", [edit(2), edit(4)], E(4)),
    ("swap slides 6 and 7, rewrite the caption and tighten the quote slide", [edit(6), edit(7), CAP, edit(4)],
     {("rewrite_caption", 0)} | E(4)),
    ("swap slides 2 and 4. They are too wordy", [edit(2), edit(4)], E(2, 4)),
    ("move slide 4 before slide 2 and shorten slide 2", [edit(2), edit(4)], E(2)),
    ("drop slide 4 and swap slides 5 and 6", [edit(4), edit(5), edit(6)], set()),
    ("remove the image on slide 5", [RMI(5), edit(5)], {("remove_image", 5)}),
    ("swap slides 2 and 4 and tighten slide 6", [edit(2), edit(4), edit(6)], E(6)),
    ("move slide 5 to slide 2, then make its headline shorter", [edit(5), edit(2)], E(5)),
]


@pytest.mark.parametrize("note,over,expected", CASES, ids=[c[0][:50] for c in CASES])
def test_ac1_ac2_exact_surviving_sets(note, over, expected):
    got, llm = surviving(note, WIDE, over)
    if got is None:  # nothing left for the planner to keep -> PlanFailed is not acceptable here
        pytest.fail("PlanFailed")
    assert got == expected, (note, got)


def test_ac1_d4_repro_on_every_real_deck():
    for k in KEYS:
        deck = DECKS[k]
        got, _ = surviving("swap slides 2 and 4 and make the caption casual", deck, [edit(2), edit(4), CAP])
        assert got == {("rewrite_caption", 0)}, (k, got)
        r = asyncio.run(plan_revision(Fake({"asks": ["c"], "global": False, "actions": [edit(2), edit(4), CAP]}),
                                      "swap slides 2 and 4 and make the caption casual", deck, CAPTION, [], THEMES))
        assert sorted(a.op for a in r.actions) == ["reorder", "rewrite_caption"]


def test_ac2_consumed_reason_and_dropped_recorded():
    ans = {"asks": ["x"], "global": False, "actions": [edit(2), edit(4), CAP]}
    r = asyncio.run(plan_revision(Fake(ans), "swap slides 2 and 4 and make the caption casual", WIDE,
                                  CAPTION, [], THEMES))
    assert {d.reason for d in r.dropped if d.slide in (2, 4)} == {Reason.CONSUMED_BY_RULES}


def test_ac2_destination_never_readded_by_pronoun():
    got, _ = surviving("move slide 3 to slide 6 and tighten them", WIDE, [edit(3), edit(6)])
    assert ("edit_slide", 6) not in got and ("edit_slide", 3) in got


def test_ac2_negated_pronoun_observation():
    """OBSERVATION: 'don't touch them' contains a back-reference, so the pair's edits survive
    grounding (edit_slide has no negation guard). Recorded, not asserted as desired."""
    got, _ = surviving("swap slides 2 and 4 but don't touch them", WIDE, [edit(2), edit(4)])
    print("negated pronoun ->", got)


@pytest.mark.parametrize("note", [c[0] for c in CASES])
def test_ac3_rules_actions_pass_through_unchanged(note):
    """Whatever the planner drops, the rules actions parse_rules produced are all returned."""
    rules, _ = parse_rules(note, WIDE, THEMES)
    r = asyncio.run(plan_revision(Fake({"asks": [], "global": False, "actions": []}), note, WIDE, CAPTION, [], THEMES))
    want = sorted((a.op, a.slide, a.order) for a in rules)
    assert sorted((a.op, a.slide, a.order) for a in r.actions if (a.op, a.slide, a.order) in want) == want
