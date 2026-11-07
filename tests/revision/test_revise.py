"""T301: stages/revise.py end to end through the real Worker, a real (temp) database,
the real planner, executors and check, with a fake LLM and fake web.

FakeLLM answers are consumed in call order, so each test reads as the script of
model calls the stage is expected to make: planner (good), edits (good), judge (cheap).
Nothing here reads data/app.db or talks to a network.
"""

from __future__ import annotations

import copy
import itertools
import json
from dataclasses import replace

import pytest

from pipeline.__main__ import partial_stage
from pipeline.config import Settings
from pipeline.db import Database
from pipeline.errors import BadCompletion, ConfigFault, EnginesBlocked, Retryforever
from pipeline.models import WORKER_HALTS, Status
from pipeline.revision import execute as execute_mod
from pipeline.revision import fetch as fetch_mod
from pipeline.revision import request as rq
from pipeline.revision.model import Action, Op, Outcome, OutcomeCode
from pipeline.revision.plan import PlanFailed
from pipeline.stages import revise as revise_mod
from pipeline.stages.render import available_themes
from pipeline.stages.revise import Notice, NoticeKind, revise
from pipeline.worker import Worker
from tests.conftest import FakeHTTP

RAW = "Fusion lab reports tungsten tiles survived plasma exposure."
IMG = "/media/42/slide3.png"
DECK = [
    {"type": "hook", "headline": "Tungsten tiles survive plasma", "sub": "A fusion lab report."},
    {"type": "point", "headline": "The test", "bullets": ["Tiles sat in the plasma", "Runs lasted for days"]},
    {"type": "point", "headline": "The result", "bullets": ["Tiles held up", "Surface stayed intact"],
     "image": IMG, "image_mode": "inset"},
    {"type": "point", "headline": "Pricing", "bullets": ["Tiles are costly", "Supply is thin"]},
    {"type": "takeaway", "headline": "So what", "sub": "It holds up."},
    {"type": "sources", "headline": "Sources", "urls": ["https://a.example/1"]},
    {"type": "follow", "headline": "Follow for more", "sub": "Daily tech, explained."},
]
CAPTION = "Tiles survived the plasma.\n\n#fusion"
RESEARCH = [{"claim": "Tungsten tiles survived plasma exposure", "detail": "The lab said so.",
             "sources": ["https://a.example/1"], "confidence": "high"}]
RENDERED = ["/media/42/s1.png", "/media/42/s2.png"]
JUDGE_OK = {"uncovered": []}


def point(**kw):
    s = {"type": "point", "headline": "The result", "bullets": ["Tiles held up", "Surface stayed intact"]}
    s.update(kw)
    return s


def act(op, *, ask=0, slide=0, instruction="", query="", theme="", order=()):
    return {"op": op, "ask": ask, "slide": slide, "instruction": instruction, "query": query,
            "theme": theme, "order": list(order)}


def plan(asks, *actions):
    return {"asks": list(asks), "global": False, "actions": list(actions)}


@pytest.fixture
def cfg(settings: Settings) -> Settings:
    return replace(settings, verify_enabled=False, media_dir=settings.media_dir)


class Notices:
    """Collects what the stage would tell the operator."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, Notice]] = []

    async def __call__(self, item, notice):
        self.sent.append((item.id, notice))

    def kinds(self):
        return [n.kind for _, n in self.sent]


@pytest.fixture
def notices() -> Notices:
    return Notices()


_msg_ids = itertools.count(1000)


async def seed(db, *, request, status=Status.REVISION_REQUESTED, **fields) -> int:
    i = await db.insert_item(source="channel", source_chat_id=-1, source_msg_id=next(_msg_ids), raw_text=RAW)
    base = {"slides": copy.deepcopy(DECK), "caption": CAPTION, "research": copy.deepcopy(RESEARCH),
            "brief": "brief", "theme": "signal", "rendered_paths": list(RENDERED),
            "revision_request": request}
    base.update(fields)
    await db.transition(i, status, base)
    return i


def req(note="slide 3 shorter", origin="operator", **kw):
    r = rq.new_request(origin=origin, note=note, surface="telegram")
    r.update(kw)
    return r


def worker(db, llm, cfg, http=None, notify=None) -> Worker:
    stage = partial_stage(revise, llm=llm, http=http or FakeHTTP(), settings=cfg, db=db, notify=notify)
    return Worker(db, {Status.REVISION_REQUESTED: (stage, Status.COMPOSED)})


async def run(db, llm, cfg, **kw):
    await worker(db, llm, cfg, **kw).tick()
    # Every test is the exact script of model calls: a reply nobody asked for means
    # the stage made fewer calls than the test believes (or a reply went to the wrong caller).
    assert not llm._queue, f"{len(llm._queue)} scripted reply(ies) never consumed"


def roles(llm):
    return [c.role for c in llm.calls]


def last_run(item) -> dict:
    return item.revision["runs"][-1]


def fingerprint(slides):
    return [json.dumps(s, sort_keys=True) for s in slides]


# ---------------------------------------------------------------- one op each


async def test_edit_slide_end_to_end_changes_only_that_slide(db, cfg, fake_llm):
    i = await seed(db, request=req("slide 3 shorter"))
    fake_llm.queue_each([
        plan(["shorten slide 3"], act("edit_slide", slide=3, instruction="shorten, keep the facts")),
        point(bullets=["Tiles held up"]),
        JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)

    item = await db.get_item(i)
    assert roles(fake_llm) == ["good", "good", "cheap"]
    assert item.status is Status.COMPOSED
    assert item.slides[2]["bullets"] == ["Tiles held up"]
    # The model never sees or returns the picture; it is reattached from the original.
    assert item.slides[2]["image"] == IMG and item.slides[2]["image_mode"] == "inset"
    assert fingerprint([s for n, s in enumerate(item.slides) if n != 2]) == fingerprint(
        [s for n, s in enumerate(DECK) if n != 2])
    assert item.revision_request is None
    run_ = last_run(item)
    assert run_["check"] == "satisfied" and run_["changed"] == [3] and run_["rounds"] == 1
    assert run_["actions"] == [{"op": "edit_slide", "slide": 3, "ask": 0, "outcome": "applied", "reason": ""}]
    events = await db.events_for(i)
    assert events[-1]["to_status"] == "composed" and "1/1 action(s) applied" in events[-1]["detail"]


async def test_caption_only_goes_back_to_rendered_and_keeps_the_pngs(db, cfg, fake_llm):
    i = await seed(db, request=req("make the caption more casual"))
    fake_llm.queue_each([
        plan(["casual caption"], act("rewrite_caption", instruction="casual tone")),
        {"caption": "Tiles survived the plasma, wild.", "hashtags": ["fusion"]},
        JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)

    item = await db.get_item(i)
    assert item.status is Status.RENDERED  # D257: nothing to re-render
    assert item.rendered_paths == RENDERED and item.slides == DECK
    assert "wild" in item.caption and last_run(item)["caption_changed"] is True
    assert last_run(item)["changed"] == []


async def test_no_change_at_all_is_rendered_with_an_honest_record(db, cfg, fake_llm):
    i = await seed(db, request=req("slide 3 shorter"))
    same = point()  # the model returns the slide untouched
    fake_llm.queue_each([
        plan(["shorten slide 3"], act("edit_slide", slide=3, instruction="shorten")),
        same, same,  # edit attempt + the integrity-style retry on no change? (one call each)
    ])
    # Nothing changed -> no judge call (check_revision answers in code); the replan is
    # offered once, and the planner now plans nothing.
    fake_llm.queue_each([plan([])])
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert item.status is Status.RENDERED and item.slides == DECK
    assert last_run(item)["check"] == "unsatisfied"
    assert last_run(item)["actions"][0]["outcome"] == "unchanged"


async def test_remove_image_is_rules_only_with_no_planner_call(db, cfg, fake_llm):
    i = await seed(db, request=req("remove the photo on slide 3"))
    fake_llm.queue(JUDGE_OK)
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert roles(fake_llm) == ["cheap"]  # rules explained the note: 0 planner calls
    assert "image" not in item.slides[2] and "image_mode" not in item.slides[2]
    assert item.status is Status.COMPOSED and last_run(item)["changed"] == [3]


async def test_set_theme_pins_it_and_rerenders(db, cfg, fake_llm):
    name = available_themes()[0]
    i = await seed(db, request=req(f"use the {name} theme"))
    fake_llm.queue(JUDGE_OK)
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert item.theme_pin == name and item.slides == DECK
    assert item.status is Status.COMPOSED and last_run(item)["theme"] == name


async def test_reorder_swaps_and_untouched_slides_are_byte_identical(db, cfg, fake_llm):
    i = await seed(db, request=req("swap slides 2 and 4"))
    fake_llm.queue(JUDGE_OK)
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert [s["headline"] for s in item.slides][:4] == [
        "Tungsten tiles survive plasma", "Pricing", "The result", "The test"]
    assert sorted(fingerprint(item.slides)) == sorted(fingerprint(DECK))
    assert last_run(item)["reordered"] is True and item.status is Status.COMPOSED


async def test_add_slide_inserts_after_the_anchor(db, cfg, fake_llm):
    i = await seed(db, request=req("add a new slide after slide 2 about the lab test"))
    fake_llm.queue_each([
        plan(["add a slide about the lab test"], act("add_slide", slide=2, instruction="a slide about the lab test")),
        {"type": "point", "headline": "Inside the lab", "bullets": ["Tiles sat in the plasma"]},
        JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert [s["headline"] for s in item.slides][:4] == [
        "Tungsten tiles survive plasma", "The test", "Inside the lab", "The result"]
    assert len(item.slides) == len(DECK) + 1
    assert last_run(item)["added"] == [3] and last_run(item)["check"] == "satisfied"


async def test_replace_image_and_new_research_reach_the_later_edit(db, cfg, fake_llm, monkeypatch):
    """research -> image -> text order, and the new note is priority material for the edit."""
    seen = {}
    new_note = {"claim": "Launch is set for March", "sources": ["https://b.example/2"], "origin": "revision"}

    async def fake_research(llm, item, action, settings, http):
        seen["research_before_edit"] = True
        return [new_note], Outcome(action.op, OutcomeCode.APPLIED, 0, action.ask)

    async def fake_replace(item, deck, action, settings, http):
        assert seen.get("research_before_edit")
        new = copy.deepcopy(deck[action.slide_id])
        new["image"] = "/media/researched/42/rev_aa.png"
        return new, Outcome(action.op, OutcomeCode.APPLIED, action.slide, action.ask, "image_unchecked")

    monkeypatch.setattr(fetch_mod, "research", fake_research)
    monkeypatch.setattr(fetch_mod, "replace_image", fake_replace)
    i = await seed(db, request=req(
        "find out when it launches and add it to slide 4, and a clearer photo on slide 3"))
    fake_llm.queue_each([
        plan(["find the launch date", "add it to slide 4", "clearer photo on slide 3"],
             act("research", ask=0, query="tile launch date"),
             act("edit_slide", ask=1, slide=4, instruction="add the launch date"),
             act("replace_image", ask=2, slide=3, query="tungsten tile close-up photo")),
        point(headline="Pricing", bullets=["Launch is set for March", "Supply is thin"]),
        JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert item.slides[2]["image"].endswith("rev_aa.png")
    assert "Launch is set for March" in item.slides[3]["bullets"]
    edit_prompt = fake_llm.calls[1].user
    assert "Launch is set for March" in edit_prompt  # priority material, threaded in
    assert item.research[-1]["claim"] == "Launch is set for March"  # persisted, origin kept
    assert item.research[-1]["origin"] == "revision"
    assert [a["op"] for a in last_run(item)["actions"]] == ["research", "replace_image", "edit_slide"]


# ------------------------------------------------------------- confirm gate


async def test_destructive_action_is_parked_and_nothing_runs_without_yes(db, cfg, fake_llm, notices):
    i = await seed(db, request=req("remove slide 4"))
    await run(db, fake_llm, cfg, notify=notices)

    item = await db.get_item(i)
    assert item.status is Status.REVISION_CONFIRM
    assert item.status in WORKER_HALTS
    assert item.slides == DECK and fake_llm.calls == []  # rules-only plan: not one model call
    pc = rq.pending_confirm(item.revision_request)
    assert pc and pc[rq.P_ANSWER] is None and len(pc[rq.P_TOKEN]) == 8
    assert [a["op"] for a in pc[rq.P_PLAN][rq.PL_ACTIONS]] == ["remove_slide"]
    # told AFTER the transition committed, with the data the prompt needs
    assert notices.kinds() == [NoticeKind.CONFIRM]
    notice = notices.sent[0][1]
    assert notice.token == pc[rq.P_TOKEN] and [a.slide for a in notice.held] == [4]
    # the worker will not touch it, however often it ticks
    assert await worker(db, fake_llm, cfg).tick() == 0


async def test_yes_runs_the_stored_plan_survives_a_restart_and_removes(db, cfg, fake_llm, tmp_path):
    i = await seed(db, request=req("remove slide 4"))
    await run(db, fake_llm, cfg)
    token = rq.pending_confirm((await db.get_item(i)).revision_request)[rq.P_TOKEN]

    # restart: a brand new connection to the same file
    await db.close()
    db2 = await Database(tmp_path / "t.db").connect()
    try:
        assert (await db2.get_item(i)).status is Status.REVISION_CONFIRM
        assert await rq.confirm_revision(db2, i, True, token) is rq.ConfirmResult.YES
        fake_llm.queue(JUDGE_OK)
        await run(db2, fake_llm, cfg)
        item = await db2.get_item(i)
    finally:
        await db2.close()
    assert item.status is Status.COMPOSED and len(item.slides) == len(DECK) - 1
    assert "Pricing" not in [s["headline"] for s in item.slides]
    assert last_run(item)["removed"] == [4] and item.revision_request is None
    assert roles(fake_llm) == ["cheap"]  # the stored plan ran: no second planning


async def test_no_drops_the_destructive_action_and_runs_the_rest(db, cfg, fake_llm):
    i = await seed(db, request=req("remove slide 4 and tighten slide 2"))
    fake_llm.queue(plan(["tighten slide 2"], act("edit_slide", ask=0, slide=2, instruction="tighten")))
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert item.status is Status.REVISION_CONFIRM
    held = [a["op"] for a in rq.pending_confirm(item.revision_request)[rq.P_PLAN][rq.PL_ACTIONS]]
    assert sorted(held) == ["edit_slide", "remove_slide"]

    token = rq.pending_confirm(item.revision_request)[rq.P_TOKEN]
    assert await rq.confirm_revision(db, i, False, token) is rq.ConfirmResult.NO_CONTINUE
    fake_llm.queue_each([point(headline="The test", bullets=["Tiles sat in plasma"]), JUDGE_OK])
    await run(db, fake_llm, cfg)

    item = await db.get_item(i)
    assert item.status is Status.COMPOSED and len(item.slides) == len(DECK)  # slide 4 kept
    assert "Pricing" in [s["headline"] for s in item.slides]
    rows = {(a["op"], a["outcome"]) for a in last_run(item)["actions"]}
    assert rows == {("edit_slide", "applied"), ("remove_slide", "declined")}
    # a declined removal is not reported as a failure of the note
    assert last_run(item)["check"] == "satisfied"


async def test_no_with_nothing_else_returns_to_the_gate_without_a_preview(db, cfg, fake_llm):
    i = await seed(db, request=req("remove slide 4"), status=Status.REVISION_REQUESTED)
    await run(db, fake_llm, cfg)
    token = rq.pending_confirm((await db.get_item(i)).revision_request)[rq.P_TOKEN]
    assert await rq.confirm_revision(db, i, False, token) is rq.ConfirmResult.NO_NOTHING_LEFT
    item = await db.get_item(i)
    assert item.status is Status.AWAITING_APPROVAL and item.slides == DECK
    assert item.revision_request is None and fake_llm.calls == []
    assert last_run(item)["actions"][0]["outcome"] == "declined"


async def test_rebuild_needs_yes_then_composes_with_the_previous_deck(db, cfg, fake_llm, monkeypatch):
    composed = {}

    async def fake_compose(item, llm, settings=None):
        composed["note"], composed["had_slides"] = item.regen_note, bool(item.slides)
        return {"slides": [{"type": "hook", "headline": "Fresh"}], "caption": "new", "theme": "signal",
                "regen_note": None, "verification": None}

    monkeypatch.setattr(revise_mod, "compose", fake_compose)
    i = await seed(db, request=req("start over from scratch"))
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert item.status is Status.REVISION_CONFIRM and composed == {} and item.slides == DECK
    token = rq.pending_confirm(item.revision_request)[rq.P_TOKEN]
    assert await rq.confirm_revision(db, i, True, token) is rq.ConfirmResult.YES
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert composed == {"note": "start over from scratch", "had_slides": True}
    assert item.status is Status.COMPOSED and item.slides[0]["headline"] == "Fresh"
    assert last_run(item)["fallback"] is True and last_run(item)["cause"] == "rebuild"


# ------------------------------------------------------------ fallback ladder


async def test_flag_off_is_the_legacy_regen_and_calls_nothing(db, cfg, fake_llm):
    off = replace(cfg, revise_enabled=False)
    i = await seed(db, request=req("make it punchier"))
    await run(db, fake_llm, off)
    item = await db.get_item(i)
    assert item.status is Status.SYNTHESIZED and item.regen_note == "make it punchier"
    assert item.revision_request is None and item.slides == DECK and fake_llm.calls == []


async def test_flag_off_skip_is_a_note_less_regen(db, cfg, fake_llm):
    off = replace(cfg, revise_enabled=False)
    i = await seed(db, request=rq.new_request(origin="skip", surface="telegram"))
    await run(db, fake_llm, off)
    item = await db.get_item(i)
    assert item.status is Status.SYNTHESIZED and item.regen_note is None


@pytest.mark.parametrize("request_, fields, cause", [
    (rq.new_request(origin="skip", surface="telegram"), {}, "skip"),
    (rq.new_request(origin="operator", note="  ", surface="telegram"), {}, "skip"),
    (req("shorten slide 3"), {"slides": []}, "no_slides"),
])
async def test_ladder_composes_with_the_previous_deck(db, cfg, fake_llm, monkeypatch, request_, fields, cause):
    seen = {}

    async def fake_compose(item, llm, settings=None):
        seen["note"], seen["slides"] = item.regen_note, len(item.slides)
        return {"slides": copy.deepcopy(DECK), "caption": "c", "theme": "signal",
                "regen_note": None, "verification": None}

    monkeypatch.setattr(revise_mod, "compose", fake_compose)
    i = await seed(db, request=request_, **fields)
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert item.status is Status.COMPOSED and fake_llm.calls == []
    assert seen["note"] == (request_["note"].strip() if request_["note"] and request_["note"].strip() else None)
    assert seen["slides"] == len(fields.get("slides", DECK))
    run_ = last_run(item)
    assert run_["fallback"] is True and run_["cause"] == cause and run_["check"] is None
    assert item.revision_request is None


async def test_planner_failure_without_rules_asks_the_operator_not_a_rebuild(db, cfg, fake_llm, monkeypatch):
    """D289: no whole-deck rebuild without a Yes. The deck and preview stay; the run says why."""
    async def boom(*a, **k):
        raise PlanFailed(["bad json"], [])

    async def no_compose(*a, **k):
        raise AssertionError("compose must not run")

    monkeypatch.setattr(revise_mod, "plan_revision", boom)
    monkeypatch.setattr(revise_mod, "compose", no_compose)
    i = await seed(db, request=req("rework the second half"))
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert item.status is Status.AWAITING_APPROVAL and item.revision_request is None
    assert [s["headline"] for s in item.slides] == [s["headline"] for s in DECK]
    r = last_run(item)
    assert r["ask_operator"] is True and r["plan_failed"] is True and r["fallback"] is False


async def test_planner_failure_still_reports_a_theme_that_does_not_exist(db, cfg, fake_llm, monkeypatch):
    """The ask names the theme problem even though the planner itself failed (so cause = WHICH_THEME)."""
    from pipeline.revision.copy import AskCause, ask_cause

    async def boom(*a, **k):
        raise PlanFailed(["bad json"], [])

    monkeypatch.setattr(revise_mod, "plan_revision", boom)
    i = await seed(db, request=req("use the xyzzy theme"))
    await run(db, fake_llm, cfg)
    r = last_run(await db.get_item(i))
    assert r["ask_operator"] and r["plan_failed"] and [d["reason"] for d in r["dropped"]] == ["bad_theme"]
    assert ask_cause(r["dropped"]) is AskCause.WHICH_THEME
    j = await seed(db, request=req("rework the second half"))
    await run(db, fake_llm, cfg)
    assert last_run(await db.get_item(j))["dropped"] == []


async def test_planner_failure_keeps_the_rules_actions(db, cfg, fake_llm, monkeypatch):
    rules = [Action(op="reorder", ask=0, order=(1, 4, 3, 2, 5, 6, 7))]

    async def boom(*a, **k):
        raise PlanFailed(["bad json"], rules)

    monkeypatch.setattr(revise_mod, "plan_revision", boom)
    i = await seed(db, request=req("swap slides 2 and 4 and make it nicer"))
    fake_llm.queue(JUDGE_OK)
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert [s["headline"] for s in item.slides][1] == "Pricing"
    assert last_run(item)["plan_failed"] is True and last_run(item)["fallback"] is False


async def test_compose_needs_input_during_fallback_clears_the_request(db, cfg, fake_llm, monkeypatch):
    from pipeline.conversation import NeedsInput

    async def fake_compose(item, llm, settings=None):
        raise NeedsInput("q?", resume_status=Status.RESEARCHED)

    monkeypatch.setattr(revise_mod, "compose", fake_compose)
    i = await seed(db, request=rq.new_request(origin="skip", surface="telegram"))
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert item.status is Status.NEEDS_INPUT and item.revision_request is None


async def test_vague_note_asks_the_operator_and_changes_nothing(db, cfg, fake_llm, notices):
    i = await seed(db, request=req("make it better"))
    fake_llm.queue({"asks": ["unspecific"], "global": True, "actions": []})
    await run(db, fake_llm, cfg, notify=notices)
    item = await db.get_item(i)
    assert item.status is Status.AWAITING_APPROVAL  # D270: never a rebuild
    assert item.slides == DECK and item.caption == CAPTION and item.revision_request is None
    assert last_run(item)["ask_operator"] is True and last_run(item)["actions"] == []
    assert notices.kinds() == [NoticeKind.ASK_OPERATOR]


# --------------------------------------------------------- infrastructure faults


@pytest.mark.parametrize("exc", [Retryforever("down"), ConfigFault("bad model")])
async def test_infra_fault_defers_with_the_request_intact_and_one_notice(db, cfg, fake_llm, notices, exc):
    original = req("shorten slide 3")
    i = await seed(db, request=original)
    for _ in range(3):  # three worker retries
        fake_llm.queue(exc)
        await db.clear_backoff(i)
        await run(db, fake_llm, cfg, notify=notices)
        item = await db.get_item(i)
        assert item.status is Status.REVISION_REQUESTED and item.attempts == 0
    assert {k: v for k, v in item.revision_request.items() if k != "notified"} == original
    assert item.revision_request["notified"] is True
    assert notices.kinds() == [NoticeKind.DEFERRED]  # once per request, not per retry
    assert item.slides == DECK and item.revision is None


async def test_engines_blocked_defers_with_a_long_retry_and_remembers_since(db, cfg, fake_llm, monkeypatch):
    async def blocked(*a, **k):
        raise EnginesBlocked("captcha")

    monkeypatch.setattr(fetch_mod, "research", blocked)
    i = await seed(db, request=req("look up the latest benchmark and tell me"))
    fake_llm.queue(plan(["look up the benchmark"], act("research", query="benchmark")))
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert item.status is Status.REVISION_REQUESTED
    assert item.revision_request["blocked_since"] and item.revision_request["origin"] == "operator"
    assert item.next_attempt_at is not None and item.slides == DECK


async def test_engines_blocked_past_the_cap_drops_search_actions_and_runs_the_rest(
        db, cfg, fake_llm, monkeypatch):
    async def never(*a, **k):  # must not be called once the cap is hit
        raise AssertionError("search ran past the cap")

    monkeypatch.setattr(fetch_mod, "research", never)
    old = "2000-01-01T00:00:00+00:00"
    i = await seed(db, request=req("find the latest news and shorten slide 3", blocked_since=old))
    fake_llm.queue_each([
        plan(["find the latest news", "shorten slide 3"],
             act("research", ask=0, query="latest news"),
             act("edit_slide", ask=1, slide=3, instruction="shorten")),
        point(bullets=["Tiles held up"]),
        JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert item.status is Status.COMPOSED and item.slides[2]["bullets"] == ["Tiles held up"]
    rows = {(a["op"], a["outcome"], a["reason"]) for a in last_run(item)["actions"]}
    assert rows == {("research", "not_found", "search_blocked"), ("edit_slide", "applied", "")}
    assert last_run(item)["check"] == "partial"


async def test_cap_zero_gives_up_on_the_first_deferral(db, cfg, fake_llm, monkeypatch):
    zero = replace(cfg, revise_engines_blocked_max_s=0)
    monkeypatch.setattr(fetch_mod, "research", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    i = await seed(db, request=req("find the latest news about it", blocked_since=rq.now_iso()))
    fake_llm.queue(plan(["find news"], act("research", query="news")))
    await run(db, fake_llm, zero)
    item = await db.get_item(i)
    assert last_run(item)["actions"][0]["reason"] == "search_blocked"


# ----------------------------------------------------------------- the rounds


async def test_round_two_replans_once_with_feedback_and_can_succeed(db, cfg, fake_llm):
    i = await seed(db, request=req("slide 3 shorter"))
    same = point()
    fake_llm.queue_each([
        plan(["shorten slide 3"], act("edit_slide", slide=3, instruction="shorten")),
        same,  # round 1: the model returns the slide unchanged -> UNCHANGED
        plan(["shorten slide 3"], act("edit_slide", slide=3, instruction="cut the second bullet")),
        point(bullets=["Tiles held up"]),
        JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert roles(fake_llm) == ["good", "good", "good", "good", "cheap"]
    feedback_prompt = fake_llm.calls[2].user
    assert "FEEDBACK" in feedback_prompt and "edit_slide slide 3: unchanged" in feedback_prompt
    assert item.slides[2]["bullets"] == ["Tiles held up"]
    run_ = last_run(item)
    assert run_["rounds"] == 2 and run_["check"] == "satisfied"
    assert [a["outcome"] for a in run_["actions"]] == ["unchanged", "applied"]
    assert len(run_["asks"]) == 1  # the same ask, not a duplicate


async def test_never_more_than_two_rounds_and_never_an_automatic_recompose(db, cfg, fake_llm, monkeypatch):
    async def no_compose(*a, **k):
        raise AssertionError("an unsatisfied note must not become a full recompose (D253)")

    monkeypatch.setattr(revise_mod, "compose", no_compose)
    i = await seed(db, request=req("slide 3 shorter"))
    same = point()
    fake_llm.queue_each([
        plan(["shorten slide 3"], act("edit_slide", slide=3, instruction="shorten")), same,
        plan(["shorten slide 3"], act("edit_slide", slide=3, instruction="shorten more")), same,
    ])
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert len(fake_llm.calls) == 4  # 2 plans + 2 edits: no third round, no judge (nothing changed)
    assert last_run(item)["rounds"] == 2 and last_run(item)["check"] == "unsatisfied"
    assert item.status is Status.RENDERED and item.slides == DECK


async def test_no_replan_once_the_numbering_has_shifted(db, cfg, fake_llm):
    """Round 2 would read 'slide 5' against a deck whose slide 5 is no longer the operator's."""
    i = await seed(db, request=req("remove slide 2 and shorten slide 5"))
    fake_llm.queue(plan(["shorten slide 5"], act("edit_slide", ask=0, slide=5, instruction="shorten")))
    await run(db, fake_llm, cfg)
    token = rq.pending_confirm((await db.get_item(i)).revision_request)[rq.P_TOKEN]
    await rq.confirm_revision(db, i, True, token)
    fake_llm.queue(takeaway_same := {"type": "takeaway", "headline": "So what", "sub": "It holds up."})
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert last_run(item)["rounds"] == 1  # unchanged edit was retryable, but the numbering moved
    assert len(item.slides) == len(DECK) - 1
    del takeaway_same


# --------------------------------------------------------------- rule 15 / 17


async def test_unusable_model_output_costs_the_action_not_the_item_and_leaks_nothing(db, cfg, fake_llm):
    secret = "SECRET-PROMPT-ECHO-9f3a"
    i = await seed(db, request=req("slide 3 shorter"))
    fake_llm.queue_each([
        plan(["shorten slide 3"], act("edit_slide", slide=3, instruction="shorten")),
        BadCompletion(secret),
        plan([]), plan([]),  # round 2 offers a replan; the planner has nothing (twice: retry)
    ])
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert item.status is not Status.FAILED and item.slides == DECK
    assert last_run(item)["actions"][0]["reason"] == "model_unusable"
    dump = json.dumps(item.revision) + json.dumps([e["detail"] for e in await db.events_for(i)])
    assert secret not in dump and (item.last_error or "") == ""


async def test_integrity_rejection_text_never_reaches_the_record(db, cfg, fake_llm):
    i = await seed(db, request=req("slide 3 shorter"))
    bad = point(bullets=["Tiles held up", "9,999 hours of runs"])
    fake_llm.queue_each([
        plan(["shorten slide 3"], act("edit_slide", slide=3, instruction="shorten")),
        bad, bad, plan([]), plan([]),
    ])
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    row = last_run(item)["actions"][0]
    assert row["outcome"] == "rejected_integrity" and row["reason"] == ""
    assert "9999" not in json.dumps(item.revision)


async def test_run_history_is_capped_and_the_drop_is_logged(db, cfg, fake_llm, caplog):
    from pipeline.revision.model import REVISION_RUNS_KEPT
    from pipeline.revision.record import append_run

    revision = None
    caplog.set_level("INFO")
    for n in range(REVISION_RUNS_KEPT + 2):
        revision = append_run(revision, {"n": n})
    assert [r["n"] for r in revision["runs"]] == list(range(2, REVISION_RUNS_KEPT + 2))
    assert revision["v"] == 1 and caplog.text.count("dropping 1 oldest run(s)") == 2


async def test_verification_is_rewritten_only_when_the_deck_changed(db, settings, fake_llm):
    on = replace(settings, verify_enabled=True)
    i = await seed(db, request=req("slide 3 shorter"), verification={"status": "ok", "stale": True})
    fake_llm.queue_each([
        plan(["shorten slide 3"], act("edit_slide", slide=3, instruction="shorten")),
        point(bullets=["Tiles held up"]), JUDGE_OK,
    ])
    fake_llm.queue_verify({"claims": []})
    await run(db, fake_llm, on)
    item = await db.get_item(i)
    assert item.verification and item.verification.get("stale") is None  # a new record
    assert len(fake_llm.verify_calls) == 1


def test_executor_table_covers_the_whole_registry():
    from pipeline.revision.model import ACTION_SPECS

    assert set(execute_mod._HANDLERS) == set(ACTION_SPECS)
    assert {o.value for o in Op} == set(ACTION_SPECS)


async def test_flag_off_then_compose_is_the_legacy_regen_byte_for_byte(db, cfg):
    """The unchanged-path test: with REVISE_ENABLED=false the whole route
    (REVISION_REQUESTED -> SYNTHESIZED -> compose) yields exactly what the legacy
    regen produced: compose() on the same item with the note as ``regen_note``."""
    from pipeline.stages.compose import compose
    from tests.conftest import FakeLLM
    from tests.stages.test_compose import _doc

    off = replace(cfg, revise_enabled=False)
    i = await seed(db, request=req("make it punchier"))
    snapshot = await db.get_item(i)
    llm = FakeLLM()
    llm.queue(_doc())
    registry = {
        Status.REVISION_REQUESTED: (partial_stage(revise, llm=llm, http=FakeHTTP(), settings=off, db=db),
                                    Status.COMPOSED),
        Status.SYNTHESIZED: (partial_stage(compose, llm=llm, settings=off), Status.COMPOSED),
    }
    w = Worker(db, registry)
    await w.tick()
    await w.tick()
    via_stage = await db.get_item(i)

    legacy_llm = FakeLLM()
    legacy_llm.queue(_doc())
    legacy = await compose(replace(snapshot, status=Status.SYNTHESIZED, regen_note="make it punchier",
                                   revision_request=None), legacy_llm, off)
    assert via_stage.status is Status.COMPOSED
    assert (via_stage.slides, via_stage.caption, via_stage.theme) == (
        legacy["slides"], legacy["caption"], legacy["theme"])
    assert via_stage.regen_note is None and via_stage.revision is None  # no agentic trace
    import re

    def prompts(client):  # the per-call data-block nonce is random by design (D211)
        return [re.sub(r"DATA-[0-9a-f]+", "DATA-N", c.user) for c in client.calls]

    assert prompts(llm) == prompts(legacy_llm)  # the same prompt


async def test_an_added_slide_follows_its_anchor_wherever_a_reorder_moved_it(db, cfg, fake_llm):
    i = await seed(db, request=req("swap slides 2 and 4, and add a new slide after slide 2 about the lab"))
    fake_llm.queue_each([
        plan(["add a slide about the lab"], act("add_slide", slide=2, instruction="a slide about the lab")),
        {"type": "point", "headline": "Inside the lab", "bullets": ["Tiles sat in the plasma"]},
        JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    # "The test" (old slide 2) moved to 4th place; the new slide stays right behind it.
    assert [s["headline"] for s in item.slides] == [
        "Tungsten tiles survive plasma", "Pricing", "The result", "The test", "Inside the lab",
        "So what", "Sources", "Follow for more"]


async def test_a_failed_add_slide_inserts_nothing_and_costs_only_itself(db, cfg, fake_llm):
    i = await seed(db, request=req("add a new slide after slide 2 about the lab"))
    fake_llm.queue_each([
        plan(["add a slide"], act("add_slide", slide=2, instruction="about the lab")),
        BadCompletion("garbage"), plan([]), plan([]),
    ])
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert item.slides == DECK and item.status is Status.RENDERED
    assert [(a["op"], a["outcome"]) for a in last_run(item)["actions"]] == [("add_slide", "unchanged")]


async def test_search_ops_are_all_skipped_past_the_cap_including_images(db, cfg, fake_llm, monkeypatch):
    async def never(*a, **k):
        raise AssertionError("searched")

    for name in ("research", "fix_fact", "replace_image"):
        monkeypatch.setattr(fetch_mod, name, never)
    i = await seed(db, request=req("a clearer photo on slide 3, check the number on slide 4",
                                   blocked_since="2000-01-01T00:00:00+00:00"))
    fake_llm.queue_each([
        plan(["clearer photo", "check the figure"],
             act("replace_image", ask=0, slide=3, query="tile photo"),
             act("fix_fact", ask=1, slide=4, query="tile price")),
    ])
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert {(a["op"], a["reason"]) for a in last_run(item)["actions"]} == {
        ("replace_image", "search_blocked"), ("fix_fact", "search_blocked")}
    assert item.slides == DECK


# ------------------------------------------------ gaps found by the mutation sweep


async def test_a_deterministic_rules_op_that_changes_nothing_is_not_replanned(db, cfg, fake_llm):
    name = available_themes()[0]
    i = await seed(db, request=req(f"use the {name} theme"), theme_pin=name)  # already pinned
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert fake_llm.calls == []  # not even a replan: retrying a rule changes nothing
    assert last_run(item)["rounds"] == 1 and last_run(item)["actions"][0]["outcome"] == "unchanged"
    assert item.status is Status.RENDERED


async def test_round_two_never_replays_a_rules_op(db, cfg, fake_llm):
    name = available_themes()[0]
    i = await seed(db, request=req(f"use the {name} theme and shorten slide 3"), theme_pin=name)
    same = point()
    fake_llm.queue_each([
        plan(["shorten slide 3"], act("edit_slide", slide=3, instruction="shorten")), same,
        plan(["shorten slide 3"], act("edit_slide", slide=3, instruction="cut a bullet")),
        point(bullets=["Tiles held up"]), JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    # text runs before structural; the theme appears ONCE - round 2 is the model's asks only
    assert [a["op"] for a in last_run(item)["actions"]] == ["edit_slide", "set_theme", "edit_slide"]
    assert last_run(item)["rounds"] == 2


async def test_caption_only_without_pngs_still_re_renders(db, cfg, fake_llm):
    i = await seed(db, request=req("make the caption more casual"), rendered_paths=[])
    fake_llm.queue_each([
        plan(["casual caption"], act("rewrite_caption", instruction="casual")),
        {"caption": "Wild stuff.", "hashtags": ["fusion"]}, JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)
    assert (await db.get_item(i)).status is Status.COMPOSED


async def test_a_caption_change_gets_a_fresh_verification_record(db, settings, fake_llm):
    on = replace(settings, verify_enabled=True)
    i = await seed(db, request=req("make the caption more casual"), verification={"status": "ok", "stale": True})
    fake_llm.queue_each([
        plan(["casual caption"], act("rewrite_caption", instruction="casual")),
        {"caption": "Wild stuff.", "hashtags": ["fusion"]}, JUDGE_OK,
    ])
    fake_llm.queue_verify({"claims": []})
    await run(db, fake_llm, on)
    item = await db.get_item(i)
    assert len(fake_llm.verify_calls) == 1 and item.verification.get("stale") is None


async def test_an_unchanged_deck_keeps_its_verification_record(db, settings, fake_llm):
    on = replace(settings, verify_enabled=True)
    old = {"status": "ok", "stale": True}
    name = available_themes()[0]
    i = await seed(db, request=req(f"use the {name} theme"), theme_pin=name, verification=old)
    await run(db, fake_llm, on)
    assert (await db.get_item(i)).verification == old and fake_llm.verify_calls == []


async def test_a_skip_request_is_a_skip_even_if_a_note_is_attached(db, cfg, fake_llm, monkeypatch):
    seen = {}

    async def fake_compose(item, llm, settings=None):
        seen["note"] = item.regen_note
        return {"slides": copy.deepcopy(DECK), "caption": "c", "theme": "signal", "regen_note": None,
                "verification": None}

    monkeypatch.setattr(revise_mod, "compose", fake_compose)
    raw = {**req("x"), "origin": "skip"}  # a hand-built / legacy row: origin wins
    i = await seed(db, request=raw)
    await run(db, fake_llm, cfg)
    assert fake_llm.calls == [] and last_run(await db.get_item(i))["cause"] == "skip"


async def test_blocked_since_is_set_once_and_the_retry_is_spaced_out(db, cfg, fake_llm, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from pipeline.config import REVISE_ENGINES_BLOCKED_RETRY_S

    async def blocked(*a, **k):
        raise EnginesBlocked("captcha")

    monkeypatch.setattr(fetch_mod, "research", blocked)
    i = await seed(db, request=req("look up the latest benchmark and tell me"))
    firsts = []
    for _ in range(2):
        fake_llm.queue(plan(["look up the benchmark"], act("research", query="benchmark")))
        await db.clear_backoff(i)
        await run(db, fake_llm, cfg)
        item = await db.get_item(i)
        firsts.append(item.revision_request["blocked_since"])
    assert firsts[0] == firsts[1]
    due = datetime.fromisoformat(item.next_attempt_at)
    assert due - datetime.now(timezone.utc) > timedelta(seconds=REVISE_ENGINES_BLOCKED_RETRY_S - 30)


async def test_the_source_credit_is_reapplied_to_a_channel_caption_but_not_a_dm(db, cfg, fake_llm):
    credited = replace(cfg, source_credit="@aipost")
    reply = {"caption": "Wild stuff.", "hashtags": []}
    plan_ = plan(["casual caption"], act("rewrite_caption", instruction="casual"))
    i = await seed(db, request=req("make the caption more casual"))
    fake_llm.queue_each([plan_, reply, JUDGE_OK])
    await run(db, fake_llm, credited)
    assert "Source: @aipost" in (await db.get_item(i)).caption

    j = await db.insert_item(source="dm", source_chat_id=-2, source_msg_id=2, raw_text=RAW)
    await db.transition(j, Status.REVISION_REQUESTED, {
        "slides": copy.deepcopy(DECK), "caption": CAPTION, "research": copy.deepcopy(RESEARCH),
        "rendered_paths": list(RENDERED), "revision_request": req("make the caption more casual")})
    fake_llm.queue_each([plan_, reply, JUDGE_OK])
    await run(db, fake_llm, credited)
    assert "Source:" not in (await db.get_item(j)).caption


async def test_an_executor_that_touches_an_untouched_slide_is_undone(db, cfg, fake_llm, monkeypatch):
    """D251: the invariant is enforced in code. A buggy structural executor that also
    rewrites the add_slide ANCHOR (which no action targets) is put right."""
    real = execute_mod.structural.insert_slide

    def buggy(deck, action, new_slide, **kw):
        step = real(deck, action, new_slide, **kw)
        slides = list(step.deck.slides)
        pos = step.deck.position(action.slide_id)
        slides[pos] = {**slides[pos], "headline": "VANDALISED"}
        return replace(step, deck=replace(step.deck, slides=tuple(slides)))

    monkeypatch.setattr(execute_mod.structural, "insert_slide", buggy)
    i = await seed(db, request=req("add a new slide after slide 2 about the lab"))
    fake_llm.queue_each([
        plan(["add a slide"], act("add_slide", slide=2, instruction="about the lab")),
        {"type": "point", "headline": "Inside the lab", "bullets": ["Tiles sat in the plasma"]}, JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert item.slides[1] == DECK[1] and "VANDALISED" not in json.dumps(item.slides)
    assert any(h["headline"] == "Inside the lab" for h in item.slides)


async def test_new_findings_from_fix_fact_reach_the_later_edit(db, cfg, fake_llm, monkeypatch):
    note = {"claim": "Launch is set for March", "sources": ["https://b.example/2"], "origin": "revision"}

    async def fake_fix(llm, item, deck, action, raw_note, settings, http):
        return None, [note], Outcome(action.op, OutcomeCode.UNCHANGED, action.slide, action.ask)

    monkeypatch.setattr(fetch_mod, "fix_fact", fake_fix)
    i = await seed(db, request=req("check the figure on slide 4 and tighten slide 2"))
    fake_llm.queue_each([
        plan(["check slide 4", "tighten slide 2"],
             act("fix_fact", ask=0, slide=4, query="tile price"),
             act("edit_slide", ask=1, slide=2, instruction="tighten")),
        point(headline="The test", bullets=["Tiles sat in plasma"]), JUDGE_OK,
        plan([]), plan([]),  # the fix_fact came out unchanged: round 2 is offered, the planner has nothing
    ])
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert "Launch is set for March" in fake_llm.calls[1].user
    assert item.research[-1]["claim"] == "Launch is set for March"


async def test_a_figure_is_backed_by_the_operators_raw_note_never_by_the_planner(db, cfg, fake_llm):
    """D276, end to end: 2026 is in the operator's words; 2031 is only the planner's."""
    i = await seed(db, request=req("put (2026) in the headline of slide 3"))
    ok = point(headline="The result (2026)")
    fake_llm.queue_each([
        plan(["year in slide 3 headline"], act("edit_slide", slide=3, instruction="add the year to the headline")),
        ok, JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)
    assert (await db.get_item(i)).slides[2]["headline"] == "The result (2026)"

    j = await db.insert_item(source="channel", source_chat_id=-3, source_msg_id=3, raw_text=RAW)
    await db.transition(j, Status.REVISION_REQUESTED, {
        "slides": copy.deepcopy(DECK), "caption": CAPTION, "research": copy.deepcopy(RESEARCH),
        "rendered_paths": list(RENDERED), "revision_request": req("make slide 3 more current")})
    bad = point(headline="The result (2031)")
    fake_llm.queue_each([
        plan(["make slide 3 current"], act("edit_slide", slide=3, instruction="mention 2031")),
        bad, bad, plan([]), plan([]),
    ])
    await run(db, fake_llm, cfg)
    item = await db.get_item(j)
    assert item.slides[2]["headline"] == "The result" and last_run(item)["actions"][0]["outcome"] == "rejected_integrity"


async def test_an_unmet_rules_op_next_to_a_satisfied_model_ask_does_not_trigger_a_second_planner_call(
        db, cfg, fake_llm):
    name = available_themes()[0]
    i = await seed(db, request=req(f"use the {name} theme and shorten slide 3"), theme_pin=name)
    fake_llm.queue_each([
        plan(["shorten slide 3"], act("edit_slide", slide=3, instruction="shorten")),
        point(bullets=["Tiles held up"]), JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)  # a replan would hit FakeLLM's empty queue and fail the stage
    item = await db.get_item(i)
    assert last_run(item)["rounds"] == 1 and roles(fake_llm) == ["good", "good", "cheap"]


async def test_an_added_slide_may_carry_a_figure_only_the_operators_note_states(db, cfg, fake_llm):
    i = await seed(db, request=req("add a new slide after slide 2 about the 2026 launch"))
    fake_llm.queue_each([
        plan(["add a launch slide"], act("add_slide", slide=2, instruction="about the launch")),
        {"type": "point", "headline": "Launch in 2026", "bullets": ["Tiles sat in the plasma"]}, JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)
    assert "Launch in 2026" in [s["headline"] for s in (await db.get_item(i)).slides]


async def test_a_note_longer_than_the_parse_cap_is_flagged_and_never_reads_satisfied(db, cfg, fake_llm):
    from pipeline.revision.model import MAX_NOTE_CHARS

    long_note = "shorten slide 3" + " " * (MAX_NOTE_CHARS + 50) + "thanks"
    r = rq.new_request(origin="operator", note=long_note, surface="dashboard")
    assert len(r["note"]) > MAX_NOTE_CHARS  # still visibly longer than the parse cap
    i = await seed(db, request=r)
    fake_llm.queue_each([
        plan(["shorten slide 3"], act("edit_slide", slide=3, instruction="shorten")),
        point(bullets=["Tiles held up"]), JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)
    run_ = last_run(await db.get_item(i))
    assert run_["clipped"] is True and run_["check"] == "partial" and "note_clipped" in run_["check_reasons"]
    assert len(run_["note"]) == MAX_NOTE_CHARS  # the record keeps only what was read


async def test_overflow_requests_use_the_legacy_sentence_until_t304(db, cfg, fake_llm, monkeypatch):
    seen = {}

    async def fake_compose(item, llm, settings=None):
        seen["note"] = item.regen_note
        return {"slides": copy.deepcopy(DECK), "caption": "c", "theme": "signal", "regen_note": None,
                "verification": None}

    monkeypatch.setattr(revise_mod, "compose", fake_compose)
    r = rq.new_request(origin="overflow", slide=3, reason="the point content is too long", surface="worker")
    i = await seed(db, request=r)
    await run(db, fake_llm, cfg)
    assert seen["note"] == "Slide 3 did not fit: the point content is too long. Shorten it."
    assert last_run(await db.get_item(i))["cause"] == "overflow"
    j = await seed(db, request=r)  # flag off: the same sentence as regen_note, as the worker wrote it
    await run(db, fake_llm, replace(cfg, revise_enabled=False))
    assert (await db.get_item(j)).regen_note == seen["note"]


async def test_a_reworded_retry_satisfies_the_original_ask_not_a_new_one(db, cfg, fake_llm):
    i = await seed(db, request=req("slide 3 shorter"))
    fake_llm.queue_each([
        plan(["shorten slide 3"], act("edit_slide", slide=3, instruction="shorten")), point(),
        plan(["cut slide 3 down"], act("edit_slide", ask=0, slide=3, instruction="cut it")),
        point(bullets=["Tiles held up"]), JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)
    run_ = last_run(await db.get_item(i))
    assert run_["asks"] == ["shorten slide 3"]
    assert [(a["ask"], a["outcome"]) for a in run_["actions"]] == [(0, "unchanged"), (0, "applied")]
    assert run_["check"] == "satisfied" and run_["unsatisfied"] == []


async def test_round_two_sees_the_claims_round_one_found(db, cfg, fake_llm, monkeypatch):
    note = {"claim": "Launch is set for March", "sources": ["https://b.example/2"], "origin": "revision"}

    async def fake_research(llm, item, action, settings, http):
        return [note], Outcome(action.op, OutcomeCode.APPLIED, 0, action.ask)

    monkeypatch.setattr(fetch_mod, "research", fake_research)
    await seed(db, request=req("find the latest launch news and shorten slide 3"))
    fake_llm.queue_each([
        plan(["find the latest launch news", "shorten slide 3"],
             act("research", ask=0, query="launch news"),
             act("edit_slide", ask=1, slide=3, instruction="shorten")),
        point(),  # the edit comes back unchanged; no slide changed, so no judge call
        plan(["shorten slide 3"], act("edit_slide", ask=1, slide=3, instruction="cut it")),
        point(bullets=["Tiles held up"]), JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)
    assert roles(fake_llm) == ["good", "good", "good", "good", "cheap"]
    replan_prompt = fake_llm.calls[2].user
    assert "FEEDBACK" in replan_prompt and "Launch is set for March" in replan_prompt


async def test_planner_asks_that_got_no_action_are_reported_not_lost(db, cfg, fake_llm):
    i = await seed(db, request=req("shorten slide 3 and make it spicier"))
    fake_llm.queue_each([
        plan(["shorten slide 3", "make it spicier"], act("edit_slide", ask=0, slide=3, instruction="shorten")),
        point(bullets=["Tiles held up"]), JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)
    assert last_run(await db.get_item(i))["unaddressed"] == ["make it spicier"]  # D280/D281


async def test_a_replan_that_names_one_failed_ask_exactly_credits_that_ask(db, cfg, fake_llm):
    i = await seed(db, request=req("shorten slide 3 and shorten slide 4"))
    pricing = {"type": "point", "headline": "Pricing", "bullets": ["Tiles are costly", "Supply is thin"]}
    fake_llm.queue_each([
        plan(["shorten slide 3", "shorten slide 4"],
             act("edit_slide", ask=0, slide=3, instruction="shorten"),
             act("edit_slide", ask=1, slide=4, instruction="shorten")),
        point(), pricing,  # both come back unchanged
        plan(["shorten slide 4"], act("edit_slide", ask=0, slide=4, instruction="cut it")),
        {**pricing, "bullets": ["Tiles are costly"]}, JUDGE_OK,
    ])
    await run(db, fake_llm, cfg)
    run_ = last_run(await db.get_item(i))
    assert run_["asks"] == ["shorten slide 3", "shorten slide 4"]
    assert [(a["slide"], a["ask"], a["outcome"]) for a in run_["actions"]] == [
        (3, 0, "unchanged"), (4, 1, "unchanged"), (4, 1, "applied")]
    assert run_["unsatisfied"] == [0] and run_["check"] == "partial"


# ------------------------------------------------------------ T301 bounce 1


async def test_blocked_since_is_dropped_when_the_next_outage_is_something_else(db, cfg, fake_llm):
    i = await seed(db, request=req("shorten slide 3", blocked_since=rq.now_iso()))
    fake_llm.queue(ConfigFault("bad model"))
    await run(db, fake_llm, cfg)
    assert "blocked_since" not in (await db.get_item(i)).revision_request


async def test_blocked_since_is_dropped_once_search_succeeded(db, cfg, fake_llm, monkeypatch):
    async def fine(llm, item, action, settings, http):
        return [], Outcome(action.op, OutcomeCode.NOT_FOUND, 0, action.ask, "no_results")

    monkeypatch.setattr(fetch_mod, "research", fine)
    i = await seed(db, request=req("look up the latest news and shorten slide 3", blocked_since=rq.now_iso()))
    fake_llm.queue_each([
        plan(["look up the latest news", "shorten slide 3"],
             act("research", ask=0, query="news"), act("edit_slide", ask=1, slide=3, instruction="shorten")),
        point(bullets=["Tiles held up"]),
        ConfigFault("judge down"),  # a different outage after search worked
    ])
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    assert item.status is Status.REVISION_REQUESTED and "blocked_since" not in item.revision_request


async def test_flag_off_after_a_no_returns_to_the_gate_instead_of_composing(db, cfg, fake_llm):
    i = await seed(db, request=req("remove slide 4 and tighten slide 2"))
    fake_llm.queue(plan(["tighten slide 2"], act("edit_slide", slide=2, instruction="tighten")))
    await run(db, fake_llm, cfg)
    token = rq.pending_confirm((await db.get_item(i)).revision_request)[rq.P_TOKEN]
    await rq.confirm_revision(db, i, False, token)
    await run(db, fake_llm, replace(cfg, revise_enabled=False))
    item = await db.get_item(i)
    assert item.status is Status.AWAITING_APPROVAL and item.regen_note is None
    assert item.slides == DECK and last_run(item)["actions"][0]["outcome"] == "declined"


async def test_flag_off_after_a_yes_is_the_legacy_regen(db, cfg, fake_llm):
    i = await seed(db, request=req("remove slide 4"))
    await run(db, fake_llm, cfg)
    token = rq.pending_confirm((await db.get_item(i)).revision_request)[rq.P_TOKEN]
    await rq.confirm_revision(db, i, True, token)
    await run(db, fake_llm, replace(cfg, revise_enabled=False))
    item = await db.get_item(i)
    assert item.status is Status.SYNTHESIZED and item.regen_note == "remove slide 4"


async def test_only_an_exact_true_answer_runs_destructive_actions(db, cfg, fake_llm):
    i = await seed(db, request=req("remove slide 4 and tighten slide 2"))
    fake_llm.queue(plan(["tighten slide 2"], act("edit_slide", slide=2, instruction="tighten")))
    await run(db, fake_llm, cfg)
    item = await db.get_item(i)
    bad = {**item.revision_request}
    bad["pending_confirm"] = {**bad["pending_confirm"], "answer": "yes"}  # junk, not True
    await db.transition(i, Status.REVISION_REQUESTED, {"revision_request": bad})
    fake_llm.queue_each([point(headline="The test", bullets=["Tiles sat in plasma"]), JUDGE_OK])
    await run(db, fake_llm, cfg)
    assert "Pricing" in [s["headline"] for s in (await db.get_item(i)).slides]


async def test_the_note_given_to_compose_is_clipped_to_the_parse_cap(db, cfg, fake_llm, monkeypatch):
    from pipeline.revision.model import MAX_NOTE_CHARS

    seen = {}

    async def fake_compose(item, llm, settings=None):
        seen["n"] = len(item.regen_note)
        return {"slides": copy.deepcopy(DECK), "caption": "c", "theme": "signal", "regen_note": None,
                "verification": None}

    monkeypatch.setattr(revise_mod, "compose", fake_compose)
    r = {**req("x"), "origin": "overflow", "note": "y" * (MAX_NOTE_CHARS + 500)}
    await seed(db, request=r)
    await run(db, fake_llm, cfg)
    assert seen["n"] == MAX_NOTE_CHARS
    j = await seed(db, request=req("z" * (MAX_NOTE_CHARS + 500)))
    await run(db, fake_llm, replace(cfg, revise_enabled=False))
    assert len((await db.get_item(j)).regen_note) == MAX_NOTE_CHARS


async def test_a_later_blockage_starts_a_fresh_clock_after_search_worked(db, cfg, fake_llm, monkeypatch):
    from datetime import datetime, timedelta, timezone

    calls = {"n": 0}

    async def flaky(llm, item, action, settings, http):
        calls["n"] += 1
        if calls["n"] > 1:
            raise EnginesBlocked("captcha")
        return [], Outcome(action.op, OutcomeCode.NOT_FOUND, 0, action.ask, "no_results")

    monkeypatch.setattr(fetch_mod, "research", flaky)
    old = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    i = await seed(db, request=req("look up the latest news and shorten slide 3", blocked_since=old))
    fake_llm.queue_each([
        plan(["look up the latest news", "shorten slide 3"],
             act("research", ask=0, query="news"), act("edit_slide", ask=1, slide=3, instruction="shorten")),
        point(),  # unchanged -> retryable
        plan(["shorten slide 3", "look up the latest news"],
             act("edit_slide", ask=0, slide=3, instruction="cut"), act("research", ask=1, query="news again")),
    ])
    await run(db, fake_llm, cfg)
    since = (await db.get_item(i)).revision_request["blocked_since"]
    assert datetime.fromisoformat(since) > datetime.fromisoformat(old) + timedelta(seconds=30)
