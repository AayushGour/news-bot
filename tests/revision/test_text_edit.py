"""T298: revision/text_edit.py - edit_slide / add_slide / rewrite_caption."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from pipeline.errors import Retryforever
from pipeline.models import Item, Status
from pipeline.revision import text_edit as te
from pipeline.revision.model import Action, OutcomeCode
from pipeline.stages import compose

MATERIAL = "The lab measured 74 plasma shots across 3 days. Tiles are 20 mm thick."
DECK = [
    {"type": "hook", "headline": "Tungsten tiles", "sub": "They survived plasma.", "image": "/Users/x/media/h.jpg",
     "image_mode": "cover"},
    {"type": "point", "headline": "The test", "bullets": ["74 plasma shots", "3 days of runs"],
     "logo": "/Users/x/logo.png", "image": "/Users/x/media/p.jpg"},
    {"type": "takeaway", "headline": "So what", "sub": "It holds up."},
]


def item(**kw):
    base = dict(id=1, source="channel", status=Status.COMPOSED, raw_text=MATERIAL, caption="Old caption.\n\n#fusion",
                research=[{"claim": "74 plasma shots in 3 days", "sources": ["https://a.example/1"]}],
                slides=copy.deepcopy(DECK))
    base.update(kw)
    return Item(**base)


def act(op="edit_slide", slide=2, instruction="shorten the headline"):
    return Action(op=op, ask=1, slide=slide, instruction=instruction)


def point(**kw):
    s = {"type": "point", "headline": "The test", "bullets": ["74 plasma shots", "3 days of runs"]}
    s.update(kw)
    return s


async def run_edit(llm, it=None, action=None):
    it = it or item()
    return await te.edit_slide(llm, it, it.slides, action or act(), MATERIAL)


async def test_edit_applies_and_reattaches_image_logo(fake_llm):
    fake_llm.queue(point(headline="Plasma test"))
    new, out = await run_edit(fake_llm)
    assert out.code is OutcomeCode.APPLIED and out.slide == 2
    assert new["headline"] == "Plasma test"
    assert new["image"] == "/Users/x/media/p.jpg" and new["logo"] == "/Users/x/logo.png"
    assert new["bullets"] == ["74 plasma shots", "3 days of runs"]


async def test_model_supplied_image_is_ignored(fake_llm):
    fake_llm.queue(point(headline="Plasma test", image="/etc/passwd", logo="/etc/shadow"))
    new, _ = await run_edit(fake_llm)
    assert new["image"] == "/Users/x/media/p.jpg" and new["logo"] == "/Users/x/logo.png"


async def test_slide_without_image_stays_without(fake_llm):
    it = item()
    fake_llm.queue({"type": "takeaway", "headline": "So what now", "sub": "It holds up."})
    new, out = await te.edit_slide(fake_llm, it, it.slides, act(slide=3), MATERIAL)
    assert out.code is OutcomeCode.APPLIED and "image" not in new


async def test_prompt_has_no_path_and_wraps_data(fake_llm):
    fake_llm.queue(point(headline="Plasma test"))
    await run_edit(fake_llm)
    call = fake_llm.calls[0]
    assert "/Users/" not in call.user and "logo" not in call.user and "media" not in call.user
    assert call.user.count("[DATA-") >= 3 and "[/DATA-" in call.user
    assert "shorten the headline" in call.user and "74 plasma shots across 3 days" in call.user
    assert call.schema == compose.slide_schema_for("point")


async def test_invented_number_retries_then_reverts(fake_llm):
    bad = point(bullets=["74 plasma shots", "9,999 hours of runs"])
    fake_llm.queue_each([bad, bad])
    it = item()
    before = copy.deepcopy(it.slides)
    new, out = await run_edit(fake_llm, it)
    assert new is None and out.code is OutcomeCode.REJECTED_INTEGRITY
    assert "9999" in out.reason
    assert len(fake_llm.calls) == 2 and "REJECTED" in fake_llm.calls[1].user and "9999" in fake_llm.calls[1].user
    assert it.slides == before


async def test_invented_number_then_fixed_on_retry(fake_llm):
    fake_llm.queue_each([point(bullets=["74 plasma shots", "9,999 hours"]), point(headline="Fixed")])
    new, out = await run_edit(fake_llm)
    assert out.code is OutcomeCode.APPLIED and new["headline"] == "Fixed"


async def test_preexisting_number_is_not_blamed(fake_llm):
    # "5000" is in the original slide but in no source: editing the headline must still pass.
    it = item()
    it.slides[1]["bullets"] = ["5000 things", "3 days of runs"]
    fake_llm.queue(point(headline="Plasma", bullets=["5000 things", "3 days of runs"]))
    new, out = await te.edit_slide(fake_llm, it, it.slides, act(), MATERIAL)
    assert out.code is OutcomeCode.APPLIED and new["headline"] == "Plasma"


async def test_new_absence_claim_rejected_but_preexisting_is_not_blamed(fake_llm):
    claim = "No studies exist on this."
    fake_llm.queue_each([point(headline=claim), point(headline=claim)])
    new, out = await run_edit(fake_llm)
    assert new is None and out.code is OutcomeCode.REJECTED_INTEGRITY and "absence" in out.reason
    it = item()
    it.slides[2]["sub"] = claim  # already in the deck before this edit
    fake_llm.queue(point(headline="Plasma test"))
    new, out = await te.edit_slide(fake_llm, it, it.slides, act(), MATERIAL)
    assert out.code is OutcomeCode.APPLIED


async def test_figure_in_operator_note_is_allowed_but_not_in_model_instruction(fake_llm):
    ins = 'add " (2026)" to the headline'
    fake_llm.queue(point(headline="The test (2026)"))
    new, out = await te.edit_slide(fake_llm, item(), item().slides, act(instruction=ins), MATERIAL, note="add 2026 to slide 2")
    assert out.code is OutcomeCode.APPLIED
    # the year appears ONLY in the (model-written) instruction -> not backed
    fake_llm.queue_each([point(headline="The test (2026)")] * 2)
    new, out = await run_edit(fake_llm, action=act(instruction=ins))
    assert new is None and out.code is OutcomeCode.REJECTED_INTEGRITY and "2026" in out.reason
    fake_llm.queue_each([point(headline="Revenue grew 400%")] * 2)
    new, out = await run_edit(fake_llm, action=act(instruction="say revenue grew 400%"))
    assert out.code is OutcomeCode.REJECTED_INTEGRITY
    fake_llm.queue_each([{"caption": "Revenue grew 400%.", "hashtags": []}] * 2)
    cap, out = await te.rewrite_caption(fake_llm, item(), act("rewrite_caption", slide=0, instruction="say revenue grew 400%"), MATERIAL)
    assert out.code is OutcomeCode.REJECTED_INTEGRITY


REPO = {"type": "repo", "headline": "Cool repo", "owner": "acme", "name": "tool", "url": "https://github.com/acme/tool",
        "stars": 42, "language": "Python", "sub": "A tool.", "logo": "/Users/x/l.png"}


@pytest.mark.parametrize("field,value", [
    ("url", "https://evil.example/phish"), ("owner", "evil"), ("name", "other"),
    ("stars", 99999), ("language", "Rust"), ("logo", "/etc/passwd"),
])
async def test_repo_fact_fields_are_locked(fake_llm, field, value):
    it = item()
    it.slides[1] = dict(REPO)
    tampered = {k: v for k, v in REPO.items() if k != "logo"}
    tampered[field] = value
    tampered["sub"] = "A better tool."
    fake_llm.queue(tampered)
    new, out = await te.edit_slide(fake_llm, it, it.slides, act(), MATERIAL)
    assert out.code is OutcomeCode.APPLIED and new["sub"] == "A better tool."
    for k in te.LOCKED_FIELDS["repo"]:
        assert new[k] == REPO[k]


async def test_identical_repo_reply_is_unchanged(fake_llm):
    it = item()
    it.slides[1] = dict(REPO)
    fake_llm.queue({k: v for k, v in REPO.items() if k != "logo"})
    new, out = await te.edit_slide(fake_llm, it, it.slides, act(), MATERIAL)
    assert new is None and out.code is OutcomeCode.UNCHANGED


async def test_repo_field_added_by_model_is_removed(fake_llm):
    it = item()
    it.slides[1] = {k: v for k, v in REPO.items() if k != "stars"}
    fake_llm.queue({**{k: v for k, v in REPO.items() if k != "logo"}, "sub": "New sub."})
    new, out = await te.edit_slide(fake_llm, it, it.slides, act(), MATERIAL)
    assert "stars" not in new


async def test_cta_in_body_slide_rejected_for_edit_and_add(fake_llm):
    cta = "Follow us for more!"
    fake_llm.queue_each([{"type": "takeaway", "headline": "So what", "sub": cta}] * 2)
    new, out = await run_edit(fake_llm, action=act(slide=3))
    assert new is None and out.code is OutcomeCode.REJECTED_INTEGRITY and "call to action" in out.reason
    fake_llm.queue_each([{"type": "takeaway", "headline": "More", "sub": cta}] * 2)
    new, out = await te.add_slide(fake_llm, item(), item().slides, act("add_slide"), MATERIAL)
    assert new is None and out.code is OutcomeCode.REJECTED_INTEGRITY
    fake_llm.queue_each([{"type": "takeaway", "headline": "More", "sub": cta}, {"type": "takeaway", "headline": "More", "sub": "Fine."}])
    new, out = await te.add_slide(fake_llm, item(), item().slides, act("add_slide"), MATERIAL)
    assert out.code is OutcomeCode.APPLIED


def test_is_body_cta_matches_drop_duplicate_ctas():
    follow = {"type": "follow", "headline": "Follow us", "sub": "for more"}
    body = {"type": "point", "headline": "Follow us for more", "bullets": ["a"]}
    assert not compose.is_body_cta(follow) and compose.is_body_cta(body)
    assert compose.drop_duplicate_ctas([follow, body]) == [follow]


async def test_changed_quote_needs_backing_even_if_original_unbacked(fake_llm):
    it = item()
    it.slides[1] = {"type": "quote", "headline": "Said", "quote": "an unverifiable line", "attribution": "Lab"}
    bad = {"type": "quote", "headline": "Said", "quote": "another invented line", "attribution": "Lab"}
    fake_llm.queue_each([bad, bad])
    new, out = await te.edit_slide(fake_llm, it, it.slides, act(), MATERIAL)
    assert new is None and out.code is OutcomeCode.REJECTED_INTEGRITY
    # unchanged payload, headline edit: the pre-existing unbacked quote is not blamed
    fake_llm.queue({"type": "quote", "headline": "Said so", "quote": "an unverifiable line", "attribution": "Lab"})
    new, out = await te.edit_slide(fake_llm, it, it.slides, act(), MATERIAL)
    assert out.code is OutcomeCode.APPLIED


async def test_preexisting_gap_slide_not_blamed_after_insertion(fake_llm):
    it = item()
    gap = {"type": "takeaway", "headline": "Gap", "sub": "The sources do not cover this."}
    it.slides[1:] = [dict(gap), dict(gap, headline="Gap two"), it.slides[2]]  # 2 gap slides: already a problem
    fake_llm.queue({"type": "takeaway", "headline": "Why 3 days matters", "sub": "Three days of runs."})
    new, out = await te.add_slide(fake_llm, it, it.slides, act("add_slide", slide=1), MATERIAL)
    assert out.code is OutcomeCode.APPLIED


async def test_bad_index_reason_is_out_of_range(fake_llm):
    _, out = await run_edit(fake_llm, action=act(slide=9))
    assert out.reason == "out_of_range"


def test_material_budget_follows_settings(settings):
    assert te._material_budget(None, 100) == te.MATERIAL_BUDGET_CHARS
    small = te._material_budget(settings, 10**9)
    assert small == 0
    assert te._material_budget(settings, 10) <= te.MATERIAL_BUDGET_CHARS


async def test_one_element_array_reply_is_unwrapped(fake_llm):
    fake_llm.queue([point(headline="Plasma test")])
    new, out = await run_edit(fake_llm)
    assert out.code is OutcomeCode.APPLIED and new["headline"] == "Plasma test"
    fake_llm.queue_each([[point(), point()]] * 2)
    new, out = await run_edit(fake_llm)
    assert out.code is OutcomeCode.REJECTED_INTEGRITY
    fake_llm.queue([{"caption": "Tiles took 74 shots.", "hashtags": []}])
    cap, out = await te.rewrite_caption(fake_llm, item(), act("rewrite_caption", slide=0), MATERIAL)
    assert out.code is OutcomeCode.APPLIED


async def test_identical_output_is_unchanged(fake_llm):
    fake_llm.queue(point())
    new, out = await run_edit(fake_llm)
    assert new is None and out.code is OutcomeCode.UNCHANGED
    assert len(fake_llm.calls) == 1


async def test_type_change_rejected(fake_llm):
    other = {"type": "takeaway", "headline": "The test", "sub": "x"}
    fake_llm.queue_each([other, other])
    new, out = await run_edit(fake_llm)
    assert new is None and out.code is OutcomeCode.REJECTED_INTEGRITY
    assert "type" in out.reason


async def test_non_dict_and_empty_body_rejected(fake_llm):
    fake_llm.queue_each(["nope", {"type": "point", "headline": "H", "bullets": []}])
    new, out = await run_edit(fake_llm)
    assert new is None and out.code is OutcomeCode.REJECTED_INTEGRITY


async def test_bad_slide_index_is_invalid(fake_llm):
    for n in (0, 4):
        new, out = await run_edit(fake_llm, action=act(slide=n))
        assert new is None and out.code is OutcomeCode.DROPPED_INVALID
    assert not fake_llm.calls


async def test_input_deck_not_mutated(fake_llm):
    it = item()
    before = copy.deepcopy(it.slides)
    fake_llm.queue(point(headline="Changed"))
    await te.edit_slide(fake_llm, it, it.slides, act(), MATERIAL)
    assert it.slides == before


async def test_retryforever_propagates(fake_llm):
    fake_llm.queue(Retryforever("provider down"))
    with pytest.raises(Retryforever):
        await run_edit(fake_llm)


async def test_retryforever_on_retry_propagates(fake_llm):
    fake_llm.queue_each([point(bullets=["9,999 hours"]), Retryforever("down")])
    with pytest.raises(Retryforever):
        await run_edit(fake_llm)


async def test_unbacked_quote_edit_rejected(fake_llm):
    it = item()
    it.slides[1] = {"type": "quote", "headline": "Said", "quote": "measured 74 plasma shots across 3 days", "attribution": "Lab"}
    bad = {"type": "quote", "headline": "Said", "quote": "We never doubted it", "attribution": "Lab"}
    fake_llm.queue_each([bad, bad])
    new, out = await te.edit_slide(fake_llm, it, it.slides, act(), "The lab measured 74 plasma shots across 3 days.")
    assert new is None and out.code is OutcomeCode.REJECTED_INTEGRITY and "quote" in out.reason


async def test_add_slide_returns_slide_in_addable_types(fake_llm):
    fake_llm.queue({"type": "takeaway", "headline": "Why 3 days matters", "sub": "Three days of runs."})
    it = item()
    new, out = await te.add_slide(fake_llm, it, it.slides, act("add_slide", slide=2, instruction="add why"), MATERIAL)
    assert out.code is OutcomeCode.APPLIED and new["type"] == "takeaway"
    assert "image" not in new and len(it.slides) == 3
    assert "/Users/" not in fake_llm.calls[0].user
    assert te.HOOK_TYPE not in te.ADDABLE_TYPES and not set(te.ADDABLE_TYPES) & set(compose.CLOSING_TYPES)


@pytest.mark.parametrize("kind", ["hook", "follow", "sources", "photo"])
async def test_add_slide_refuses_non_addable_type(fake_llm, kind):
    bad = {"type": kind, "headline": "H", "sub": "s", "image": "x"}
    fake_llm.queue_each([bad, bad])
    it = item()
    new, out = await te.add_slide(fake_llm, it, it.slides, act("add_slide"), MATERIAL)
    assert new is None and out.code is OutcomeCode.REJECTED_INTEGRITY


async def test_add_slide_invented_figure_rejected(fake_llm):
    bad = {"type": "takeaway", "headline": "H", "sub": "Revenue grew 400%."}
    fake_llm.queue_each([bad, bad])
    it = item()
    new, out = await te.add_slide(fake_llm, it, it.slides, act("add_slide"), MATERIAL)
    assert new is None and out.code is OutcomeCode.REJECTED_INTEGRITY and "400" in out.reason


async def test_rewrite_caption_applies_with_credit_and_tags(fake_llm):
    fake_llm.queue({"caption": "Tiles took 74 shots.", "hashtags": ["Fusion", "#tungsten"]})
    it = item()
    cap, out = await te.rewrite_caption(fake_llm, it, act("rewrite_caption", slide=0), MATERIAL, credit="@chan")
    assert out.code is OutcomeCode.APPLIED
    assert cap == "Tiles took 74 shots.\n\nSource: @chan\n\n#fusion #tungsten"
    assert it.caption == "Old caption.\n\n#fusion"


async def test_rewrite_caption_invented_figure_and_unchanged(fake_llm):
    it = item()
    bad = {"caption": "Tiles took 8,000 shots.", "hashtags": []}
    fake_llm.queue_each([bad, bad])
    cap, out = await te.rewrite_caption(fake_llm, it, act("rewrite_caption", slide=0), MATERIAL)
    assert cap is None and out.code is OutcomeCode.REJECTED_INTEGRITY
    fake_llm.queue({"caption": "Old caption.", "hashtags": ["fusion"]})
    cap, out = await te.rewrite_caption(fake_llm, it, act("rewrite_caption", slide=0), MATERIAL)
    assert cap is None and out.code is OutcomeCode.UNCHANGED


async def test_rewrite_caption_empty_and_retryforever(fake_llm):
    fake_llm.queue_each([{"caption": " ", "hashtags": []}, {"caption": "", "hashtags": []}])
    cap, out = await te.rewrite_caption(fake_llm, item(), act("rewrite_caption", slide=0), MATERIAL)
    assert cap is None and out.code is OutcomeCode.REJECTED_INTEGRITY
    fake_llm.queue(Retryforever("down"))
    with pytest.raises(Retryforever):
        await te.rewrite_caption(fake_llm, item(), act("rewrite_caption", slide=0), MATERIAL)


# --- compose must be untouched by the alias (first-compose prompt byte-identical) ---
FIRST_COMPOSE_FINGERPRINT = "dbdcd3907770f3db3f5ba153447e9fe34d6fa4ef26d2d0da4958a4246069d1ac"


async def test_first_compose_prompt_fingerprint_unchanged(fake_llm):
    it = Item(id=1, source="channel", status=Status.SYNTHESIZED, raw_text="news", brief="THE BRIEF",
              research=[{"claim": "c", "sources": ["https://a.example/1"]}])
    doc = {"slides": [{"type": "hook", "headline": "Big news", "sub": "x"},
                      {"type": "point", "headline": "D", "bullets": ["a", "b"]},
                      {"type": "takeaway", "headline": "So", "sub": "c"}], "caption": "c", "hashtags": ["a"]}
    fake_llm.queue(doc)
    fake_llm.queue_each([doc] * 3)
    try:
        await compose.compose(it, fake_llm)
    except Exception:  # only the first call's prompt matters
        pass
    c = fake_llm.calls[0]
    digest = hashlib.sha256(json.dumps([c.system, c.user, c.schema, c.degraded_schema], sort_keys=True).encode())
    assert digest.hexdigest() == FIRST_COMPOSE_FINGERPRINT
    assert compose.slide_schema_for is compose._slide_variant


async def test_photo_slide_edit_applies_and_keeps_image(fake_llm):
    it = item()
    it.slides[1] = {"type": "photo", "headline": "Shot", "caption": "old", "image": "/Users/x/p.jpg", "image_mode": "hero"}
    fake_llm.queue({"type": "photo", "headline": "Shot", "caption": "new", "stars": 999999})
    new, out = await te.edit_slide(fake_llm, it, it.slides, act(), MATERIAL)
    assert out.code is OutcomeCode.APPLIED and new["caption"] == "new"
    assert new["image"] == "/Users/x/p.jpg" and new["image_mode"] == "hero"
    assert "stars" not in new  # undeclared fields never leak through normalise_slides


async def test_legacy_headline_only_slide_is_editable_and_identity_is_unchanged(fake_llm):
    it = item()
    it.slides[1] = {"type": "point", "headline": "Legacy"}
    fake_llm.queue({"type": "point", "headline": "Legacy!"})
    new, out = await te.edit_slide(fake_llm, it, it.slides, act(), MATERIAL)
    assert out.code is OutcomeCode.APPLIED and new == {"type": "point", "headline": "Legacy!"}
    fake_llm.queue({"type": "point", "headline": "Legacy"})
    new, out = await te.edit_slide(fake_llm, it, it.slides, act(), MATERIAL)
    assert new is None and out.code is OutcomeCode.UNCHANGED
    # a NEW slide still needs a body
    fake_llm.queue_each([{"type": "point", "headline": "H"}] * 2)
    new, out = await te.add_slide(fake_llm, it, it.slides, act("add_slide"), MATERIAL)
    assert out.code is OutcomeCode.REJECTED_INTEGRITY


async def test_preexisting_cta_is_baseline_and_identity_is_unchanged_before_gates(fake_llm):
    it = item()
    it.slides[1] = point(headline="Follow us for more", bullets=["74 plasma shots"])
    fake_llm.queue(point(headline="Follow us for more!", bullets=["74 plasma shots"]))
    new, out = await te.edit_slide(fake_llm, it, it.slides, act(), MATERIAL)
    assert out.code is OutcomeCode.APPLIED
    fake_llm.queue(point(headline="Follow us for more", bullets=["74 plasma shots"]))
    new, out = await te.edit_slide(fake_llm, it, it.slides, act(), MATERIAL)
    assert new is None and out.code is OutcomeCode.UNCHANGED and len(fake_llm.calls) == 2


def test_unit_suffix_cannot_attach_across_leaves():
    # "32.1" then a "%" unit leaf must not read as 32.1%
    s = {"type": "chart", "headline": "H", "series": [{"label": "a", "value": 32.1, "display": "32.1"}], "unit": "%"}
    assert te.unbacked_figures(te._slide_text(s), te.typed_numbers_in("value 32.1")) == []


async def test_legacy_branch_headline_is_cleaned_like_compose(fake_llm):
    # compose's normalise_slides strips a headline and applies no length cap; the
    # legacy (headline-only original) branch must match it, not invent its own rule.
    long = "  " + "x" * 500 + "  "
    it = item()
    it.slides[1] = {"type": "point", "headline": "Legacy"}
    fake_llm.queue({"type": "point", "headline": long})
    new, out = await te.edit_slide(fake_llm, it, it.slides, act(), MATERIAL)
    via_compose = compose.normalise_slides([{"type": "point", "headline": long, "sub": "s"}])[0]["headline"]
    assert out.code is OutcomeCode.APPLIED and new["headline"] == via_compose == "x" * 500
