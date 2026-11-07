"""T297: revision/structural.py - deterministic deck ops - and render's theme precedence."""

from __future__ import annotations

import copy
from functools import partial

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pipeline.deck_shape import CLOSING_TYPES, MAX_SLIDES, MIN_SLIDES
from pipeline.models import Item, Status
from pipeline.revision import structural as s
from pipeline.revision.model import Action, OutcomeCode
from pipeline.stages.render import TEXT_ONLY_THEMES, available_themes, wanted_theme

THEMES = available_themes()
NON_TEXT_ONLY = next(t for t in THEMES if t not in TEXT_ONLY_THEMES)


def mk(t: str, i: int, **kw) -> dict:
    base = {"type": t, "headline": f"H{i}"}
    body = {"hook": {"sub": "x"}, "point": {"bullets": ["b"]}, "kpi": {"tiles": [1]},
            "photo": {"image": "p.jpg"}, "takeaway": {"sub": "t"}, "follow": {"sub": "f"},
            "sources": {"urls": ["http://a"]}, "chart": {"series": [1]}}
    return {**base, **body.get(t, {}), **kw}


def deck_of(*types: str) -> s.Deck:
    return s.Deck.of([mk(t, i) for i, t in enumerate(types)])


D8 = ("hook", "point", "photo", "point", "kpi", "takeaway", "sources", "follow")


CTX = {"is_dm": False, "source_urls": []}
remove_slide = partial(s.remove_slide, **CTX)
reorder = partial(s.reorder, **CTX)
remove_image = partial(s.remove_image, **CTX)
insert_slide = partial(s.insert_slide, **CTX)
OPS = {"remove_slide": remove_slide, "remove_image": remove_image}


def A(op: str, slide: int = 0, **kw) -> Action:
    return Action(op=op, slide=slide, **kw)


def heads(d: s.Deck) -> list[str]:
    return [x["headline"] for x in d.slides]


# ---- remove_slide -------------------------------------------------------

def test_remove_slide_by_stable_id_survives_earlier_removal():
    d = deck_of(*D8)
    snap = copy.deepcopy(d.slides)
    st1 = remove_slide(d, A("remove_slide", 2))  # slide 2 = id 1
    assert st1.outcome.code is OutcomeCode.APPLIED and heads(st1.deck)[:3] == ["H0", "H2", "H3"]
    # a later removal planned against the ORIGINAL numbering: slide 4 is still "H3"
    st2 = remove_slide(st1.deck, A("remove_slide", 4))
    assert "H3" not in heads(st2.deck) and "H2" in heads(st2.deck)
    assert d.slides == snap  # input untouched
    assert st2.deck.ids == (0, 2, 4, 5, 6, 7)


def test_by_id_maps_original_ids_removed_absent_added_above_original_length():
    d = deck_of("hook", "point", "point", "point", "takeaway")
    r = remove_slide(d, A("remove_slide", 3))
    m = r.deck.by_id()
    assert 2 not in m and m[1]["headline"] == "H1" and m[3]["headline"] == "H3"
    added = [i for i in m if i not in d.ids]
    assert added and all(i >= len(d) for i in added)
    assert r.outcome.slide == 3 and r.outcome.ask == 0  # 1-based slide, ask index from the action


def test_remove_then_edit_target_still_resolves():
    st1 = remove_slide(deck_of(*D8), A("remove_slide", 2))
    assert st1.deck.position(Action("edit_slide", slide=5).slide_id) == 3  # H4 moved to index 3, id kept


def test_remove_already_removed_is_unchanged_and_unknown_is_not_found():
    d = remove_slide(deck_of(*D8), A("remove_slide", 2)).deck
    assert remove_slide(d, A("remove_slide", 2)).outcome.code is OutcomeCode.UNCHANGED
    assert remove_slide(d, A("remove_slide", 40)).outcome.code is OutcomeCode.NOT_FOUND
    assert remove_slide(d, A("remove_slide", 0)).outcome.code is OutcomeCode.NOT_FOUND


def test_remove_refuses_below_min_slides():
    d = deck_of("hook", "point", "follow")
    assert len(d) == MIN_SLIDES
    r = remove_slide(d, A("remove_slide", 2))
    assert (r.outcome.code, r.outcome.reason) == (OutcomeCode.REFUSED_INVARIANT, s.REASON_MIN_SLIDES)
    assert r.deck is d


def test_remove_exactly_to_min_is_allowed():
    d = deck_of("hook", "point", "point", "follow")
    assert remove_slide(d, A("remove_slide", 2)).outcome.code is OutcomeCode.APPLIED


@pytest.mark.parametrize("n", [7, 8])
def test_closing_slides_cannot_be_removed(n):
    d = deck_of(*D8)
    r = remove_slide(d, A("remove_slide", n))
    assert (r.outcome.code, r.outcome.reason) == (OutcomeCode.REFUSED_INVARIANT, s.REASON_CLOSING)
    assert r.deck is d


def test_hook_cannot_be_removed():
    r = remove_slide(deck_of(*D8), A("remove_slide", 1))
    assert (r.outcome.code, r.outcome.reason) == (OutcomeCode.REFUSED_INVARIANT, s.REASON_HOOK)


def test_removal_reruns_closing_pass_and_gives_added_slide_a_fresh_id():
    # no follow slide in the input: ensure_closing_slide must add one, with an unused id
    d = deck_of("hook", "point", "point", "point", "takeaway")
    r = remove_slide(d, A("remove_slide", 2))
    assert r.deck.slides[-1]["type"] == "follow"
    assert r.deck.ids[-1] >= d.next_id and len(set(r.deck.ids)) == len(r.deck)


def test_every_slide_added_by_the_closing_pass_gets_its_own_fresh_id():
    d = deck_of("hook", "point", "point", "point")
    r = remove_slide(d, A("remove_slide", 2), source_urls=["http://a"])
    assert [x["type"] for x in r.deck.slides[-2:]] == ["sources", "follow"]
    assert len(set(r.deck.ids)) == len(r.deck) and r.deck.next_id == max(r.deck.ids) + 1


def test_removal_never_deletes_another_slide_through_the_cta_pass():
    d = s.Deck.of([mk("hook", 0), mk("point", 1), mk("point", 2),
                   mk("point", 3, headline="Follow us for more!"), mk("follow", 4)])
    r = remove_slide(d, A("remove_slide", 2))
    assert (r.outcome.code, r.outcome.reason) == (OutcomeCode.REFUSED_INVARIANT, s.REASON_CTA_DROPPED)
    assert r.deck is d


def test_legacy_deck_without_follow_is_never_silently_trimmed_on_insert():
    d = deck_of("hook", *["point"] * 8)  # 9 slides, no closing slide
    r = insert_slide(d, A("add_slide", 2), mk("point", 99))
    assert (r.outcome.code, r.outcome.reason) == (OutcomeCode.REFUSED_INVARIANT, s.REASON_CLOSING_CLAMP)
    assert r.deck is d and "H8" in heads(r.deck)


def test_legacy_ten_slide_reorder_is_never_silently_trimmed():
    d = deck_of("hook", *["point"] * 9)
    r = reorder(d, A("reorder", order=(1, 3, 2)))
    assert (r.outcome.code, r.outcome.reason) == (OutcomeCode.REFUSED_INVARIANT, s.REASON_CLOSING_CLAMP)
    assert r.deck is d


# ---- reorder ------------------------------------------------------------

def test_reorder_swaps_and_keeps_hook_first_closing_last():
    r = reorder(deck_of(*D8), A("reorder", order=(2, 1, 5, 4, 3, 6, 8, 7)))  # tries to move hook + closers
    assert r.outcome.code is OutcomeCode.APPLIED
    assert r.deck.slides[0]["type"] == "hook"
    assert [x["type"] for x in r.deck.slides[-2:]] == ["sources", "follow"]


def test_reorder_moves_body_slide():
    r = reorder(deck_of(*D8), A("reorder", order=(1, 5, 3, 4, 2, 6, 7, 8)))
    assert r.outcome.code is OutcomeCode.APPLIED
    assert heads(r.deck)[:5] == ["H0", "H4", "H2", "H3", "H1"]


def test_reorder_identity_is_unchanged():
    d = deck_of(*D8)
    r = reorder(d, A("reorder", order=tuple(range(1, 9))))
    assert r.outcome.code is OutcomeCode.UNCHANGED and r.deck is d


def test_reorder_only_closing_shuffle_is_unchanged():
    r = reorder(deck_of(*D8), A("reorder", order=(1, 2, 3, 4, 5, 6, 8, 7)))
    assert r.outcome.code is OutcomeCode.UNCHANGED


def test_reorder_skips_removed_ids_and_appends_omitted():
    d = remove_slide(deck_of(*D8), A("remove_slide", 3)).deck  # drop id 2
    r = reorder(d, A("reorder", order=(1, 5, 3, 4, 2)))  # 3 is gone; 6,7,8 omitted
    assert r.outcome.code is OutcomeCode.APPLIED
    assert heads(r.deck)[:4] == ["H0", "H4", "H3", "H1"]
    assert len(r.deck) == 7


def test_reorder_ignores_duplicates_and_out_of_range():
    r = reorder(deck_of(*D8), A("reorder", order=(1, 5, 5, 99, 0, -3, 2)))
    assert sorted(r.deck.ids) == list(range(8))


# ---- remove_image -------------------------------------------------------

def test_remove_image_drops_image_and_mode_only_on_that_slide():
    d = s.Deck.of([mk("hook", 0), mk("point", 1, image="a.jpg", image_mode="inset"),
                   mk("point", 2, image="b.jpg"), mk("follow", 3)])
    snap = copy.deepcopy(d.slides)
    r = remove_image(d, A("remove_image", 2))
    assert r.outcome.code is OutcomeCode.APPLIED
    assert "image" not in r.deck.slides[1] and "image_mode" not in r.deck.slides[1]
    assert r.deck.slides[2]["image"] == "b.jpg"
    assert d.slides == snap


def test_remove_image_without_image_is_unchanged():
    d = deck_of(*D8)
    r = remove_image(d, A("remove_image", 2))
    assert (r.outcome.code, r.outcome.reason) == (OutcomeCode.UNCHANGED, "no_image") and r.deck is d


def test_remove_image_from_photo_slide_refused_not_hollowed():
    d = deck_of(*D8)
    r = remove_image(d, A("remove_image", 3))
    assert (r.outcome.code, r.outcome.reason) == (OutcomeCode.REFUSED_INVARIANT, s.REASON_BODYLESS)
    assert r.deck.slides[2]["image"] == "p.jpg"


def test_remove_image_on_removed_slide_is_not_found():
    d = remove_slide(deck_of(*D8), A("remove_slide", 2)).deck
    assert remove_image(d, A("remove_image", 2)).outcome.code is OutcomeCode.NOT_FOUND


# ---- insert_slide (add_slide placement + ceiling) ------------------------

def test_insert_after_target_by_stable_id():
    d = remove_slide(deck_of(*D8), A("remove_slide", 2)).deck
    r = insert_slide(d, A("add_slide", 4), mk("point", 99))
    assert r.outcome.code is OutcomeCode.APPLIED
    assert heads(r.deck).index("H99") == heads(r.deck).index("H3") + 1
    assert r.deck.ids[heads(r.deck).index("H99")] == d.next_id


def test_insert_after_removed_target_goes_after_nearest_earlier():
    d = remove_slide(deck_of(*D8), A("remove_slide", 4)).deck  # id 3 gone
    r = insert_slide(d, A("add_slide", 4), mk("point", 99))
    assert heads(r.deck).index("H99") == heads(r.deck).index("H2") + 1


def test_insert_after_closing_target_lands_before_tail():
    r = insert_slide(deck_of(*D8), A("add_slide", 8), mk("point", 99))
    assert [x["type"] for x in r.deck.slides[-2:]] == ["sources", "follow"]
    assert "H99" in heads(r.deck)


def test_insert_refused_at_max_slides():
    d = deck_of("hook", *["point"] * (MAX_SLIDES - 3), "sources", "follow")
    assert len(d) == MAX_SLIDES
    r = insert_slide(d, A("add_slide", 2), mk("point", 99))
    assert (r.outcome.code, r.outcome.reason) == (OutcomeCode.REFUSED_INVARIANT, s.REASON_MAX_SLIDES)
    assert r.deck is d


def test_insert_one_below_max_allowed():
    d = deck_of("hook", *["point"] * (MAX_SLIDES - 4), "sources", "follow")
    r = insert_slide(d, A("add_slide", 2), mk("point", 99))
    assert r.outcome.code is OutcomeCode.APPLIED and len(r.deck) == MAX_SLIDES


def test_insert_of_cta_slide_is_refused_not_silently_lost():
    d = deck_of(*D8)
    r = insert_slide(d, A("add_slide", 2), mk("point", 99, headline="Follow us for more"))
    assert (r.outcome.code, r.outcome.reason) == (OutcomeCode.REFUSED_INVARIANT, s.REASON_CTA_DROPPED)
    assert r.deck is d


@pytest.mark.parametrize("t", sorted(CLOSING_TYPES))
def test_insert_of_a_closing_type_slide_is_refused(t):
    d = deck_of(*D8)
    r = insert_slide(d, A("add_slide", 2), mk(t, 99))
    assert (r.outcome.code, r.outcome.reason) == (OutcomeCode.REFUSED_INVARIANT, s.REASON_CLOSING)
    assert r.deck is d


def test_insert_after_a_reordered_target_uses_its_current_position():
    r1 = reorder(deck_of(*D8), A("reorder", order=(1, 3, 2, 4, 5, 6, 7, 8)))  # ids now 0,2,1,3,...
    assert r1.deck.ids[:3] == (0, 2, 1)
    r2 = insert_slide(r1.deck, A("add_slide", 3), mk("point", 99))  # after slide 3 = id 2 = H2
    assert heads(r2.deck)[:4] == ["H0", "H2", "H99", "H1"]


def test_insert_leaves_the_input_deck_object_and_next_id_untouched_on_refusal():
    d = deck_of(*D8)
    r = insert_slide(d, A("add_slide", 2), mk("follow", 99))
    assert r.deck.next_id == 8


# ---- set_theme ----------------------------------------------------------

def item(**kw) -> Item:
    return Item(id=1, source="channel", status=Status.COMPOSED,
                slides=kw.pop("slides", [mk("hook", 0), mk("point", 1), mk("follow", 2)]), **kw)


def test_set_theme_pins_on_a_copy():
    it = item()
    r = s.set_theme(it, A("set_theme", theme=NON_TEXT_ONLY.upper()))
    assert r.deck is None
    assert r.outcome.code is OutcomeCode.APPLIED and r.outcome.reason == ""
    assert r.item.theme_pin == NON_TEXT_ONLY and it.theme_pin is None


def test_set_theme_matches_case_insensitively_and_pins_the_canonical_name():
    r = s.set_theme(item(), A("set_theme", theme="riso"), themes=["Riso", "Signal"])
    assert r.item.theme_pin == "Riso"


def test_set_theme_unknown_invalid():
    r = s.set_theme(item(), A("set_theme", theme="nope"))
    assert r.outcome.code is OutcomeCode.DROPPED_INVALID and r.item.theme_pin is None
    assert s.set_theme(item(), A("set_theme", theme="")).outcome.code is OutcomeCode.DROPPED_INVALID


def test_set_theme_same_pin_unchanged():
    it = item(theme_pin=NON_TEXT_ONLY)
    assert s.set_theme(it, A("set_theme", theme=NON_TEXT_ONLY)).outcome.code is OutcomeCode.UNCHANGED


def test_text_only_theme_with_photo_is_applied_with_warning():
    t = sorted(TEXT_ONLY_THEMES)[0]
    it = item(slides=[mk("hook", 0), mk("photo", 1), mk("follow", 2)])
    r = s.set_theme(it, A("set_theme", theme=t))
    assert (r.outcome.code, r.outcome.reason) == (OutcomeCode.APPLIED, s.WARN_TEXT_ONLY_THEME)
    assert r.item.theme_pin == t


def test_text_only_theme_without_photo_has_no_warning():
    t = sorted(TEXT_ONLY_THEMES)[0]
    assert s.set_theme(item(), A("set_theme", theme=t)).outcome.reason == ""


def test_set_theme_uses_in_flight_slides_over_item_slides():
    t = sorted(TEXT_ONLY_THEMES)[0]
    r = s.set_theme(item(), A("set_theme", theme=t), slides=[mk("photo", 1)])
    assert r.outcome.reason == s.WARN_TEXT_ONLY_THEME


# ---- render precedence (T283) -------------------------------------------

def test_wanted_theme_pin_beats_setting_and_composer():
    it = item(theme="composer", theme_pin=NON_TEXT_ONLY)
    for cfg in ("rotate", "random", "other", ""):
        assert wanted_theme(it, cfg) == NON_TEXT_ONLY


def test_wanted_theme_unpinned_path_is_unchanged():
    # exactly the pre-T297 rule: setting (named/rotate/random) beats the composer's pick
    it = item(theme="composer")
    assert wanted_theme(it, "rotate") == "rotate"
    assert wanted_theme(it, "random") == "random"
    assert wanted_theme(it, " signal ") == "signal"
    assert wanted_theme(it, "") == "composer"
    assert wanted_theme(it, None) == "composer"
    assert wanted_theme(item(), "") == ""
    assert wanted_theme(item(theme_pin="  "), "rotate") == "rotate"


def test_wanted_theme_ignores_pin_for_a_theme_that_no_longer_exists():
    assert wanted_theme(item(theme="composer", theme_pin="deleted-theme"), "rotate") == "rotate"


# ---- property: any structural sequence keeps the invariants --------------

def _ops():
    n = MAX_SLIDES + 2
    return st.one_of(
        st.tuples(st.just("remove_slide"), st.integers(0, n)),
        st.tuples(st.just("remove_image"), st.integers(0, n)),
        st.tuples(st.just("reorder"), st.lists(st.integers(-1, n), max_size=n + 2)),
        st.tuples(st.just("add_slide"), st.integers(0, n)),
    )


@settings(max_examples=300, deadline=None)
@given(
    mid=st.lists(st.sampled_from(["point", "kpi", "chart", "photo"]), min_size=1, max_size=MAX_SLIDES - 3),
    tail=st.sampled_from([("sources", "follow"), ("follow",)]),
    ops=st.lists(_ops(), max_size=14),
)
def test_any_op_sequence_keeps_hook_first_closing_last_and_bounds(mid, tail, ops):
    d = deck_of("hook", *mid, *tail)
    for kind, arg in ops:
        if kind == "reorder":
            r = reorder(d, A("reorder", order=tuple(arg)))
        elif kind == "add_slide":
            r = insert_slide(d, A("add_slide", arg), mk("point", 500 + d.next_id))
        else:
            r = OPS[kind](d, A(kind, arg))
        assert r.outcome.code in set(OutcomeCode)
        if r.outcome.code is not OutcomeCode.APPLIED:
            assert r.deck is d
        d = r.deck
        types = [x["type"] for x in d.slides]
        assert types[0] == "hook"
        assert MIN_SLIDES <= len(d) <= MAX_SLIDES
        k = sum(t in CLOSING_TYPES for t in types)
        assert k >= 1 and all(t in CLOSING_TYPES for t in types[-k:])
        assert not any(t in CLOSING_TYPES for t in types[:-k])
        assert len(set(d.ids)) == len(d) and max(d.ids) < d.next_id
        assert types[-1] == "follow"


def test_deck_ops_require_closing_context_explicitly():
    with pytest.raises(TypeError):
        s.remove_slide(deck_of(*D8), A("remove_slide", 2))
