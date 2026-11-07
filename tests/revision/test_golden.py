"""T295: the T293 spike's 15 notes x 5 decks, with the model's RECORDED plans (designs C, D).

Fixture provenance: scripts/build_t293_golden.py. No model is called here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.revision.model import MODEL_OPS, RULES_OPS, Op, ground, parse_rules, validate_plan

G = json.loads((Path(__file__).parent / "fixtures/t293_golden.json").read_text())
THEMES = G["themes"]
ROWS = [(r, G["notes"][r["deck"]][r["note_id"]], G["decks"][r["deck"]]) for r in G["plans"]]
ID = lambda row: f'{row[0]["design"]}-{row[0]["deck"]}-{row[0]["note_id"]}'  # noqa: E731


def sig(a):
    if a.op in (Op.EDIT_SLIDE, Op.REMOVE_SLIDE, Op.REMOVE_IMAGE, Op.REPLACE_IMAGE, Op.FIX_FACT, Op.ADD_SLIDE):
        return [a.op, a.slide]
    return [a.op, a.theme if a.op == Op.SET_THEME else None]


def run(row):
    r, note, deck = row
    res = validate_plan(r["plan"], deck, THEMES)
    kept, dropped = ground(res.actions, note["note"], deck, THEMES)
    return res, kept, dropped


def test_fixture_shape():
    assert len(G["decks"]) == 5 and len(G["plans"]) == 105
    assert {n["kind"] for d in G["notes"].values() for n in d.values()} >= {"vague", "adversarial"}


@pytest.mark.parametrize("kind", ["vague", "adversarial"])
def test_vague_and_adversarial_notes_end_with_zero_grounded_actions(kind):
    rows = [x for x in ROWS if x[1]["kind"] == kind]
    assert len(rows) == 10  # C: 5 decks; D: the 30-pair subset
    planned = 0
    for row in rows:
        res, kept, _ = run(row)
        planned += len(res.actions)
        assert kept == [], (ID(row), kept)
    assert planned > 0  # the model DID plan something to drop: the test is not vacuous


def test_legit_exact_op_plans_survive_grounding_unchanged():
    """Model ops survive exactly; rules-only ops (U6) are dropped from a MODEL plan -
    the rules front end owns them (see the last test)."""
    checked = rules_dropped = 0
    for row in ROWS:
        r, note, deck = row
        if note["kind"] in ("vague", "adversarial") or not r["plan"]:
            continue
        res, kept, dropped = run(row)
        raw_ops = sorted(a["op"] for a in r["plan"]["actions"])
        if raw_ops != sorted(o for o, _ in note["exp"]):
            continue  # the model's op set was already off; covered below
        checked += 1
        model_exp = [list(e) for e in note["exp"] if e[0] in MODEL_OPS]
        assert sorted(map(sig, kept), key=str) == sorted(model_exp, key=str), (ID(row), kept, dropped)
        for d in dropped:
            assert d.reason == "rules_only" and d.op in RULES_OPS, (ID(row), d)
            rules_dropped += 1
    assert checked >= 60 and rules_dropped >= 20


def test_identity_reorders_are_dropped_not_executed():
    rows = [x for x in ROWS if x[0]["note_id"] == "reorder" and x[0]["design"] == "C"]
    identity = [x for x in rows if x[0]["plan"]["actions"][0]["order"] == sorted(x[0]["plan"]["actions"][0]["order"])]
    assert identity  # the spike's 3/5 failure exists in the data
    for row in identity:
        res, kept, _ = run(row)
        assert kept == [] and res.dropped[0].reason == "noop_reorder"


def test_rules_front_end_explains_the_spike_notes_it_can_and_never_the_rest():
    explained = set()
    for deck_id, notes in G["notes"].items():
        deck = G["decks"][deck_id]
        for nid, n in notes.items():
            acts, full = parse_rules(n["note"], deck, THEMES)
            if full:
                explained.add(nid)
                res = validate_plan({"asks": [], "global": False, "actions": [
                    {"op": a.op, "ask": 0, "slide": a.slide, "instruction": a.instruction, "query": a.query,
                     "theme": a.theme, "order": list(a.order)} for a in acts]}, deck, THEMES)
                assert len(res.actions) == len(acts) and not res.dropped, (deck_id, nid)
                if n["exp"]:
                    assert sorted(a.op for a in acts) == sorted(o for o, _ in n["exp"]), (deck_id, nid)
    assert {"reorder", "remove", "theme", "rm_img"} <= explained
    assert explained.isdisjoint({"vague", "adversarial", "research", "caption", "edit", "recompose"})
