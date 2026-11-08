"""UX-5: failure and stuck-item recovery on the item page (and the pure explain_error helper)."""

from __future__ import annotations

import pytest

from pipeline.models import Status
from tests.test_dashboard_shell import _load, SCRIPT
from tests.test_dashboard_ux4_detail import PAYLOAD, PAYLOAD_ESC, Req, html_of, make


@pytest.fixture
def dash():
    return _load(SCRIPT, "dashboard_ux5_under_test")

CANARY = "CANARY-/Users/x/secret.py http://127.0.0.1:11434"


async def fail_at(db, stage, error):
    i = await make(db, stage)
    await db.record_failure(i, error, terminal=True)
    return i


# ------------------------------------------------------------------ explain_error (pure)

REAL = [
    ("Retryable: only 1 of 5 researchers produced notes; need at least 2", "web research"),
    ('Retryable: openrouter 404: {"error":{"message":"Provider returned error"}}', "provider"),
    ("instagram 400: The caption was too long.", "caption"),
    ("container 17911642596592870 failed: ERROR", "Instagram"),
    ("Retryable: R2 upload failed for items/3/a.png: Could not connect to the endpoint URL", "storage"),
    ("ollama 400: request (22153 tokens) exceeds the available context size (8192 tokens)", "too long"),
    ("Retryable: openrouter returned an empty completion:", "empty"),
    ("Retryable: model returned unparseable JSON: '**Entity:**'", "format"),
    ("ReadTimeout: took too long, timed out", "too long to answer"),
]


@pytest.mark.parametrize(("raw", "expect"), REAL)
def test_known_errors_map_to_a_plain_sentence(dash, raw, expect):
    out = dash.explain_error(raw)
    assert expect in out and out != dash.GENERIC_REASON
    assert raw not in out


def test_unknown_empty_and_hostile_input_never_leak(dash):
    assert dash.explain_error("zzz " + CANARY) == dash.GENERIC_REASON
    assert dash.explain_error(None) == dash.explain_error("  \n") == dash.NO_ERROR_REASON
    for text in (PAYLOAD, "x" * 100000, "\x00" * 10):
        assert dash.explain_error(text) in {s for _, s in dash.ERROR_REASONS} | {dash.GENERIC_REASON}
    # specific beats general: a caption error that also says 'instagram 400'
    assert "caption" in dash.explain_error("instagram 400: caption too long")


# ------------------------------------------------------------------ failed item

async def test_failed_item_shows_stage_reason_retry_and_hides_raw_text(dash, db):
    i = await fail_at(db, Status.TRIAGED, "Retryable: only 0 of 5 researchers produced notes " + CANARY)
    html = await html_of(dash, db, i)
    top = html.split("<h2>Technical details</h2>")[0]
    assert "Failed while researching" in top and "did not find enough usable sources" in top
    assert "CANARY" not in top and "127.0.0.1" not in top
    assert (f"<form method=post action='/item/{i}/requeue' data-confirm='{dash.esc(dash.RETRY_CONFIRM)}'>"
            "<input type=hidden name=to value='triaged'>"
            "<button class='btn go'>Retry: researching</button></form>") in top and "href='#stage'" in top and "id=stage" in html
    assert "CANARY" in html.split("<h2>Technical details</h2>")[1]  # raw kept, folded, escaped


@pytest.mark.parametrize(("stage", "phrase"), [
    (Status.EXTRACTED, "scoring the story"), (Status.COMPOSED, "rendering the slides"),
    (Status.PUBLISHING, "publishing to Instagram"), (Status.APPROVED, "uploading the images"),
])
async def test_retry_targets_the_failed_stage(dash, db, stage, phrase):
    i = await fail_at(db, stage, "boom")
    html = await html_of(dash, db, i)
    assert (f"<input type=hidden name=to value='{stage.value}'>"
            f"<button class='btn go'>Retry: {phrase}</button>") in html


async def test_failed_without_a_known_stage_offers_no_one_click_retry(dash, db):
    i = await make(db, Status.FAILED, last_error="boom")
    await db.conn.execute("DELETE FROM events WHERE item_id=?", (i,))   # no failed event recorded
    html = await html_of(dash, db, i)
    assert "<h2>Failed</h2>" in html and "Retry:" not in html and "Other stages" in html
    j = await fail_at(db, Status.AWAITING_APPROVAL, "boom")  # not a requeue target
    assert "Retry:" not in await html_of(dash, db, j)


async def test_failed_with_no_error_text_is_handled(dash, db):
    i = await fail_at(db, Status.TRIAGED, "")
    assert dash.NO_ERROR_REASON in await html_of(dash, db, i)


async def test_failed_here_marker_has_text_and_replaces_now(dash, db):
    i = await fail_at(db, Status.RESEARCHED, "boom")
    html = await html_of(dash, db, i)
    assert html.count("failed here</span>") == 1 and "tl-node fail" in html
    assert "tl-node now" not in html and "tl-node done" in html and "tl-node todo" in html
    # the marker sits on the failed stage, not elsewhere
    node = html.split("tl-node fail", 1)[1].split("</button>", 1)[0]
    assert ">researched<" in node


def test_timeline_signature_stays_backward_compatible(dash):
    from types import SimpleNamespace
    item = SimpleNamespace(id=1, status="triaged")
    assert "tl-node now" in dash.timeline_control(item)


async def test_repeated_failure_uses_the_latest_event(dash, db):
    i = await fail_at(db, Status.TRIAGED, "boom")
    await db.transition(i, Status.COMPOSED)
    await db.record_failure(i, "boom again", terminal=True)
    html = await html_of(dash, db, i)
    assert "Retry: rendering the slides" in html and "Retry: researching" not in html


# ------------------------------------------------------------------ other states

async def test_needs_input_question_comes_first_with_autofocus_and_one_form(dash, db):
    i = await make(db, Status.NEEDS_INPUT, question="Which year did <b>this</b> happen?")
    html = await html_of(dash, db, i)
    assert html.index("Needs your answer</h2>") < html.index("id=changes")
    assert "Which year did &lt;b&gt;this&lt;/b&gt; happen?" in html
    assert html.count("autofocus") == 1 and html.count(f"action='/item/{i}/say'") == 1


async def test_question_text_is_escaped(dash, db):
    i = await make(db, Status.NEEDS_INPUT, question=PAYLOAD)
    html = await html_of(dash, db, i)
    assert f"<p class=question>{PAYLOAD_ESC}</p>" in html
    assert html.count("onerror=alert(1)") == html.count(PAYLOAD_ESC)   # only ever the escaped form


async def test_other_statuses_keep_the_chat_form_and_no_autofocus(dash, db):
    i = await make(db, Status.AWAITING_APPROVAL)
    html = await html_of(dash, db, i)
    assert html.count(f"action='/item/{i}/say'") == 1 and "autofocus" not in html
    assert "Needs your answer</h2>" not in html


async def test_running_item_that_errored_gets_a_quiet_retrying_note(dash, db):
    i = await make(db, Status.TRIAGED)
    await db.update_fields(i, {"last_error": "Retryable: openrouter 404 " + CANARY})
    html = await html_of(dash, db, i)
    top = html.split("<h2>Technical details</h2>")[0]
    assert "Last attempt hit a problem" in top and "tried again automatically" in top
    assert "CANARY" not in top


async def test_waiting_revision_shows_only_the_closed_sentence(dash, db):
    i = await make(db, Status.REVISION_REQUESTED)
    await db.update_fields(i, {"last_error": "ReadTimeout " + CANARY})
    html = await html_of(dash, db, i)
    assert "CANARY" not in html and dash.rcopy.REVISION_WAITING_SUMMARY in html.split("<h2>Technical details")[0]


async def test_clean_items_have_no_failure_card(dash, db):
    for status in (Status.AWAITING_APPROVAL, Status.PUBLISHED, Status.TRIAGED):
        html = await html_of(dash, db, await make(db, status))
        assert "class='card failure'" not in html and "Last attempt" not in html


# ------------------------------------------------ a provider body reaches no page outside Technical details

PROVIDER_BODY = (
    'Retryable: openrouter 404: {"error":{"message":"Provider returned error","code":404,'
    '"metadata":{"raw":"{\\"detail\\":\\"CANARY-PROVIDER-BODY /srv/key=sk-or-LEAK\\"}"}}}'
)


async def test_provider_error_body_is_absent_from_overview_list_and_item(dash, db):
    from tests.test_dashboard_ux2_overview import FakeRequest as OvReq
    i = await fail_at(db, Status.TRIAGED, PROVIDER_BODY)
    held = await make(db, Status.APPROVED)
    from datetime import datetime, timedelta, timezone
    soon = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    await db.conn.execute("UPDATE items SET next_attempt_at=?, last_error=? WHERE id=?", (soon, PROVIDER_BODY, held))
    await db.conn.commit()

    overview = (await dash.overview(OvReq(db))).text
    lst = (await dash.index(OvReq(db))).text
    item = (await dash.detail(Req(db, i))).text
    for name, page_html in (("/", overview), ("/items", lst)):
        for leak in ("CANARY-PROVIDER-BODY", "sk-or-LEAK", "openrouter 404", "metadata", "Retryable"):
            assert leak not in page_html, (name, leak)
    assert dash.explain_error(PROVIDER_BODY) in overview and dash.explain_error(PROVIDER_BODY) in lst
    outside = item.split("<h2>Technical details</h2>")[0]
    assert "CANARY-PROVIDER-BODY" not in outside and "sk-or-LEAK" not in outside
    assert "CANARY-PROVIDER-BODY" in item.split("<h2>Technical details</h2>")[1]  # folded, escaped


# ------------------------------------------------ bounded matching (no regex blow-up on hostile text)

@pytest.mark.parametrize("hostile", [
    "instagram 12 " * 8000,                      # ~100KB
    "instagram 1 caption " * 100,                # 2000 chars
    "instagram 1 " + "caption " * 20000,
    "openrouter " * 10000,
    "x" * 200000,
])
def test_explain_error_is_fast_on_adversarial_input(dash, hostile):
    import time
    start = time.perf_counter()
    for _ in range(5):
        assert dash.explain_error(hostile)
    assert (time.perf_counter() - start) / 5 < 0.05


def test_only_the_head_is_classified_and_json_is_not_a_catch_all(dash):
    cap = dash.ERROR_MATCH_CHARS
    late = "boom " * 700 + "researchers produced nothing"   # ~3.5KB in: far past the cap
    assert dash.explain_error(late) == dash.GENERIC_REASON            # beyond the cap: not read
    assert "web research" in dash.explain_error("researchers produced nothing" + " " * cap + "zzz")
    assert dash.explain_error("config.json not found") == dash.GENERIC_REASON
    assert "format" in dash.explain_error("json.decoder.JSONDecodeError: Expecting value")
    assert "caption" in dash.explain_error("instagram 400: The caption was too long.")


# ------------------------------------------------ the raw-error fold

async def test_raw_error_fold_holds_the_recorded_text_when_no_failure_event_exists(dash, db):
    i = await make(db, Status.TRIAGED)
    await db.update_fields(i, {"last_error": "Retryable: openrouter 404 FOLD-CANARY"})
    html = await html_of(dash, db, i)       # running + a deferral error: no failed event, so no other copy
    tech = html.split("<h2>Technical details</h2>")[1]
    assert "<summary>Error (as recorded)</summary><pre>Retryable: openrouter 404 FOLD-CANARY</pre>" in tech
    assert "FOLD-CANARY" not in html.split("<h2>Technical details</h2>")[0]
    j = await make(db, Status.TRIAGED)
    assert "Error (as recorded)" not in await html_of(dash, db, j)
