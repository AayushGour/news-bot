"""UX-6: confirms, toasts from whitelisted codes, and the styled error pages (real app + middleware)."""

from __future__ import annotations

import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pipeline.models import Status
from tests.test_dashboard_shell import SCRIPT, _load
from tests.test_dashboard_ux4_detail import PAYLOAD, make

HTML = {"Accept": "text/html,application/xhtml+xml"}


@pytest.fixture
def dash():
    return _load(SCRIPT, "dashboard_ux6_under_test")


@pytest.fixture
async def client(dash, settings, db):
    app = await dash.build(settings)

    async def boom(request):
        raise RuntimeError("SECRET-CANARY /Users/x/key sk-or-LEAK")

    app.router.add_get("/boom", boom)
    c = TestClient(TestServer(app))
    await c.start_server()
    yield c
    await c.close()


async def post(client, path, data=None, **headers):
    return await client.post(path, data=data or {}, allow_redirects=False, headers=headers)


# ------------------------------------------------------------------ confirms

async def test_decision_forms_carry_their_confirm(dash, client, db):
    i = await make(db, Status.AWAITING_APPROVAL)
    html = await (await client.get(f"/item/{i}")).text()
    for verb in ("approve", "reject", "drop"):
        assert f"action='/item/{i}/{verb}' data-confirm='{dash.esc(dash.DECISION_CONFIRM[verb])}'" in html
    assert "Instagram" in dash.DECISION_CONFIRM["approve"] and all(dash.DECISION_CONFIRM.values())


async def test_approving_an_already_published_item_warns_about_a_second_carousel(dash, client, db):
    i = await make(db, Status.AWAITING_APPROVAL)
    await db.update_fields(i, {"publish_log": [{"at": "2026-01-01T00:00:00", "ig_post_id": "9"}]})
    html = await (await client.get(f"/item/{i}")).text()
    assert f"action='/item/{i}/approve' data-confirm='{dash.esc(dash.DOUBLE_POST_CONFIRM)}'" in html


async def test_timeline_retry_and_list_drop_confirm(dash, client, db):
    i = await make(db, Status.TRIAGED)
    await db.record_failure(i, "boom", terminal=True)
    html = await (await client.get(f"/item/{i}")).text()
    assert html.count("class=tl-step data-confirm=") == len(dash.TARGETS)
    assert "Re-run from triaged?" in html
    assert "<input type=hidden name=to value='triaged'><button class='btn go'>Retry" in html
    assert f"action='/item/{i}/requeue' data-confirm='{dash.esc(dash.RETRY_CONFIRM)}'" in html
    j = await make(db, Status.TRIAGED)
    lst = await (await client.get("/items")).text()
    assert f"action='/item/{j}/drop' data-confirm='{dash.esc(dash.DECISION_CONFIRM['drop'])}'" in lst


# ------------------------------------------------------------------ toasts / redirects

@pytest.mark.parametrize(("status", "verb", "code"), [
    (Status.AWAITING_APPROVAL, "approve", "approved"),
    (Status.AWAITING_APPROVAL, "reject", "rejected"),
    (Status.TRIAGED, "drop", "dropped"),
    (Status.TRIAGED, "requeue", "requeued"),
    (Status.TRIAGED, "bump", "priority"),
    (Status.TRIAGED, "lower", "priority"),
])
async def test_every_verb_redirects_back_with_its_notice(client, db, status, verb, code):
    i = await make(db, status)
    r = await post(client, f"/item/{i}/{verb}", Referer=f"http://localhost/item/{i}")
    assert r.status == 302 and r.headers["Location"] == f"/item/{i}?notice={code}"


async def test_caption_regen_say_new_and_move_notices(client, db):
    i = await make(db, Status.AWAITING_APPROVAL, brief="b")
    r = await post(client, f"/item/{i}/caption", {"caption": "new words"}, Referer=f"/item/{i}")
    assert r.headers["Location"] == f"/item/{i}?notice=saved"
    r = await post(client, f"/item/{i}/regen", {"note": "x"}, Referer=f"/item/{i}")
    assert r.headers["Location"] == f"/item/{i}?notice=regen"
    j = await make(db, Status.TRIAGED)
    r = await post(client, f"/item/{j}/say", {"text": "hello"})
    assert r.headers["Location"] == f"/item/{j}?notice=sent"
    q = await make(db, Status.NEEDS_INPUT, question="q?")
    r = await post(client, f"/item/{q}/say", {"text": "answer"})
    assert r.headers["Location"] == f"/item/{q}?notice=answered"
    r = await post(client, f"/item/{j}/say", {"text": "  "})
    assert r.headers["Location"] == f"/item/{j}"           # nothing happened, so no toast
    r = await post(client, "/new", {"text": "A real story about a new open model release from a lab " * 3})
    assert r.status == 302 and r.headers["Location"].endswith("?notice=queued")
    r = await post(client, f"/queue/{j}/top")
    assert r.headers["Location"] == "/queue?notice=moved"


async def test_referer_is_reduced_to_a_local_path_and_old_notice_replaced(client, db):
    i = await make(db, Status.TRIAGED)
    r = await post(client, f"/item/{i}/bump", Referer="http://evil.example/items?lane=work&notice=sent")
    assert r.headers["Location"] == "/items?lane=work&notice=priority"          # host dropped, notice replaced
    for bad in ("//evil.example/x", "http://h//evil.example/x", "http://h/\\evil.example", "javascript:alert(1)", "", "/a b"):
        j = await make(db, Status.TRIAGED)
        r = await post(client, f"/item/{j}/bump", Referer=bad)
        loc = r.headers["Location"]
        assert loc.startswith("/") and not loc.startswith("//") and "evil" not in loc and "\n" not in loc, bad


async def test_notice_codes_render_only_from_the_whitelist(dash, client, db):
    i = await make(db, Status.AWAITING_APPROVAL)
    for path in (f"/item/{i}", "/", "/items", "/queue"):
        ok = await (await client.get(f"{path}?notice=approved")).text()
        assert "Approved - publishing." in ok and "role=status" in ok, path
        bad = await (await client.get(f"{path}", params={"notice": PAYLOAD})).text()
        assert "class='toast" not in bad and "onerror=alert(1)" not in bad, path
        none = await (await client.get(f"{path}?notice=nope")).text()
        assert "class='toast" not in none, path


# ------------------------------------------------------------------ error pages

async def test_missing_item_is_a_styled_404_for_browsers_and_plain_otherwise(client):
    r = await client.get("/item/999", headers=HTML)
    body = await r.text()
    assert r.status == 404 and "<h1>Not found</h1>" in body and "Back to the board" in body
    assert "no such item" not in body
    plain = await client.get("/item/999")
    assert plain.status == 404 and await plain.text() == "no such item"      # callers that don't ask for HTML
    unmatched = await client.get("/nothing/here", headers=HTML)
    assert unmatched.status == 404 and "Back to the board" in await unmatched.text()


async def test_conflict_page_names_the_current_state_and_links_back(client, db):
    i = await make(db, Status.PUBLISHED)
    r = await post(client, f"/item/{i}/approve", Accept="text/html")
    body = await r.text()
    assert r.status == 409 and "<h1>Nothing was changed</h1>" in body
    assert f"Item {i} is now:" in body and "data-status='published'" in body
    assert f"href='/item/{i}'>See item {i}</a>" in body and "Back to the board" in body
    assert "not awaiting approval" in body                                     # our own closed wording
    plain = await post(client, f"/item/{i}/approve")
    assert plain.status == 409 and "not awaiting approval" in await plain.text() and "<html" not in await plain.text()
    # the guard itself is untouched: nothing moved
    assert (await db.get_item(i)).status == Status.PUBLISHED.value


async def test_bad_request_page_does_not_echo_request_text(client, db):
    i = await make(db, Status.TRIAGED)
    r = await post(client, f"/item/{i}/requeue", {"to": PAYLOAD}, Accept="text/html")
    body = await r.text()
    assert r.status == 400 and "couldn&#x27;t be processed" in body and "onerror=alert(1)" not in body
    plain = await post(client, f"/item/{i}/requeue", {"to": PAYLOAD})
    assert plain.status == 400 and await plain.text() == "not a requeueable stage"   # nothing of the request comes back
    r = await post(client, f"/item/{i}/{'x' * 20}", Accept="text/html")
    assert r.status == 400 and "x" * 20 not in await r.text()
    r = await post(client, f"/queue/{i}/sideways", Accept="text/html")
    assert r.status in (400, 302)


async def test_unexpected_error_is_a_generic_500_logged_but_never_shown(client, caplog):
    with caplog.at_level(logging.ERROR):
        r = await client.get("/boom", headers=HTML)
    body = await r.text()
    assert r.status == 500 and "Something went wrong on our side" in body and "Back to the board" in body
    for leak in ("SECRET-CANARY", "/Users/x", "sk-or-LEAK", "RuntimeError", "Traceback"):
        assert leak not in body
    assert "SECRET-CANARY" in caplog.text            # server side only
    assert (await client.get("/boom")).status == 500   # non-browser callers keep aiohttp's behaviour


async def test_redirects_and_success_pass_through_the_middleware(client, db):
    i = await make(db, Status.TRIAGED)
    r = await post(client, f"/item/{i}/drop", Accept="text/html")
    assert r.status == 302
    assert (await client.get("/", headers=HTML)).status == 200


async def test_error_page_survives_a_dead_database(client, db, monkeypatch):
    async def dead(*a, **k):
        raise RuntimeError("db gone")
    monkeypatch.setattr(type(db), "status_counts", dead)
    r = await client.get("/item/1", headers=HTML)          # the handler fails, and so does the page's own count lookup
    body = await r.text()
    assert r.status == 500 and "Something went wrong on our side" in body and "db gone" not in body


def test_back_with_notice_rejects_header_injection_unit(dash):
    from types import SimpleNamespace
    req = SimpleNamespace(headers={"Referer": "/x\r\nSet-Cookie: a=b"})
    assert dash.back_with_notice(req, "saved").location == "/?notice=saved"
    req = SimpleNamespace(headers={})
    assert dash.back_with_notice(req, "saved", "/queue").location == "/queue?notice=saved"


async def test_double_slash_path_in_a_referer_never_becomes_a_protocol_relative_redirect(client, db):
    i = await make(db, Status.TRIAGED)
    r = await post(client, f"/item/{i}/bump", Referer="http://evil//evil2.com")
    assert r.headers["Location"] == "/?notice=priority"          # fell back, not "//evil2.com" or "?notice=..."


async def test_styled_405_keeps_the_allow_header(client):
    r = await client.post("/queue", headers=HTML, allow_redirects=False)
    assert r.status == 405 and "GET" in r.headers["Allow"] and "Back to the board" in await r.text()
    plain = await client.post("/queue", allow_redirects=False)
    assert plain.status == 405 and "GET" in plain.headers["Allow"]
