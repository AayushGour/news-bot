"""T259 / D235: config faults defer the item and try the spare, once-alerted."""
from dataclasses import replace

import pytest

from pipeline.errors import BadCompletion, ConfigFault, Retryforever
from pipeline.llm import LLMClient
from pipeline.models import Status
from pipeline.worker import Worker

MISSING = {"error": "model 'qwen-gone' not found"}
GONE = {"error": {"message": "No endpoints found for acme/gone:free.", "code": 404}}
OK_LOCAL = {"message": {"content": "from the spare"}}


def or_ok(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


# ------------------------------------------------------------- Ollama 404


async def test_ollama_404_is_a_config_fault_not_retryable(settings, fake_http):
    fake_http.respond(404, MISSING)
    with pytest.raises(ConfigFault, match="qwen-gone|no model"):
        await LLMClient(settings, fake_http).good("s", "u")
    assert len(fake_http.calls) == 1, "no blind retry of a missing model"


async def test_ollama_404_tries_the_configured_spare(settings, fake_http):
    s = replace(settings, model_good_fallback="spare-model")
    fake_http.respond_sequence([(404, MISSING), (200, OK_LOCAL)])
    out = await LLMClient(s, fake_http).good("s", "u")
    assert out == "from the spare"
    assert [c.json["model"] for c in fake_http.calls] == [s.model_good, "spare-model"]


async def test_ollama_404_without_spare_propagates(settings, fake_http):
    s = replace(settings, model_good_fallback="")
    fake_http.respond(404, MISSING)
    with pytest.raises(ConfigFault):
        await LLMClient(s, fake_http).good("s", "u")


async def test_ollama_other_4xx_stays_retryable(settings, fake_http):
    """Only 404 is a config fault; a 422 is still the item's request."""
    from pipeline.errors import Retryable
    fake_http.respond(422, {"error": "bad"})
    with pytest.raises(Retryable) as ei:
        await LLMClient(settings, fake_http).good("s", "u")
    assert not isinstance(ei.value, Retryforever)


# -------------------------------------------------- Ollama 200 non-JSON body


async def test_ollama_200_non_json_body_is_bad_completion(settings, fake_http):
    fake_http.respond("<html>proxy error</html>")
    with pytest.raises(BadCompletion):
        await LLMClient(settings, fake_http).cheap("s", "u")


async def test_ollama_200_without_message_key_is_bad_completion(settings, fake_http):
    fake_http.respond({"unexpected": 1})
    with pytest.raises(BadCompletion):
        await LLMClient(settings, fake_http).cheap("s", "u")


# ------------------------------------------------------------- OpenRouter


@pytest.fixture
def or_fallback(openrouter_settings):
    return replace(openrouter_settings, fallback_to_local=True,
                   model_good_fallback="spare-model")


@pytest.mark.parametrize("status,body", [
    (404, GONE),
    (400, {"error": {"message": "acme/gone:free is not a valid model ID"}}),
    (404, {"error": {"message": "Model is unavailable for free"}}),
])
async def test_openrouter_gone_model_is_a_config_fault_without_retries(
    openrouter_settings, fake_http, status, body,
):
    s = replace(openrouter_settings, fallback_to_local=False)
    fake_http.respond(status, body)
    with pytest.raises(ConfigFault):
        await LLMClient(s, fake_http).cheap("s", "u")
    assert len(fake_http.calls) == 1, "same slug fails identically; no 3x retry"


async def test_openrouter_gone_model_falls_to_local_then_spare(or_fallback, fake_http):
    fake_http.respond_sequence([
        (404, GONE),
        (404, MISSING),          # local primary also absent
        (200, OK_LOCAL),         # spare answers
    ])
    out = await LLMClient(or_fallback, fake_http).good("s", "u")
    assert out == "from the spare"
    models = [c.json["model"] for c in fake_http.calls]
    assert models[1:] == [or_fallback.model_good, "spare-model"]


async def test_chain_exhausted_on_config_faults_stays_a_config_fault(
    or_fallback, fake_http,
):
    fake_http.respond(404, GONE)  # default for every call, local too
    with pytest.raises(ConfigFault):
        await LLMClient(or_fallback, fake_http).good("s", "u")


async def test_schema_unsupported_404_is_not_misread_as_a_gone_model(
    openrouter_settings, fake_http,
):
    """The json_schema capability 404 must keep degrading, not become a fault."""
    fake_http.respond_sequence([
        (404, {"error": {"message": "No endpoints found that support json_schema"}}),
        (200, or_ok('{"a": 1}')),
    ])
    out = await LLMClient(openrouter_settings, fake_http).cheap(
        "s", "u", schema={"type": "object"})
    assert out == {"a": 1}


async def test_openrouter_terminal_statuses_untouched(openrouter_settings, fake_http):
    """401/402/403 wait on U1 and must stay Terminal."""
    from pipeline.errors import Terminal
    s = replace(openrouter_settings, fallback_to_local=False)
    fake_http.respond(402, {"error": {"message": "No endpoints found; pay up"}})
    with pytest.raises(Terminal):
        await LLMClient(s, fake_http).cheap("s", "u")


# -------------------------------------------------------------- alert once


async def test_alert_fires_once_per_fault_not_per_item(settings, fake_http):
    sent = []

    async def alert(text):
        sent.append(text)

    llm = LLMClient(replace(settings, model_good_fallback=""), fake_http)
    llm.on_config_fault = alert
    fake_http.respond(404, MISSING)
    for _ in range(4):
        with pytest.raises(ConfigFault):
            await llm.good("s", "u")
    assert len(sent) == 1 and "Config fault" in sent[0]


async def test_a_different_model_alerts_again(settings, fake_http):
    sent = []

    async def alert(text):
        sent.append(text)

    llm = LLMClient(replace(settings, model_good_fallback=""), fake_http)
    llm.on_config_fault = alert
    fake_http.respond(404, MISSING)
    for role in (llm.good, llm.cheap):
        with pytest.raises(ConfigFault):
            await role("s", "u")
    assert len(sent) == 2


async def test_a_failing_alert_does_not_mask_the_fault(settings, fake_http):
    async def alert(text):
        raise RuntimeError("telegram down")

    llm = LLMClient(replace(settings, model_good_fallback=""), fake_http)
    llm.on_config_fault = alert
    fake_http.respond(404, MISSING)
    with pytest.raises(ConfigFault):
        await llm.good("s", "u")


# ------------------------------------------- end to end: attempts unchanged


async def test_config_fault_through_the_worker_leaves_attempts_alone(
    db, settings, fake_http,
):
    s = replace(settings, model_good_fallback="")
    fake_http.respond(404, MISSING)
    llm = LLMClient(s, fake_http)
    i = await db.insert_item(source="dm", source_chat_id=1, source_msg_id=9, raw_text="x")

    async def stage(item):
        await llm.good("s", "u")

    worker = Worker(db, {Status.INGESTED: (stage, Status.TRIAGED)}, max_attempts=3)
    for _ in range(4):
        await db.clear_backoff(i)
        await worker.tick()
    item = await db.get_item(i)
    assert item.status == Status.INGESTED
    assert item.attempts == 0


async def test_capability_404_after_the_degrade_is_not_a_gone_model(
    openrouter_settings, fake_http,
):
    """With no schema (or once the degrade has been spent) the same wording
    must not be promoted to a ConfigFault -- it names a capability, not a slug."""
    s = replace(openrouter_settings, fallback_to_local=False)
    fake_http.respond(404, {"error": {"message": "No endpoints found that support json_schema"}})
    with pytest.raises(Exception) as ei:
        await LLMClient(s, fake_http).cheap("s", "u")
    assert not isinstance(ei.value, ConfigFault)
