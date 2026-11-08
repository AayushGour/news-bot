"""No gate fails open on an outage (T260, D235, coding-standards rule 17).

Each site used to turn "the infrastructure is down" into a normal-looking
answer: coverage said "covered", extraction said "no description", enumeration
asked the operator for "a better search term", and search said "no results".
Every test here drives the real stage through the real Worker and asserts the
four things deferral means: the fault reaches the worker as a deferral, the
item's attempts are unchanged, the operator was asked nothing, and nothing
degraded was persisted.
"""

import pytest

from pipeline.conversation import NeedsInput
from pipeline.coverage import unaddressed
from pipeline.errors import ConfigFault, Retryforever, Terminal
from pipeline.models import Item, Status
from pipeline.search import searx
from pipeline.stages.enumerate_items import enumerate_items
from pipeline.stages.extract import extract
from pipeline.worker import Worker


class Bot:
    """Records any message the worker tries to send the operator."""

    def __init__(self):
        self.sent = []

    async def send_message(self, *a, **k):
        self.sent.append((a, k))

    def __getattr__(self, name):
        async def record(*a, **k):
            self.sent.append((name, a, k))
        return record


async def _defers(db, settings, handler, **fields):
    """Run ``handler`` as INGESTED's stage; return the item after one tick."""
    i = await db.insert_item(
        source="dm", source_chat_id=1, source_msg_id=1,
        raw_text=fields.pop("raw_text", "10 awesome GitHub repositories for testing"),
    )
    if fields:
        await db.transition(i, Status.INGESTED, fields)
    bot = Bot()
    worker = Worker(
        db, {Status.INGESTED: (handler, Status.TRIAGED)},
        bot=bot, settings=settings,
    )
    await worker.tick()
    item = await db.get_item(i)
    assert item.status == Status.INGESTED, "item must stay where it was"
    assert item.attempts == 0, "an outage must not spend the item's attempts"
    assert bot.sent == [], "the operator must not be asked anything"
    assert item.extracted == {} and item.research == [], "nothing degraded persisted"
    return item


LIST_PLAN = {"intent": "list", "count": 8, "subject": "obscure thing",
             "queries": ["obscure thing"], "why": "list", "catalogue": "repos"}


# ------------------------------------------------------------------ coverage


class _CoverageLLM:
    def __init__(self, exc):
        self.exc = exc

    async def cheap(self, *a, **k):
        raise self.exc


@pytest.mark.parametrize("exc", [
    Retryforever("ollama down"), ConfigFault("model gone"), Terminal("bad key"),
])
async def test_coverage_outage_is_not_covered(exc):
    with pytest.raises(type(exc)):
        await unaddressed(_CoverageLLM(exc), ["an ask"], ["some material"])


async def test_coverage_outage_defers_through_the_worker(db, settings):
    async def stage(item):
        return {"clauses_missing": await unaddressed(
            _CoverageLLM(Retryforever("down")), ["ask"], ["m"])}

    await _defers(db, settings, stage)


async def test_coverage_own_fault_is_still_advisory():
    assert await unaddressed(_CoverageLLM(RuntimeError("bug")), ["a"], ["m"]) == []


# ------------------------------------------------------------------- extract


class _VisionLLM:
    def __init__(self, exc):
        self.exc = exc

    async def vision(self, *a, **k):
        raise self.exc


@pytest.mark.parametrize("exc", [Retryforever("down"), ConfigFault("model gone")])
async def test_vision_outage_defers_the_item(db, settings, fake_http, exc):
    async def stage(item):
        return await extract(
            Item(id=item.id, source="dm", status=Status.INGESTED,
                 raw_text="look", raw_media_paths=["/tmp/a.png"]),
            _VisionLLM(exc), fake_http,
        )

    await _defers(db, settings, stage)


async def test_vision_terminal_is_not_swallowed_into_a_marker(fake_http):
    """Terminal fails the item (the worker's job); it must not be hidden as a
    per-image error marker either."""
    item = Item(id=1, source="dm", status=Status.INGESTED, raw_text="look",
                raw_media_paths=["/tmp/a.png"])
    with pytest.raises(Terminal):
        await extract(item, _VisionLLM(Terminal("bad key")), fake_http)


async def test_vision_own_fault_still_degrades_with_a_marker(fake_http):
    item = Item(id=1, source="dm", status=Status.INGESTED, raw_text="look",
                raw_media_paths=["/tmp/a.png"])
    out = await extract(item, _VisionLLM(RuntimeError("bad image")), fake_http)
    assert "RuntimeError" in out["extracted"]["image_descriptions"][0]["error"]


# ----------------------------------------------------------------- enumerate


async def test_searxng_down_defers_enumeration_instead_of_asking(
    db, settings, fake_llm, fake_http,
):
    fake_llm.queue(LIST_PLAN)
    fake_http.raise_on_request = ConnectionError("searxng unreachable")

    async def stage(item):
        return await enumerate_items(item, fake_llm, fake_http, settings)

    await _defers(db, settings, stage)


async def test_genuine_nothing_found_still_asks_the_operator(
    fake_llm, fake_http, settings,
):
    """The other side of the rule: healthy engines, 200, no results."""
    fake_llm.queue(LIST_PLAN)
    fake_http.respond(200, {"results": []})
    with pytest.raises(NeedsInput):
        await enumerate_items(
            Item(id=1, source="dm", status=Status.TRIAGED, raw_text="x"),
            fake_llm, fake_http, settings,
        )


def _web_repo_results():
    return {"results": [
        {"url": f"https://site{n}.example.com/guide-to-obscure-thing",
         "title": "Obscure thing guide", "content": "obscure thing " * 30}
        for n in range(8)
    ]}


async def test_model_outage_during_web_extraction_defers_not_sources_deck(
    db, settings, fake_llm, fake_http,
):
    fake_llm.queue({**LIST_PLAN, "catalogue": "web"})
    fake_llm.queue({"queries": []})  # query expansion (cheap): no extras
    fake_llm.queue(Retryforever("ollama down"))  # THINGS extraction (good)
    fake_http.respond(200, _web_repo_results())

    async def stage(item):
        return await enumerate_items(item, fake_llm, fake_http, settings)

    await _defers(db, settings, stage)


# ------------------------------------------------------------------- searxng


class _Resp:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code, self._payload, self._bad = status, payload, bad_json

    def json(self):
        if self._bad:
            raise ValueError("<html>not json</html>")
        return self._payload


class _HTTP:
    def __init__(self, resp):
        self.resp = resp

    async def get(self, *a, **k):
        return self.resp


async def test_403_json_disabled_is_a_config_fault():
    with pytest.raises(ConfigFault):
        await searx(_HTTP(_Resp(403, {})), "http://x", "q")


async def test_429_is_an_outage():
    with pytest.raises(Retryforever):
        await searx(_HTTP(_Resp(429, {})), "http://x", "q")


async def test_a_200_that_is_not_json_is_an_outage():
    with pytest.raises(Retryforever):
        await searx(_HTTP(_Resp(200, bad_json=True)), "http://x", "q")


async def test_other_4xx_is_still_a_bad_query_not_an_outage():
    assert await searx(_HTTP(_Resp(400, {})), "http://x", "q") == []


@pytest.mark.parametrize("reason", [
    "Suspended: too many requests", "access denied", "CAPTCHA", "suspended: access denied",
])
async def test_category_query_with_a_blocked_engine_and_no_results_is_an_outage(reason):
    body = {"results": [], "unresponsive_engines": [["duckduckgo", reason]]}
    with pytest.raises(Retryforever):
        await searx(_HTTP(_Resp(200, body)), "http://x", "q", categories="general")


async def test_category_query_with_only_a_timeout_is_a_genuine_empty():
    body = {"results": [], "unresponsive_engines": [["duckduckgo", "timeout"]]}
    assert await searx(_HTTP(_Resp(200, body)), "http://x", "q",
                       categories="general") == []


async def test_blocked_engine_with_results_from_others_is_not_an_outage():
    hit = {"url": "https://a.example.com/x", "title": "t", "content": "c"}
    body = {"results": [hit], "unresponsive_engines": [["duckduckgo", "suspended"]]}
    out = await searx(_HTTP(_Resp(200, body)), "http://x", "q", categories="general")
    assert [r["url"] for r in out] == ["https://a.example.com/x"]


async def test_searx_403_defers_an_enumeration_through_the_worker(
    db, settings, fake_llm, fake_http,
):
    fake_llm.queue(LIST_PLAN)
    fake_http.respond(403, {})

    async def stage(item):
        return await enumerate_items(item, fake_llm, fake_http, settings)

    await _defers(db, settings, stage)


# --------------------------------------------- T260 review: N1, C2, EnginesBlocked


@pytest.mark.parametrize("bad", [5, "github", [5], [[]], {"a": 1}])
async def test_malformed_unresponsive_engines_never_raises_a_type_error(bad):
    """Unclassified exceptions spend attempts; a weird field must degrade to
    a plain empty result."""
    body = {"results": [], "unresponsive_engines": bad}
    assert await searx(_HTTP(_Resp(200, body)), "http://x", "q",
                       categories="general") == []


async def test_blocked_engine_raises_the_distinct_type():
    from pipeline.errors import EnginesBlocked

    body = {"results": [], "unresponsive_engines": [["google", "Suspended: CAPTCHA"]]}
    with pytest.raises(EnginesBlocked):
        await searx(_HTTP(_Resp(200, body)), "http://x", "q", categories="general")


async def test_enumeration_still_defers_on_a_blocked_engine(
    db, settings, fake_llm, fake_http,
):
    fake_llm.queue({**LIST_PLAN, "catalogue": "web"})
    fake_http.respond(200, {"results": [],
                            "unresponsive_engines": [["google", "Suspended: CAPTCHA"]]})

    async def stage(item):
        return await enumerate_items(item, fake_llm, fake_http, settings)

    await _defers(db, settings, stage)


async def test_search_403_alerts_the_operator_exactly_once(monkeypatch):
    from pipeline.faults import OnceAlerter
    import pipeline.search as search_mod

    sent = []

    async def send(text):
        sent.append(text)

    alerter = OnceAlerter()
    alerter.send = send
    monkeypatch.setattr(search_mod, "search_alerts", alerter)
    for _ in range(3):
        with pytest.raises(ConfigFault):
            await searx(_HTTP(_Resp(403, {})), "http://x", "q")
    assert len(sent) == 1 and "JSON output is disabled" in sent[0]


async def test_a_failing_alert_does_not_mask_the_config_fault(monkeypatch):
    from pipeline.faults import OnceAlerter
    import pipeline.search as search_mod

    async def send(text):
        raise RuntimeError("telegram down")

    alerter = OnceAlerter()
    alerter.send = send
    monkeypatch.setattr(search_mod, "search_alerts", alerter)
    with pytest.raises(ConfigFault):
        await searx(_HTTP(_Resp(403, {})), "http://x", "q")


# --------------------------------- T260 tester: reviewer's surviving mutants


async def test_s2_every_named_engine_down_is_a_plain_retryforever():
    """A named engine that is down is a service outage (defer, no ceiling
    question), not the ambiguous EnginesBlocked a category query raises."""
    from pipeline.errors import EnginesBlocked

    body = {"results": [], "unresponsive_engines": [["github", "timeout"]]}
    with pytest.raises(Retryforever) as exc:
        await searx(_HTTP(_Resp(200, body)), "http://x", "q", engines="github")
    assert not isinstance(exc.value, EnginesBlocked)


async def test_s2_named_engine_down_and_suspended_is_still_plain_retryforever():
    from pipeline.errors import EnginesBlocked

    body = {"results": [],
            "unresponsive_engines": [["github", "Suspended: too many requests"]]}
    with pytest.raises(Retryforever) as exc:
        await searx(_HTTP(_Resp(200, body)), "http://x", "q", engines="github")
    assert type(exc.value) is Retryforever and not isinstance(exc.value, EnginesBlocked)


@pytest.mark.parametrize("bad", [5, "x", {}, [5], [[]]])
async def test_s5_malformed_unresponsive_engines_with_named_engine_is_empty(bad):
    body = {"results": [], "unresponsive_engines": bad}
    assert await searx(_HTTP(_Resp(200, body)), "http://x", "q",
                       engines="github") == []


async def test_s7_one_element_entry_does_not_raise_or_block():
    body = {"results": [], "unresponsive_engines": [["google"]]}
    assert await searx(_HTTP(_Resp(200, body)), "http://x", "q",
                       categories="general") == []


async def test_s7_one_element_entry_for_the_named_engine_still_counts_as_down():
    body = {"results": [], "unresponsive_engines": [["github"]]}
    with pytest.raises(Retryforever):
        await searx(_HTTP(_Resp(200, body)), "http://x", "q", engines="github")


def test_w1_main_wires_search_alerts_to_the_llm_alert_callback():
    """`main()` builds Telegram/Telethon inline and has no seam to run without
    them, so pin the wiring in its source: search_alerts.send must be assigned
    from llm.on_config_fault, after the callback is set and before the Worker
    is built."""
    import inspect

    from pipeline import __main__ as entry

    src = inspect.getsource(entry._run)  # T348: the body moved out of main()
    wire = "search_alerts.send = llm.on_config_fault"
    assert wire in src
    assert src.index("llm.on_config_fault = ") < src.index(wire) < src.index("Worker(")
    assert entry.search_alerts is __import__("pipeline.faults", fromlist=["x"]).search_alerts


async def test_drill_403_across_three_items_alerts_once_and_spends_nothing(
    db, settings, fake_llm, fake_http, monkeypatch,
):
    """Blackbox drill through a real Worker: SearXNG answers 403 for three
    queued items. Each stays put with attempts 0, the operator is asked nothing
    by the worker, and the config-fault alert goes out exactly once."""
    from pipeline.faults import OnceAlerter
    import pipeline.search as search_mod

    alerts = []

    async def send(text):
        alerts.append(text)

    alerter = OnceAlerter()
    alerter.send = send
    monkeypatch.setattr(search_mod, "search_alerts", alerter)
    fake_http.respond(403, {})

    ids = [await db.insert_item(source="dm", source_chat_id=1, source_msg_id=n,
                                raw_text="10 awesome GitHub repositories for testing")
           for n in (1, 2, 3)]
    for i in ids:
        await db.transition(i, Status.INGESTED, {})

    async def stage(item):
        fake_llm.queue(LIST_PLAN)
        return await enumerate_items(item, fake_llm, fake_http, settings)

    bot = Bot()
    worker = Worker(db, {Status.INGESTED: (stage, Status.TRIAGED)},
                    bot=bot, settings=settings)
    for _ in ids:
        await worker.tick()
    for i in ids:
        item = await db.get_item(i)
        assert item.status == Status.INGESTED and item.attempts == 0
    assert bot.sent == []
    assert len(fake_http.calls) == 3, "all three items hit SearXNG"
    assert len(alerts) == 1 and "JSON output is disabled" in alerts[0]
