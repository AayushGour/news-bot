"""T297 blackbox: structural revision ops against REAL decks from a .backup of app.db.

Public API only. The DB is copied with sqlite's online backup (read-only source);
the suite skips if there is no app.db.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import sqlite3
import subprocess
from pathlib import Path

import pytest

from pipeline.deck_shape import CLOSING_TYPES, MAX_SLIDES, MIN_SLIDES
from pipeline.models import Item, Status
from pipeline.revision.model import Action, OutcomeCode
from pipeline.revision.structural import (
    DECK_OPS,
    Deck,
    insert_slide,
    reorder,
    remove_image,
    remove_slide,
    set_theme,
)
from pipeline.stages import render

ROOT = Path(__file__).resolve().parents[1]
SRC_DB = ROOT / "data" / "app.db"
SAMPLE_URLS = ["https://example.com/a", "https://example.com/b"]
REFUSALS = {
    OutcomeCode.REFUSED_INVARIANT, OutcomeCode.UNCHANGED, OutcomeCode.NOT_FOUND, OutcomeCode.DROPPED_INVALID,
}
APPLIED = OutcomeCode.APPLIED
ADDED_BODY = {"type": "point", "headline": "Inserted", "bullets": ["x"], "sub": "s"}


def sha(slide: dict) -> str:
    return hashlib.sha256(json.dumps(slide, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- real decks
@pytest.fixture(scope="module")
def rows(tmp_path_factory):
    if not SRC_DB.exists():
        pytest.skip("no data/app.db")
    dst = tmp_path_factory.mktemp("bk") / "copy.db"
    src = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    src.close()
    got = []
    for iid, source, slides in out.execute("select id, source, slides from items where slides != '[]'"):
        got.append((iid, source == "dm", json.loads(slides)))
    out.close()
    return got


def _types(s):
    return [x.get("type") for x in s]


def _first(rows, pred):
    return next((r for r in rows if pred(r)), None)


@pytest.fixture(scope="module")
def picks(rows):
    p = {
        "news_photo": _first(rows, lambda r: not r[1] and "photo" in _types(r[2]) and "follow" in _types(r[2])),
        "news_nophoto": _first(rows, lambda r: not r[1] and "photo" not in _types(r[2]) and "follow" in _types(r[2]) and len(r[2]) >= 8),
        "list_links": _first(rows, lambda r: "links" in _types(r[2]) and "photo" not in _types(r[2])),
        "repo_dm": _first(rows, lambda r: r[1] and "repo" in _types(r[2]) and "follow" in _types(r[2])),
        "dm_photo": _first(rows, lambda r: r[1] and "photo" in _types(r[2])),
        "dm_plain": _first(rows, lambda r: r[1] and "follow" in _types(r[2]) and "repo" not in _types(r[2])),
        "legacy_channel": _first(rows, lambda r: not r[1] and "follow" not in _types(r[2])),
        "legacy_dm": _first(rows, lambda r: r[1] and "follow" not in _types(r[2])),
        "max10": _first(rows, lambda r: len(r[2]) == 10 and "follow" in _types(r[2]) and not r[1]),
        "small": _first(rows, lambda r: len(r[2]) <= 4 and "follow" in _types(r[2])),
    }
    got = {k: v for k, v in p.items() if v}
    assert len(got) >= 6, f"need >=6 varied real decks, found {sorted(got)}"
    assert "legacy_channel" in got, "need a real legacy deck lacking a follow slide"
    return got


def urls_of(slides):
    for s in slides:
        if s.get("type") == "sources" and s.get("urls"):
            return list(s["urls"])
    return list(SAMPLE_URLS)


def all_decks(picks):
    for name, (iid, is_dm, slides) in picks.items():
        yield f"{name}#{iid}", is_dm, Deck.of(slides), urls_of(slides)


# ----------------------------------------------------------------- checkers
def check_invariants(deck: Deck, is_dm: bool, label=""):
    t = _types(deck.slides)
    assert t[0] == "hook", label
    assert t.count("follow") == 1, (label, t)
    assert t[-1] == "follow", (label, t)
    k = next(i for i, x in enumerate(t) if x in CLOSING_TYPES)
    assert all(x in CLOSING_TYPES for x in t[k:]), (label, t)
    assert [CLOSING_TYPES.index(x) for x in t[k:]] == sorted(CLOSING_TYPES.index(x) for x in t[k:]), (label, t)
    assert MIN_SLIDES <= len(deck) <= MAX_SLIDES, (label, len(deck))
    assert len(set(deck.ids)) == len(deck.ids) == len(deck.slides), label
    assert deck.next_id > max(deck.ids)
    return t


def check_step(before: Deck, step, *, is_dm, removed=None, stripped=None, inserted_after=None, label=""):
    code = step.outcome.code
    if code is not APPLIED:
        assert code in REFUSALS, (label, code)
        assert step.deck is before, (label, "refusal must return the SAME Deck object", code)
        return
    new = step.deck
    check_invariants(new, is_dm, label)
    before_by, new_by = before.by_id(), new.by_id()
    expected = set(before.ids) - ({removed} if removed is not None else set())
    assert expected <= set(new.ids), (label, "silent loss", expected - set(new.ids))
    if removed is not None:
        assert removed not in new.ids, label
    extras = [i for i in new.ids if i not in before.ids]
    for i in extras:
        assert i >= before.next_id, label
    fresh = [new_by[i] for i in extras]
    n_body_new = [s for s in fresh if s.get("type") not in CLOSING_TYPES]
    assert len(n_body_new) == (1 if inserted_after is not None else 0), (label, fresh)
    assert all(s["type"] in CLOSING_TYPES for s in fresh if s not in n_body_new), label
    if is_dm:
        assert not any(s["type"] == "sources" for s in fresh), (label, "DM got a sources slide")
    for i in expected:
        want = before_by[i]
        if stripped == i:
            want = {k: v for k, v in want.items() if k not in ("image", "image_mode")}
        assert sha(new_by[i]) == sha(want), (label, "content changed", i)
    if inserted_after is not None:
        (nid,) = [i for i in extras if new_by[i].get("type") not in CLOSING_TYPES]
        assert sha(new_by[nid]) == sha(ADDED_BODY), label
        pos = new.ids.index(nid)
        tgt = inserted_after
        anchor = tgt
        if tgt not in before.ids:  # target removed earlier: nearest earlier survivor by position (else slot 1)
            earlier = [i for i in before.ids if i < tgt]
            anchor = earlier[-1] if earlier else before.ids[0]
        if before_by[anchor].get("type") not in CLOSING_TYPES:
            assert new.ids[pos - 1] == anchor, (label, "inserted not after anchor")
        else:  # closing/removed target -> last body slide
            assert new.slides[pos + 1].get("type") in CLOSING_TYPES, label


def A(op, slide=0, order=(), theme=""):
    return Action(op=op, slide=slide, order=tuple(order), theme=theme)


def kw(is_dm, urls):
    return {"is_dm": is_dm, "source_urls": urls}


def all_ops(deck: Deck, is_dm, urls):
    """Yield (label, kind, target_id, step) for every applicable op on every slide."""
    for i, sid in enumerate(deck.ids):
        s = A("remove_slide", sid + 1)
        yield f"remove_slide[{sid}]", "remove", sid, remove_slide(deck, s, **kw(is_dm, urls))
        s = A("remove_image", sid + 1)
        yield f"remove_image[{sid}]", "image", sid, remove_image(deck, s, **kw(is_dm, urls))
        s = A("add_slide", sid + 1)
        yield f"insert_after[{sid}]", "insert", sid, insert_slide(deck, s, copy.deepcopy(ADDED_BODY), **kw(is_dm, urls))
    nums = [i + 1 for i in deck.ids]
    orders = [nums[::-1], nums[1:] + nums[:1], nums[-1:] + nums[:-1], [nums[2], nums[1]] + [x for x in nums if x not in (nums[1], nums[2])]]
    rnd = random.Random(7)
    for _ in range(10):
        o = nums[:]
        rnd.shuffle(o)
        orders.append(o)
    orders.append(nums[::2])  # partial
    orders.append([99, 0, -3] + nums[:2])  # junk numbers
    for o in orders:
        yield f"reorder{o}", "reorder", None, reorder(deck, A("reorder", order=o), **kw(is_dm, urls))


# ------------------------------------------------------------------- AC1+AC2
def test_ac1_ac2_every_op_every_slide(picks):
    seen = {"applied": 0, "refused": 0}
    for name, is_dm, deck, urls in all_decks(picks):
        for label, kind, sid, step in all_ops(deck, is_dm, urls):
            lab = f"{name} {label}"
            check_step(
                deck, step, is_dm=is_dm,
                removed=sid if kind == "remove" else None,
                stripped=sid if kind == "image" else None,
                inserted_after=sid if kind == "insert" else None,
                label=lab,
            )
            seen["applied" if step.outcome.code is APPLIED else "refused"] += 1
    assert seen["applied"] > 50 and seen["refused"] > 10, seen


def test_ac1_inputs_never_mutated(picks):
    for name, is_dm, deck, urls in all_decks(picks):
        snap = (tuple(sha(s) for s in deck.slides), deck.ids, deck.next_id)
        list(all_ops(deck, is_dm, urls))
        assert (tuple(sha(s) for s in deck.slides), deck.ids, deck.next_id) == snap, name


# ----------------------------------------------------------------------- AC3
def test_ac3_hook_and_closing_removal_refused(picks):
    for name, is_dm, deck, urls in all_decks(picks):
        t = _types(deck.slides)
        st = remove_slide(deck, A("remove_slide", 1), **kw(is_dm, urls))
        assert (st.outcome.code, st.outcome.reason) == (OutcomeCode.REFUSED_INVARIANT, "hook_slide"), name
        assert st.deck is deck
        for i, x in enumerate(t):
            if x in CLOSING_TYPES:
                st = remove_slide(deck, A("remove_slide", i + 1), **kw(is_dm, urls))
                assert (st.outcome.code, st.outcome.reason) == (OutcomeCode.REFUSED_INVARIANT, "closing_slide"), (name, i)
                assert st.deck is deck


def test_ac3_below_min_refused(picks):
    # a real deck cut down to hook + one body + follow (the smallest legal deck)
    iid, is_dm, slides = picks.get("small") or next(iter(picks.values()))
    base = [slides[0], next(s for s in slides[1:] if s["type"] not in CLOSING_TYPES), next(s for s in slides if s["type"] == "follow")]
    assert len(base) == MIN_SLIDES
    deck = Deck.of(base)
    # DM: no sources slide can be added back, so hook+follow = 2 < MIN
    st = remove_slide(deck, A("remove_slide", 2), is_dm=True, source_urls=[])
    assert (st.outcome.code, st.outcome.reason) == (OutcomeCode.REFUSED_INVARIANT, "min_slides")
    assert st.deck is deck
    # channel with urls: the closing pass legitimately restores a sources slide -> still >= MIN
    st = remove_slide(deck, A("remove_slide", 2), is_dm=False, source_urls=urls_of(slides))
    assert st.outcome.code is APPLIED and len(st.deck) >= MIN_SLIDES
    # real small deck, if present, must never drop below MIN
    if "small" in picks:
        d = Deck.of(picks["small"][2])
        for sid in d.ids:
            s = remove_slide(d, A("remove_slide", sid + 1), **kw(picks["small"][1], urls_of(picks["small"][2])))
            assert s.outcome.code is not APPLIED or len(s.deck) >= MIN_SLIDES


@pytest.mark.parametrize("ctype", list(CLOSING_TYPES))
def test_ac3_insert_closing_type_refused(picks, ctype):
    for name, is_dm, deck, urls in all_decks(picks):
        if len(deck) >= MAX_SLIDES:
            continue
        st = insert_slide(deck, A("add_slide", 2), {"type": ctype, "headline": "x", "urls": ["https://x.y"], "links": [], "sub": "s"}, **kw(is_dm, urls))
        assert st.outcome.code is OutcomeCode.REFUSED_INVARIANT, (name, ctype, st.outcome)
        assert st.deck is deck


def test_ac3_insert_at_max_refused(picks):
    iid, is_dm, slides = picks["max10"]
    deck = Deck.of(slides)
    st = insert_slide(deck, A("add_slide", 2), copy.deepcopy(ADDED_BODY), **kw(is_dm, urls_of(slides)))
    assert (st.outcome.code, st.outcome.reason) == (OutcomeCode.REFUSED_INVARIANT, "max_slides")
    assert st.deck is deck


def test_ac3_legacy_no_follow_trim_is_closing_clamp(picks):
    # real legacy deck padded with its own real body slides up to MAX (no follow), then
    # any op that would need the follow slide to be squeezed in must refuse, not trim.
    iid, is_dm, slides = picks["legacy_channel"]
    body = [s for s in slides if s["type"] not in CLOSING_TYPES]
    tail = [s for s in slides if s["type"] in CLOSING_TYPES]
    padded = list(body)
    k = 1
    while len(padded) + len(tail) < MAX_SLIDES:
        padded.append(copy.deepcopy(body[k % (len(body) - 1) + 1]))
        k += 1
    deck = Deck.of(padded + tail)
    assert len(deck) == MAX_SLIDES and "follow" not in _types(deck.slides)
    urls = urls_of(slides)
    # remove_slide frees a slot, so it must succeed and gain a follow
    ok = remove_slide(deck, A("remove_slide", 2), **kw(is_dm, urls))
    assert ok.outcome.code is APPLIED and _types(ok.deck.slides)[-1] == "follow" and len(ok.deck) == MAX_SLIDES
    # no-slot-freeing ops: refused with closing_clamp, same Deck
    seen = 0
    for sid in deck.ids:
        if deck.slides[deck.position(sid)].get("image"):
            st = remove_image(deck, A("remove_image", sid + 1), **kw(is_dm, urls))
            assert st.deck is deck or len(st.deck) <= MAX_SLIDES
            if st.outcome.code is OutcomeCode.REFUSED_INVARIANT:
                assert st.outcome.reason in ("closing_clamp", "bodyless"), st.outcome
                seen += 1
    nums = [i + 1 for i in deck.ids]
    st = reorder(deck, A("reorder", order=nums[::-1]), **kw(is_dm, urls))
    assert st.outcome.code is OutcomeCode.REFUSED_INVARIANT and st.outcome.reason == "closing_clamp", st.outcome
    assert st.deck is deck


def test_ac3_remove_image_bodyless_on_photo(picks):
    hits = 0
    for name, is_dm, deck, urls in all_decks(picks):
        for sid, s in zip(deck.ids, deck.slides, strict=True):
            if s["type"] == "photo" and s.get("image"):
                st = remove_image(deck, A("remove_image", sid + 1), **kw(is_dm, urls))
                assert (st.outcome.code, st.outcome.reason) == (OutcomeCode.REFUSED_INVARIANT, "bodyless"), (name, sid, st.outcome)
                assert st.deck is deck
                hits += 1
    assert hits >= 1


# ----------------------------------------------------------------------- AC4
def test_ac4_random_sequences(picks):
    decks = list(all_decks(picks))
    rnd = random.Random(297)
    applied = 0
    for seq in range(600):
        name, is_dm, deck, urls = rnd.choice(decks)
        for step_no in range(rnd.randint(1, 8)):
            lab = f"seq{seq}.{step_no} {name}"
            kind = rnd.choice(["remove", "remove", "image", "insert", "reorder", "junk"])
            ids = list(deck.ids)
            if kind == "junk":
                n = rnd.choice([0, -1, 99, deck.next_id + 3, deck.next_id])
                op = rnd.choice(["remove_slide", "remove_image"])
                if (n - 1) in deck.ids:
                    continue  # a live id is not junk
                fn = DECK_OPS[op]
                st = fn(deck, A(op, n), **kw(is_dm, urls))
                assert st.outcome.code is not APPLIED, (lab, op, n, st.outcome)
                assert st.deck is deck, lab
                continue
            sid = rnd.choice(ids)
            if kind == "remove":
                st = remove_slide(deck, A("remove_slide", sid + 1), **kw(is_dm, urls))
                check_step(deck, st, is_dm=is_dm, removed=sid, label=lab)
            elif kind == "image":
                st = remove_image(deck, A("remove_image", sid + 1), **kw(is_dm, urls))
                check_step(deck, st, is_dm=is_dm, stripped=sid, label=lab)
            elif kind == "insert":
                tgt = rnd.choice(list(range(deck.next_id)))
                st = insert_slide(deck, A("add_slide", tgt + 1), copy.deepcopy(ADDED_BODY), **kw(is_dm, urls))
                check_step(deck, st, is_dm=is_dm, inserted_after=tgt, label=lab)
            else:
                nums = [i + 1 for i in ids]
                rnd.shuffle(nums)
                st = reorder(deck, A("reorder", order=nums[: rnd.randint(1, len(nums))]), **kw(is_dm, urls))
                check_step(deck, st, is_dm=is_dm, label=lab)
            if st.outcome.code is APPLIED:
                applied += 1
                deck = st.deck
    assert applied > 400, applied


# ----------------------------------------------------------------------- AC5
def _item(**kwargs):
    base = dict(id=7, source="channel", status=Status.COMPOSED)
    base.update(kwargs)
    return Item(**base)


def test_ac5_valid_theme_pins_copy(picks):
    iid, _, slides = picks["news_nophoto"]
    item = _item(slides=copy.deepcopy(slides), theme="signal")
    before = copy.deepcopy(item)
    names = render.available_themes()
    assert names
    for n in names:
        if n in render.TEXT_ONLY_THEMES:
            continue
        st = set_theme(item, A("set_theme", theme=n))
        assert st.outcome.code is APPLIED and st.outcome.reason == "", n
        assert st.item.theme_pin == n and st.item is not item and st.deck is None
        assert item == before and item.theme_pin is None
    st = set_theme(item, A("set_theme", theme=names[0].upper()))
    assert st.item.theme_pin == names[0]  # case-insensitive match returns canonical name


@pytest.mark.parametrize("bad", ["../x", "aurora/../../etc", "/etc/passwd", "aurora.json", "..", "nope", "", "  ", "aurora\x00", "signal/", "..\\x", "aur ora"])
def test_ac5_bad_theme_dropped(picks, bad):
    item = _item(slides=[], theme_pin="signal")
    st = set_theme(item, A("set_theme", theme=bad))
    assert st.outcome.code is OutcomeCode.DROPPED_INVALID, (bad, st.outcome)
    assert st.item is item and item.theme_pin == "signal"


def test_ac5_text_only_theme_on_photo_deck_warns(picks):
    assert render.TEXT_ONLY_THEMES
    iid, _, slides = picks["news_photo"]
    item = _item(slides=copy.deepcopy(slides))
    for n in render.TEXT_ONLY_THEMES:
        st = set_theme(item, A("set_theme", theme=n))
        assert st.outcome.code is APPLIED and st.outcome.reason == "text_only_theme_with_photo"
        assert st.item.theme_pin == n and item.theme_pin is None
    # no photo -> no warning
    iid, _, slides = picks["news_nophoto"]
    st = set_theme(_item(slides=copy.deepcopy(slides)), A("set_theme", theme=next(iter(render.TEXT_ONLY_THEMES))))
    assert st.outcome.code is APPLIED and st.outcome.reason == ""


# ----------------------------------------------------------------------- AC6
def head_expression(item, configured):
    """HEAD:src/pipeline/stages/render.py render(): the pre-T297 inline logic."""
    configured = (configured or "").strip()
    if configured in ("rotate", "random"):
        return configured
    return configured or item.theme or ""


def test_ac6_head_logic_is_what_we_think():
    head = subprocess.run(["git", "show", "HEAD:src/pipeline/stages/render.py"], cwd=ROOT, capture_output=True, text=True)
    if head.returncode:
        pytest.skip("no git HEAD")
    assert 'configured in ("rotate", "random")' in head.stdout and "configured or item.theme" in head.stdout


@pytest.mark.parametrize("configured", ["", "  ", None, "rotate", "random", " rotate ", "signal", " aurora ", "Rotate", "nonexistent"])
@pytest.mark.parametrize("composer", [None, "", "tidal", "ember", "bogus"])
def test_ac6_unpinned_matches_head(configured, composer):
    item = _item(theme=composer)
    assert render.wanted_theme(item, configured) == head_expression(item, configured)
    item2 = _item(theme=composer, theme_pin="")
    assert render.wanted_theme(item2, configured) == head_expression(item2, configured)
    item3 = _item(theme=composer, theme_pin="   ")
    assert render.wanted_theme(item3, configured) == head_expression(item3, configured)


@pytest.mark.parametrize("configured", ["", "rotate", "random", "signal", "aurora"])
def test_ac6_pin_beats_config(configured):
    for name in render.available_themes():
        assert render.wanted_theme(_item(theme="tidal", theme_pin=name), configured) == name


@pytest.mark.parametrize("pin", ["ghost", "../signal", "signal.json", "Signal", "rotate", "random"])
@pytest.mark.parametrize("configured", ["", "rotate", "aurora"])
def test_ac6_unknown_pin_falls_through(pin, configured):
    item = _item(theme="tidal", theme_pin=pin)
    assert render.wanted_theme(item, configured) == head_expression(item, configured)


def test_ac6_resolves_to_file_with_pin():
    p = render.resolve_theme_path(render.wanted_theme(_item(theme_pin="ember"), "rotate"), item_id=7)
    assert p is not None and p.stem == "ember"
