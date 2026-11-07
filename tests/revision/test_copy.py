"""T302 / T303: the operator's wording for agentic regeneration, as a pure module.

The completeness tests are the point (D272): a new ``OutcomeCode``, ``Reason``, ``Op``,
``ConfirmResult`` or executor ``REASON_*`` constant with no sentence fails here, instead
of reaching the operator as a blank line or a raw enum value. The snapshot tests pin the
wording to the spec's mock-ups (docs/design/revision-copy.md section 6).
"""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pipeline.limits import TELEGRAM_CALLBACK_ANSWER_LIMIT
from pipeline.revision import copy as rc
from pipeline.revision import execute, fetch, structural
from pipeline.revision.check import Status
from pipeline.revision.model import (
    MAX_ACTIONS,
    MAX_ASKS,
    MAX_NOTE_CHARS,
    Action,
    Dropped,
    Op,
    Outcome,
    OutcomeCode,
    Reason,
)
from pipeline.revision.record import new_run
from pipeline.revision.request import ConfirmResult

C = OutcomeCode
DECK = [
    {"type": "hook", "headline": "Blackwell B300 is here"},
    {"type": "point", "headline": "Memory is the bottleneck"},
    {"type": "photo", "headline": "The die up close"},
    {"type": "point", "headline": "Pricing leaks"},
    {"type": "kpi", "headline": "40% faster inference"},
    {"type": "takeaway", "headline": "What to watch"},
    {"type": "sources"},
    {"type": "follow"},
]
THEMES = ["signal", "riso"]
GLYPHS = "✓✗?–"


def req(note: str | None = "x", origin: str = "operator") -> dict:
    return {"origin": origin, "surface": "telegram", "note": note}


def run(note="x", **kw) -> dict:
    return new_run(req(note), at="t", **kw)


def block(r, deck=DECK, **kw) -> str:
    return rc.render_changes(r, slides=deck, themes=THEMES, **kw)


def lines(text: str) -> list[str]:
    return text.split("\n")


# ===================================================================== completeness


def test_every_outcome_code_resolves_for_every_op_and_every_reason():
    """The cross product, not a sample: nothing may raise or come back without a row."""
    reasons = [None, "", "garbage free text", *rc.EXECUTOR_REASONS, *[r.value for r in Reason]]
    for code, op, reason in itertools.product([*OutcomeCode, "bogus"], [*Op, "bogus", None], reasons):
        row = rc.resolve(code, op, reason)
        assert isinstance(row, rc.Row)
        if row.mark is not rc.Mark.HIDE:
            assert row.text.strip(), (code, op, reason)


def test_every_outcome_code_has_a_default_row():
    for code in OutcomeCode:
        assert (code, rc.CODE_ANY, None) in rc.OUTCOME_COPY, f"{code} has no default line"


def test_every_op_has_its_own_applied_line():
    for op in Op:
        assert (C.APPLIED, op, None) in rc.OUTCOME_COPY, f"no applied line for {op}"
    for op in Op:  # and every op can be named in an "Also doing" list
        assert op.value in rc._ALSO_DOING, op


def test_every_reason_has_copy():
    assert set(rc.REASON_COPY) == set(Reason)


def test_every_executor_reason_constant_has_a_sentence():
    """Scan the executors rather than trust a list: a new REASON_* / WARN_* constant that
    the copy module does not know fails here, not in front of the operator."""
    declared = {
        value
        for module in (execute, fetch, structural)
        for name, value in vars(module).items()
        if (name.startswith("REASON_") or name.startswith("WARN_")) and isinstance(value, str)
    }
    mapped = {k[2] for k in rc.OUTCOME_COPY if k[2]} | {r.value for r in rc.REASON_COPY}
    assert declared - rc.EXECUTOR_REASONS - {r.value for r in Reason} == set(), "register it in copy.EXECUTOR_REASONS"
    assert rc.EXECUTOR_REASONS <= mapped, rc.EXECUTOR_REASONS - mapped


def test_the_named_fetch_and_structural_codes_are_all_mapped():
    """The codes docs/revision-api.md says copy must map."""
    named = {"search_blocked", "model_unusable", "global_op", "no_results", "not_answered",
             "source_unverified", "no_candidates", "no_usable_image", "same_image", "image_unchecked",
             "min_slides", "max_slides", "hook_slide", "bodyless", "cta_dropped", "closing_clamp",
             "already_removed", "text_only_theme_with_photo"}
    assert named <= rc.EXECUTOR_REASONS


def test_every_confirm_result_has_a_toast_within_the_platform_cap():
    assert set(rc.CONFIRM_TOASTS) == set(ConfirmResult)
    for text in [*rc.CONFIRM_TOASTS.values(), rc.REGEN_TOAST, rc.BUSY_CONFIRM_TOAST, rc.BUSY_REVISING_TOAST]:
        assert 0 < len(text) <= TELEGRAM_CALLBACK_ANSWER_LIMIT


def test_every_ask_cause_has_a_line():
    assert set(rc.ASK_CAUSE_LINES) == set(rc.AskCause)


def test_unknown_outcome_never_leaks_its_text():
    row = rc.resolve("exploded", "edit_slide", "Traceback: boom at /Users/x/y.py")
    assert "boom" not in row.text and "Traceback" not in row.text
    r = run(asks=["a"], actions=[Outcome("edit_slide", C.NOT_FOUND, 2, 0, "Traceback boom")], check="unsatisfied",
            unsatisfied=[0])
    assert "boom" not in block(r) and "Traceback" not in block(r)


# ===================================================================== markers by outcome


@pytest.mark.parametrize("op", [o for o in Op])
def test_applied_is_a_tick_for_every_op(op):
    r = run(asks=["a"], actions=[Outcome(op.value, C.APPLIED, 0 if op in (
        Op.REORDER, Op.SET_THEME, Op.REWRITE_CAPTION, Op.RESEARCH, Op.FULL_RECOMPOSE) else 2, 0)],
        check="satisfied", changed=[2], theme="riso")
    out = block(r)
    assert any(ln.startswith("✓ ") for ln in lines(out)), (op, out)
    assert not any(ln.startswith(("✗", "?")) for ln in lines(out) if "Couldn't check" not in ln)


@pytest.mark.parametrize("code,mark", [
    (C.NOT_FOUND, "✗"), (C.REJECTED_INTEGRITY, "✗"), (C.REFUSED_INVARIANT, "✗"), (C.UNAVAILABLE, "?"),
])
@pytest.mark.parametrize("op", [o for o in Op if o not in (Op.REORDER, Op.SET_THEME, Op.REWRITE_CAPTION, Op.RESEARCH,
                                                            Op.FULL_RECOMPOSE)])
def test_non_applied_slide_outcomes_carry_their_marker_and_say_so_in_words(code, mark, op):
    r = run(asks=["a"], actions=[Outcome(op.value, code, 2, 0)], check="unsatisfied", unsatisfied=[0])
    row = [ln for ln in lines(block(r)) if ln.startswith(("✓", "✗", "?", "–")) and "Checked" not in ln][0]
    assert row.startswith(mark + " Slide") or row.startswith(mark + " New slide"), row
    # the meaning survives without the glyph (AC-14)
    assert len(row) > 12 and any(w in row for w in (
        "kept", "not", "couldn't", "left", "unchanged", "same", "claims", "check", "found", "read"))


def test_unchanged_is_a_failure_for_slide_ops_and_neutral_for_a_theme_that_was_already_set():
    r = run(asks=["a"], actions=[Outcome("edit_slide", C.UNCHANGED, 3, 0)], check="unsatisfied", unsatisfied=[0])
    assert "✗ Slide 3 — came out the same; kept as is" in block(r)
    r = run(asks=["a"], actions=[Outcome("set_theme", C.UNCHANGED, 0, 0)], check="unsatisfied", unsatisfied=[0])
    assert "– Theme" in block(r) and "✗ Theme" not in block(r)


def test_a_duplicate_unchanged_row_under_a_satisfied_check_is_not_a_failure():
    """T301 tester: the planner emitted rewrite_caption twice; the second came out the same."""
    r = run(asks=["casual caption"], check="satisfied", caption_changed=True, actions=[
        Outcome("rewrite_caption", C.APPLIED, 0, 0), Outcome("rewrite_caption", C.UNCHANGED, 0, 0)])
    out = block(r)
    assert "✗" not in out and "came out the same" not in out
    assert "✓ Caption — rewritten" in out and "Checked against your note: done." in out


def test_an_unchanged_row_with_no_applied_twin_is_still_a_failure():
    """The suppression is only for the duplicate of something that DID run."""
    r = run(asks=["a", "b"], check="partial", unsatisfied=[1], changed=[2], actions=[
        Outcome("edit_slide", C.APPLIED, 2, 0), Outcome("edit_slide", C.UNCHANGED, 3, 1)])
    assert "✗ Slide 3 — came out the same; kept as is" in block(r)


def test_deferred_never_appears_in_a_preview():
    r = run(asks=["a"], actions=[Outcome("edit_slide", C.DEFERRED, 2, 0)], check="unsatisfied", unsatisfied=[0])
    out = block(r)
    assert out == "" or "deferred" not in out.lower()
    assert "Slide 2" not in out


def test_declined_is_neutral_and_not_counted_as_done_or_missed():
    r = run("remove slide 4 and tighten slide 2", asks=["remove slide 4", "tighten slide 2"], check="satisfied",
            changed=[2], actions=[Outcome("edit_slide", C.APPLIED, 2, 1), Outcome("remove_slide", C.DECLINED, 4, 0)])
    out = block(r)
    assert "– Slide 4: kept (you said no)" in out
    assert "Checked against your note: done." in out  # the declined ask is not counted


# ===================================================================== reasons (spec 5.5)


def drop(reason, op="edit_slide", slide=2):
    return Dropped(op, slide, C.DROPPED_CONFLICT, reason)


def test_every_reason_renders_something_sensible_or_is_hidden_on_purpose():
    shown = {Reason.MALFORMED, Reason.UNKNOWN_OP, Reason.MISSING_ARG, Reason.OUT_OF_RANGE, Reason.CLOSING_SLIDE,
             Reason.NO_IMAGE, Reason.BAD_THEME, Reason.BAD_ORDER, Reason.NOOP_REORDER, Reason.TARGET_REMOVED,
             Reason.RESEARCH_CAP, Reason.RECOMPOSE_NOT_ALONE, Reason.MAX_ACTIONS, Reason.MAX_ASKS}
    hidden = {Reason.DUPLICATE, Reason.NO_SLIDE_REFERENCE, Reason.NO_TRIGGER, Reason.THEME_NOT_NAMED,
              Reason.SCOPED_NOTE, Reason.RULES_ONLY, Reason.NEGATED, Reason.CONSUMED_BY_RULES}
    assert shown | hidden == set(Reason) and not shown & hidden
    for reason in Reason:
        r = run(asks=["a"], check="satisfied", changed=[2], actions=[Outcome("edit_slide", C.APPLIED, 2, 0)],
                dropped=[drop(reason)])
        out = block(r)
        extra = [ln for ln in lines(out)[1:] if ln.startswith(("✗", "–"))]
        assert bool(extra) == (reason in shown), (reason, out)
        assert reason.value not in out  # never the raw enum value


def test_the_ask_cap_does_not_state_the_action_cap():
    """D278: Reason.MAX_ASKS gets its own count, so no wrong number is ever stated."""
    asks = block(run(asks=["a"], check="satisfied", changed=[2], actions=[Outcome("edit_slide", C.APPLIED, 2, 0)],
                     dropped=[drop(Reason.MAX_ASKS)]))
    acts = block(run(asks=["a"], check="satisfied", changed=[2], actions=[Outcome("edit_slide", C.APPLIED, 2, 0)],
                     dropped=[drop(Reason.MAX_ACTIONS)]))
    assert MAX_ASKS != MAX_ACTIONS
    assert f"first {MAX_ASKS}." in asks and f"first {MAX_ACTIONS}" not in asks
    assert f"first {MAX_ACTIONS}." in acts and "separate requests" not in acts


def test_several_unclear_parts_are_one_line():
    r = run(asks=["a"], check="satisfied", changed=[2], actions=[Outcome("edit_slide", C.APPLIED, 2, 0)],
            dropped=[drop(Reason.MALFORMED), drop(Reason.UNKNOWN_OP), drop(Reason.MISSING_ARG)])
    assert sum("couldn't be turned into a clear edit" in ln for ln in lines(block(r))) == 1


def test_out_of_range_names_the_deck_size_it_was_planned_against():
    r = run(asks=["a"], check="satisfied", changed=[2], actions=[Outcome("edit_slide", C.APPLIED, 2, 0)],
            dropped=[drop(Reason.OUT_OF_RANGE, slide=12)])
    assert "✗ Slide 12 doesn't exist — the deck has 8 slides." in block(r)


def test_unaddressed_is_phrased_softly_never_as_not_done():
    """D281: model text that may over-report, so 'may have missed', and capped."""
    r = run(asks=["a"], check="satisfied", changed=[2], actions=[Outcome("edit_slide", C.APPLIED, 2, 0)],
            unaddressed=["add a pricing slide", "x" * 300])
    soft = [ln for ln in lines(block(r)) if "may have missed" in ln]
    assert len(soft) == 1 and soft[0].startswith("? I may have missed: \"add a pricing slide\"")
    assert "not done" not in soft[0].lower() and len(soft[0]) <= rc.CHANGE_LINE_MAX


# ===================================================================== headers and checks (golden)

NOTE_M1 = "slide 3 shorter and a better photo of the chip on slide 1"
NOTE_M2 = "shorten slide 3, check the 40% on slide 5, make the caption casual"

GOLDEN_APPLIED = f"""Revised per your note: "{NOTE_M1}"
✓ Slide 1 image — replaced
✓ Slide 3 — reworded
Checked against your note: all 2 done.
Unchanged: slides 2, 4–8"""

GOLDEN_PARTIAL = f"""Partly revised — "{NOTE_M2}"
✓ Slide 3 — reworded
✓ Caption — rewritten
✗ Slide 5 — corrected figure wasn't backed by a source; left as is
Checked against your note: 2 of 3 done. Not done: "verify the figure on slide 5".
Unchanged: slides 1–2, 4–8"""

GOLDEN_NOT_FOUND = """Couldn't do what you asked — "swap the photo on slide 3 for the B300 die shot"
✗ Slide 3 image — no photo found; kept the old one
Checked against your note: not done. Missing: "a B300 die photo on slide 3".
Nothing changed."""

GOLDEN_FALLBACK = """Rebuilt the whole deck with your note: "the whole thing is too technical"
I couldn't plan targeted edits, so I rebuilt it.
Every slide is new — compare before approving.
? Not checked against your note — read it through."""

GOLDEN_SKIP = """Fresh version (no note).
You asked for a fresh version.
Every slide is new — compare before approving."""

GOLDEN_UNCHECKED = """Revised, not verified — "tighten slide 2"
✓ Slide 2 — reworded
? Couldn't check this against your note — read the slides before approving.
Unchanged: slides 1, 3–8"""

GOLDEN_DECLINED = """Revised per your note: "remove slide 4 and tighten slide 2"
✓ Slide 2 — reworded
– Slide 4: kept (you said no)
Checked against your note: done.
Unchanged: slides 1, 3–8"""

GOLDEN_CLIPPED = f"""Revised, not verified — "tighten slide 2"
Your note was longer than {MAX_NOTE_CHARS} characters; I only read the start.
✓ Slide 2 — reworded
? Couldn't check this against your note — read the slides before approving.
Unchanged: slides 1, 3–8"""


def test_golden_applied():
    r = run(NOTE_M1, asks=["better chip photo on slide 1", "shorten slide 3"], check="satisfied", changed=[1, 3],
            actions=[Outcome("replace_image", C.APPLIED, 1, 0), Outcome("edit_slide", C.APPLIED, 3, 1)])
    assert block(r) == GOLDEN_APPLIED


def test_golden_partial():
    r = run(NOTE_M2, asks=["shorten slide 3", "verify the figure on slide 5", "casual caption"], check="partial",
            unsatisfied=[1], changed=[3], caption_changed=True, actions=[
                Outcome("edit_slide", C.APPLIED, 3, 0), Outcome("fix_fact", C.REJECTED_INTEGRITY, 5, 1),
                Outcome("rewrite_caption", C.APPLIED, 0, 2)])
    assert block(r) == GOLDEN_PARTIAL


def test_golden_not_found():
    r = run("swap the photo on slide 3 for the B300 die shot", asks=["a B300 die photo on slide 3"],
            check="unsatisfied", unsatisfied=[0],
            actions=[Outcome("replace_image", C.NOT_FOUND, 3, 0, "no_candidates")])
    assert block(r) == GOLDEN_NOT_FOUND


def test_golden_fallback_skip_and_overflow():
    assert block(run("the whole thing is too technical", fallback=True, cause="plan_failed")) == GOLDEN_FALLBACK
    skip = new_run(req(None, "skip"), at="t", fallback=True, cause="skip")
    assert block(skip) == GOLDEN_SKIP
    over = new_run(req(None, "overflow"), at="t", fallback=True, cause="overflow")
    assert lines(block(over))[0].startswith("A slide didn't fit")


def test_golden_unchecked_declined_clipped_deferred():
    one = dict(asks=["tighten slide 2"], changed=[2], actions=[Outcome("edit_slide", C.APPLIED, 2, 0)])
    assert block(run("tighten slide 2", check="unchecked", **one)) == GOLDEN_UNCHECKED
    assert block(run("tighten slide 2", check="satisfied", clipped=True, **one)) == GOLDEN_CLIPPED
    r = run("remove slide 4 and tighten slide 2", asks=["remove slide 4", "tighten slide 2"], check="satisfied",
            changed=[2], actions=[Outcome("edit_slide", C.APPLIED, 2, 1), Outcome("remove_slide", C.DECLINED, 4, 0)])
    assert block(r) == GOLDEN_DECLINED
    d = run("tighten slide 2", asks=["a"], check="unsatisfied", unsatisfied=[0],
            actions=[Outcome("edit_slide", C.DEFERRED, 2, 0)])
    assert block(d) == ""


def test_renumbering_uses_the_new_deck_and_says_so():
    deck = DECK[:1] + DECK[2:4] + [{"type": "point", "headline": "Pricing"}] + DECK[4:]
    r = run("remove slide 2 and add a pricing slide after 4", asks=["remove 2", "add pricing"], check="partial",
            unsatisfied=[1], removed=[2], added=[4], actions=[
                Outcome("remove_slide", C.APPLIED, 2, 0), Outcome("add_slide", C.APPLIED, 4, 1),
                Outcome("edit_slide", C.REFUSED_INVARIANT, 5, 1, "closing_clamp")])
    out = block(r, deck)
    assert "Slide numbers below are the new ones." in out
    assert "✓ Slide 2 (was) — removed" in out
    assert "✓ New slide after 3 — added" in out  # old 4 is now 3
    assert '✗ Slide 5 "40% faster inference"' in out  # old 5 -> new 5 (removed one, added one) + headline on ✗
    assert "Unchanged: slides 1–3, 5–8" in out


def test_a_reorder_keeps_the_numbers_of_the_note_and_says_so():
    r = run("swap slides 2 and 5 and shorten slide 3", asks=["swap", "shorten"], check="satisfied", reordered=True,
            changed=[3], actions=[Outcome("edit_slide", C.APPLIED, 3, 1), Outcome("reorder", C.APPLIED, 0, 0)])
    out = block(r)
    assert "Slides were reordered; numbers below are the ones in your note." in out
    assert "✓ Slide 3 — reworded" in out and "Unchanged" not in out


# ----- verdict wording rules (AC-8, AC-9)


def test_unchecked_never_sounds_like_done():
    for clipped, check in itertools.product([False, True], ["unchecked", "satisfied", "partial"]):
        if not clipped and check != "unchecked":
            continue
        r = run("tighten slide 2", asks=["tighten slide 2"], changed=[2], check=check, clipped=clipped,
                unsatisfied=[0] if check == "partial" else [],
                actions=[Outcome("edit_slide", C.APPLIED, 2, 0)])
        out = block(r)
        assert "not verified" in lines(out)[0], out
        assert "done" not in out.lower().replace("unchanged", ""), out
        assert any(ln.startswith("? Couldn't check") for ln in lines(out))


def test_a_clipped_note_that_did_nothing_still_says_so():
    r = run("x", asks=["a"], check="unsatisfied", unsatisfied=[0], clipped=True,
            actions=[Outcome("edit_slide", C.NOT_FOUND, 2, 0)])
    assert lines(block(r))[0].startswith("Couldn't do what you asked")


def test_fallback_says_the_cause_and_has_no_unchanged_line():
    for cause, sentence in (("plan_failed", "couldn't plan targeted edits"), ("no_slides", "couldn't plan targeted"),
                            ("rebuild", "changes the whole deck")):
        out = block(run("note", fallback=True, cause=cause))
        assert out.startswith("Rebuilt the whole deck") and sentence in out and "Unchanged" not in out


def test_check_line_variants():
    base = dict(actions=[Outcome("edit_slide", C.APPLIED, 2, 0)], changed=[2])
    assert "all 3 done." in block(run(asks=["a", "b", "c"], check="satisfied", **base))
    out = block(run(asks=["a", "b", "c", "d", "e"], check="partial", unsatisfied=[1, 2, 3, 4], **base))
    assert 'Not done: "b"; "c"; "d"; +1 more.' in out and "1 of 5 done" in out
    out = block(run(asks=["a"], check="partial", unsatisfied=[], missed_slides=[5], **base))
    assert "Not done: slide 5." in out
    assert "Missing: " in block(run(asks=["only"], check="unsatisfied", unsatisfied=[0], **base))


# ===================================================================== sanitising (AC-11)

HOSTILE = "line1\nline2\r\n\x00\x07‮​<b>bold</b> *md* [x](http://e.vil) `code` " + "y" * 500


def test_clean_makes_one_capped_line_of_inert_text():
    out = rc.clean(HOSTILE, 80)
    assert "\n" not in out and len(out) <= 80
    for ch in "\x00\x07‮​\r":
        assert ch not in out
    assert out.endswith(rc.ELLIPSIS)


def test_quote_cannot_be_closed_early():
    assert rc.quote('say "hi" now', 40) == '"say \'hi\' now"'


@pytest.mark.parametrize("field", ["note", "ask", "unaddressed", "theme"])
def test_hostile_text_renders_as_capped_single_lines(field):
    r = run(HOSTILE if field == "note" else "x", asks=[HOSTILE if field == "ask" else "a"], check="partial",
            unsatisfied=[0], changed=[2], theme="riso" if field != "theme" else HOSTILE,
            unaddressed=[HOSTILE] if field == "unaddressed" else [],
            actions=[Outcome("edit_slide", C.APPLIED, 2, 0), Outcome("set_theme", C.APPLIED, 0, 0)])
    out = block(r)
    for ln in lines(out):
        assert len(ln) <= rc.CHANGE_LINE_MAX, ln
        assert "\r" not in ln and "\x00" not in ln and "‮" not in ln


def test_headline_in_a_label_is_sanitised_and_capped():
    deck = [dict(s) for s in DECK]
    deck[2]["headline"] = "<script>" + "z" * 100 + "\nnext"
    r = run(asks=["a"], check="unsatisfied", unsatisfied=[0], removed=[9], actions=[
        Outcome("edit_slide", C.REFUSED_INVARIANT, 3, 0)])
    out = block(r, deck)
    label = [ln for ln in lines(out) if ln.startswith("✗")][0]
    assert "\n" not in label and len(label) <= rc.CHANGE_LINE_MAX and "<script>" in label  # inert text, not markup


# ===================================================================== rule 15: nothing internal leaks

FORBIDDEN = ("Traceback", "Exception", "Error", "http://", "https://", "/Users/", ".py", "ollama", "qwen",
             "dropped_", "rejected_integrity", "refused_invariant", "unavailable", "not_found", "applied")


def test_nothing_internal_reaches_any_rendered_string():
    """Fault-injected run: free-text reasons, raw enum values and exception text in every
    place a record could carry them. Whatever the copy module prints, none of it is that."""
    poison = "TimeoutError: ollama qwen3 at http://x/y.py Traceback"
    r = run("tidy slide 2", asks=["tidy slide 2"], check="partial", unsatisfied=[0],
            check_reasons=["judge_unavailable", "note_clipped"], changed=[2], actions=[
                Outcome(op.value, code, 2, 0, poison) for op in (Op.EDIT_SLIDE, Op.FIX_FACT, Op.REPLACE_IMAGE)
                for code in OutcomeCode if code is not OutcomeCode.DEFERRED],
            dropped=[drop(reason) for reason in Reason])
    # a record from the DB can carry anything in `reason`: put the poison there directly too
    for a in r["actions"]:
        a["reason"] = poison
    out = block(r)
    for word in FORBIDDEN:
        assert word not in out, (word, out)
    for text in (rc.render_ask_operator(7, r["dropped"], DECK, THEMES), rc.run_summary(r, slides=DECK)):
        for word in FORBIDDEN:
            assert word not in text, (word, text)


# ===================================================================== squeezing to fit (AC-10)


def big_run(not_done=6, ok=6):
    actions = [Outcome("edit_slide", C.APPLIED, i, 0) for i in range(2, 2 + ok)]
    actions += [Outcome("fix_fact", C.UNCHANGED, i, 1) for i in range(2, 2 + not_done)]
    return run("n " * 50, asks=["do the thing " * 6, "and also this one " * 5], check="partial", unsatisfied=[1],
               changed=list(range(2, 2 + ok)), actions=actions, caption_changed=True,
               unaddressed=["something else"])


def test_full_block_is_at_most_the_cap_and_each_line_at_most_100():
    out = block(big_run())
    assert len(out) <= rc.CHANGES_MAX_CHARS
    assert all(len(ln) <= rc.CHANGE_LINE_MAX for ln in lines(out))


@pytest.mark.parametrize("budget", [1200, 700, 500, 350, 260, 230, 200, 199, 120])
def test_squeezing_keeps_header_check_and_not_done_lines_until_the_last_resort(budget):
    r = big_run()
    full = block(r)
    res = rc.fit_changes(r, slides=DECK, themes=THEMES, budget=budget)
    assert not res.standalone or len(res.text) <= rc.CHANGES_MAX_CHARS
    if not res.standalone:
        assert len(res.text) <= budget
    out = lines(res.text)
    assert out[0] == lines(full)[0]  # header survives every step
    assert any(ln.startswith("Checked against your note") for ln in out)  # so does the check line
    if budget >= rc.LAST_RESORT_BUDGET and not res.standalone:
        assert sum(ln.startswith("✗") for ln in out) >= 5  # the not-done lines survive steps 1-5


def test_squeeze_order_collapse_then_drop_untouched_then_last_resort():
    r = big_run()
    full = block(r)
    assert "Unchanged:" in full and full.count("✓") == 6
    collapsed = lines(rc.fit_changes(r, slides=DECK, budget=len(full) - 1).text)
    assert sum(ln.startswith("✓") for ln in collapsed) == 1 and any("changes applied: slides 2–7" in ln for ln in collapsed)
    assert "Unchanged:" in "\n".join(collapsed) or "may have missed" not in "\n".join(collapsed)
    tight = rc.fit_changes(r, slides=DECK, budget=len("\n".join(collapsed)) - 1).text
    assert "Unchanged:" not in tight and "may have missed" not in tight and "✗ Slide 2 — no change made" in tight
    last = rc.fit_changes(r, slides=DECK, budget=rc.LAST_RESORT_BUDGET - 1).text
    assert len(lines(last)) == 2 and last.startswith("Partly revised") and "Checked against" in last


def test_a_block_that_cannot_share_the_message_is_flagged_standalone():
    res = rc.fit_changes(big_run(), slides=DECK, budget=10)
    assert res.standalone and res.text.startswith("Partly revised") and len(res.text) <= rc.CHANGES_MAX_CHARS


def test_more_than_six_not_done_lines_say_how_many_more():
    r = big_run(not_done=8, ok=0)
    out = block(r)
    assert sum(ln.startswith("✗") for ln in lines(out)) == rc.MAX_NOT_DONE_LINES
    assert "+2 more not done" in out


def test_lines_are_cut_at_a_word_with_an_ellipsis_never_mid_word():
    r = run("tidy", asks=["a" + " word" * 60], check="unsatisfied", unsatisfied=[0],
            actions=[Outcome("edit_slide", C.NOT_FOUND, 2, 0)])
    long = [ln for ln in lines(block(r)) if ln.startswith("Checked")][0]
    assert long.endswith("…") and len(long) <= rc.CHANGE_LINE_MAX
    assert not long[:-1].endswith("wor")  # not mid-word


def test_preview_budget_is_the_smaller_of_the_cap_and_what_is_left():
    assert rc.preview_budget(100) == rc.CHANGES_MAX_CHARS
    assert rc.preview_budget(3000, 4096) == 4096 - 3000 - rc.PREVIEW_SEPARATOR_ALLOWANCE
    assert rc.preview_budget(500, 1024) == 1024 - 500 - rc.PREVIEW_SEPARATOR_ALLOWANCE


# ===================================================================== numbering


@given(
    removed=st.sets(st.integers(1, 10), max_size=5), added=st.sets(st.integers(1, 12), max_size=4),
)
def test_numbering_maps_survivors_in_order_onto_the_slots_added_slides_left_free(removed, added):
    num = rc.Numbering(frozenset(removed), frozenset(added))
    survivors = [n for n in range(1, 11) if n not in removed]
    mapped = [num.new(n) for n in survivors]
    assert mapped == sorted(mapped) and len(set(mapped)) == len(mapped)
    assert not set(mapped) & set(added)
    assert mapped == [n for n in range(1, len(survivors) + len(added) + 1) if n not in added][: len(survivors)]


def test_numbering_edges():
    assert rc.Numbering().new(5) == 5
    assert rc.Numbering(frozenset({2})).new(3) == 2
    assert rc.Numbering(frozenset({2})).new(2) == 2  # a removed slide keeps its old number
    assert rc.Numbering(added=frozenset({1})).new(1) == 2
    assert rc.Numbering(reordered=True, removed=frozenset({1})).new(4) == 4
    assert rc.Numbering(frozenset({1})).new(0) == 0


# ===================================================================== ask the operator


def test_ask_cause_priority_and_default():
    d = lambda *reasons: [drop(r) for r in reasons]  # noqa: E731
    assert rc.ask_cause([]) is rc.AskCause.VAGUE
    assert rc.ask_cause(d(Reason.NO_TRIGGER, Reason.SCOPED_NOTE)) is rc.AskCause.VAGUE
    assert rc.ask_cause(d(Reason.NO_SLIDE_REFERENCE)) is rc.AskCause.WHICH_SLIDE
    assert rc.ask_cause(d(Reason.RULES_ONLY)) is rc.AskCause.EXPLICIT_NEEDED
    assert rc.ask_cause(d(Reason.THEME_NOT_NAMED)) is rc.AskCause.WHICH_THEME
    assert rc.ask_cause(d(Reason.BAD_THEME)) is rc.AskCause.WHICH_THEME
    assert rc.ask_cause(d(Reason.NEGATED, Reason.RULES_ONLY, Reason.NO_SLIDE_REFERENCE)) is rc.AskCause.NEGATED
    assert rc.ask_cause(d(Reason.RULES_ONLY, Reason.THEME_NOT_NAMED)) is rc.AskCause.WHICH_THEME
    assert rc.ask_cause(d(Reason.THEME_NOT_NAMED, Reason.NEGATED)) is rc.AskCause.NEGATED
    assert rc.ask_cause(d(Reason.RULES_ONLY, Reason.NO_SLIDE_REFERENCE)) is rc.AskCause.EXPLICIT_NEEDED
    # run rows (what the notifier reads back from the DB) work the same
    assert rc.ask_cause([{"reason": "negated"}, {"reason": "garbage"}]) is rc.AskCause.NEGATED


GOLDEN_ASK_VAGUE = """I couldn't tell what to change — nothing was touched.
Slides in item 12:
1 "Blackwell B300 is here"
2 "Memory is the bottleneck"
3 "The die up close"
4 "Pricing leaks"
5 "40% faster inference"
6 "What to watch"
7–8 added automatically (can't be edited)
Try e.g. "slide 2 shorter" or "the photo slide is too wordy".
Reply with a new note, or /skip for a fresh full version."""


def test_ask_operator_vague_is_the_spec_mockup():
    assert rc.render_ask_operator(12, [], DECK, THEMES) == GOLDEN_ASK_VAGUE


def test_ask_operator_cause_lines_and_examples_are_real():
    out = rc.render_ask_operator(12, [drop(Reason.NEGATED)], DECK, THEMES)
    assert out.startswith(rc.ASK_CAUSE_LINES[rc.AskCause.NEGATED])
    out = rc.render_ask_operator(12, [drop(Reason.RULES_ONLY)], DECK, THEMES)
    assert out.startswith("Removing, swapping and restyling need exact wording")
    assert '"remove slide 6"' in out and '"swap slides 2 and 3"' in out  # slides that exist, not the hook
    out = rc.render_ask_operator(12, [drop(Reason.THEME_NOT_NAMED)], DECK, THEMES)
    assert "Which theme? Available: signal, riso." in out and '"use the signal theme"' in out
    out = rc.render_ask_operator(12, [drop(Reason.NO_SLIDE_REFERENCE)], DECK, THEMES)
    assert out.startswith("Which slide do you mean?")


def test_ask_operator_names_a_slide_that_does_not_exist():
    out = rc.render_ask_operator(3, [drop(Reason.OUT_OF_RANGE, slide=12)], DECK, THEMES)
    assert out.startswith("Nothing was touched.\nSlide 12 doesn't exist — the deck has 8 slides.")


def test_ask_operator_with_a_short_deck_and_no_deck():
    short = DECK[:3] + [{"type": "follow"}]
    out = rc.render_ask_operator(3, [], short, [])
    assert "4 added automatically" in out and "Try e.g." in out
    assert rc.render_ask_operator(3, [], None, []).endswith(rc.ASK_EXIT)


def test_ask_operator_never_carries_text_it_was_not_given_by_the_deck():
    """The note is not even a parameter; the only operator-written text is a headline."""
    import inspect

    assert "note" not in inspect.signature(rc.render_ask_operator).parameters


# ===================================================================== confirm prompt


def test_confirm_remove_one_slide():
    v = rc.confirm_view([Action("remove_slide", slide=4)], [Action("edit_slide", slide=2)], slides=DECK)
    assert v.text == (
        "Remove slide 4? There is no undo — the old version isn't kept.\n"
        'Slide 4 "Pricing leaks"\n'
        "Also doing: slide 2 reworded."
    )
    assert (v.yes, v.no) == ("✅ Yes, remove", "↩️ No, keep it") and not v.rebuild


def test_confirm_remove_many_and_the_label_budget():
    held = [Action("remove_slide", slide=n) for n in (2, 3, 4, 5)]
    v = rc.confirm_view(held, slides=DECK)
    assert v.what == "4 slides" and v.yes == "✅ Yes, remove 4" and v.no == "↩️ No, keep them"
    assert sum(ln.startswith("Slide ") for ln in v.text.split("\n")) == 4  # the list stays
    v2 = rc.confirm_view(held[:2], slides=DECK)
    assert v2.what == "slides 2, 3"
    for view in (v, v2, rc.confirm_view([Action("full_recompose")])):
        assert "❌" not in view.yes + view.no and view.yes != view.no
        assert len(view.yes) <= 20 and len(view.no) <= 20
        assert "no undo" in view.text


def test_confirm_rebuild_quotes_the_note_once():
    v = rc.confirm_view([Action("full_recompose")], note="start over, this angle isn't working")
    assert v.text == ("Rebuild the whole deck from scratch? Every slide gets new text. There is no undo.\n"
                      'Your note: "start over, this angle isn\'t working"')
    assert (v.yes, v.no, v.rebuild) == ("✅ Yes, rebuild", "↩️ No, keep deck", True)
    assert "Your note" not in rc.confirm_view([Action("full_recompose")]).text


def test_also_doing_is_capped():
    rest = [Action("edit_slide", slide=n) for n in (2, 3, 5)] + [Action("rewrite_caption"), Action("research")]
    v = rc.confirm_view([Action("remove_slide", slide=4)], rest, slides=DECK)
    assert "Also doing: slide 2 reworded, slide 3 reworded, slide 5 reworded, +2 more." in v.text


def test_answered_texts_match_the_spec():
    v = rc.confirm_view([Action("remove_slide", slide=4)], slides=DECK)
    R = ConfirmResult
    assert rc.confirm_answered_text(R.YES, v, 5) == "Removing slide 4…"
    assert rc.confirm_answered_text(R.NO_CONTINUE, v, 5) == "Kept slide 4. Applying the rest…"
    assert rc.confirm_answered_text(R.NO_NOTHING_LEFT, v, 5) == (
        "Kept slide 4. Item 5 is unchanged — the preview above still stands.")
    rebuild = rc.confirm_view([Action("full_recompose")])
    assert rc.confirm_answered_text(R.YES, rebuild, 5) == "Rebuilding the deck…"
    assert rc.confirm_answered_text(R.STALE, None, 5) == "Replaced by your newer note."
    assert rc.confirm_answered_text(R.ALREADY_ANSWERED, v, 5) == "Answered — working on it."
    assert rc.confirm_answered_text(R.ALREADY_ANSWERED, v, 5, answered=True) == "Removing slide 4…"
    assert rc.confirm_answered_text(R.ALREADY_ANSWERED, v, 5, answered=False) == "Kept slide 4. Applying the rest…"
    assert rc.confirm_answered_text(R.NO_ITEM, None, 5) == "That item no longer exists."


# ===================================================================== prompt, ack, deferral


def test_regen_prompt_teaches_by_example_and_fits_a_message():
    p = rc.regen_prompt(12, THEMES)
    assert "Revising item 12" in p and len(p) < rc.REGEN_PROMPT_MAX
    assert p.count("•") >= 5
    for phrase in ("remove slide 4", "swap slides 2 and 5", "use the signal theme", "/skip = fresh full version"):
        assert phrase in p
    assert len(p) > TELEGRAM_CALLBACK_ANSWER_LIMIT, "that is why it is a chat message, not a toast"
    assert "Themes: signal, riso" in p
    assert "theme" not in rc.regen_prompt(12, []).split("/skip")[0].split("Only what")[0].split("remove slide 4")[1]


def test_ack_and_deferral_wording():
    assert rc.ACK_NOTE.format(id=3) != rc.ACK_SKIP.format(id=3)
    for text in (rc.ACK_NOTE, rc.ACK_SKIP):
        assert not any(w in text.lower() for w in ("applied", "removed", "reworded", "will change"))
    for word in ("timeout", "ollama", "error", "exception"):
        assert word not in rc.DEFERRED.lower()


def test_run_summary():
    assert rc.run_summary({"ask_operator": True}) == rc.RUN_ASKED_SUMMARY
    assert rc.run_summary(run("x")) == rc.RUN_EMPTY_SUMMARY
    assert rc.run_summary("garbage") == rc.RUN_EMPTY_SUMMARY


def test_check_status_enum_is_covered_by_headers():
    assert set(rc.HEADER_BY_STATUS) == set(Status)


# ===================================================================== boundaries found by mutation


def test_a_long_unbroken_token_is_cut_at_the_cap_not_at_a_far_earlier_space():
    out = rc.clean("ab " + "y" * 200, 40)
    assert len(out) == 40 and out.endswith("…") and out.startswith("ab yyyy")
    assert rc.clean("word " * 30, 40).endswith("word…")  # but a space near the end is used


def test_clean_leaves_text_exactly_at_the_cap_alone():
    assert rc.clean("x" * 40, 40) == "x" * 40
    assert rc.clean("x" * 41, 40) == "x" * 39 + "…"


def test_a_block_exactly_at_the_budget_is_not_squeezed():
    r = big_run()
    full = block(r)
    assert rc.fit_changes(r, slides=DECK, themes=THEMES, budget=len(full)).text == full
    assert rc.fit_changes(r, slides=DECK, themes=THEMES, budget=len(full) - 1).text != full


def test_executor_outcomes_that_carry_a_reason_value_use_the_reason_sentence():
    cases = [
        (C.UNCHANGED, "remove_image", "no_image", "✗ Slide 3 has no image to remove."),
        (C.NOT_FOUND, "edit_slide", "target_removed", "– Slide 3 edit skipped: that slide was removed."),
        (C.REFUSED_INVARIANT, "edit_slide", "closing_slide", "✗ Slide 3 is added automatically and can't be edited."),
        (C.DROPPED_INVALID, "edit_slide", "out_of_range", "✗ Slide 3 doesn't exist — the deck has 8 slides."),
        (C.UNCHANGED, "reorder", "noop_reorder", "– Slides were already in that order."),
        (C.DROPPED_INVALID, "set_theme", "bad_theme", "✗ No theme by that name. Available: signal, riso."),
    ]
    for code, op, reason, want in cases:
        r = run(asks=["a"], check="unsatisfied", unsatisfied=[0], actions=[Outcome(op, code, 3, 0, reason)])
        assert want in block(r), (code, op, reason, block(r))


def test_when_every_slide_changed_there_is_no_unchanged_line():
    five = DECK[:3] + [{"type": "sources"}, {"type": "follow"}]
    r = run(asks=["a"], check="satisfied", changed=[1, 2, 3, 4, 5],
            actions=[Outcome("edit_slide", C.APPLIED, n, 0) for n in (1, 2, 3)])
    out = block(r, five)
    assert "Unchanged" not in out and "Slides unchanged" not in out and "Nothing changed" not in out


def test_the_header_keeps_its_closing_quote_whatever_the_status_prefix():
    for check, head in (("unsatisfied", "Couldn't do what you asked"), ("partial", "Partly revised"),
                        ("unchecked", "Revised, not verified"), ("satisfied", "Revised per your note")):
        r = run("n" * 300, asks=["a"], check=check, unsatisfied=[0], changed=[2],
                actions=[Outcome("edit_slide", C.APPLIED, 2, 0)])
        first = lines(block(r))[0]
        assert first.startswith(head) and len(first) <= rc.CHANGE_LINE_MAX
        assert first.endswith('…"'), first


def test_toast_is_cut_to_the_platform_cap():
    assert len(rc.toast("x " * 300)) <= TELEGRAM_CALLBACK_ANSWER_LIMIT
    assert rc.toast("short") == "short"


# ===================================================================== tester bounce 1 (D1-D4, D289)


def test_ask_for_a_failed_plan_says_it_could_not_work_it_out():
    out = rc.render_ask_operator(3, [], DECK, THEMES, plan_failed=True)
    assert out.startswith(rc.ASK_PLAN_FAILED_LINE) and "work out" in out
    # a more specific cause still wins over the generic failure line
    assert rc.render_ask_operator(3, [drop(Reason.NEGATED)], DECK, THEMES, plan_failed=True).startswith(
        rc.ASK_CAUSE_LINES[rc.AskCause.NEGATED])
    assert rc.render_ask_operator(3, [], DECK, THEMES).startswith(rc.ASK_CAUSE_LINES[rc.AskCause.VAGUE])
