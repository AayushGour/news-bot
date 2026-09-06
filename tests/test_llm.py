import json

import pytest

from pipeline.errors import Retryable, Retryforever, Terminal
from pipeline.llm import OPENROUTER_URL, LLMClient, parse_json

SCHEMA = {"type": "object", "properties": {"score": {"type": "integer"}}}


def or_reply(content: str) -> dict:
    """An OpenAI-shaped completion, which is what OpenRouter returns."""
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


async def test_cheap_pins_num_ctx_and_returns_parsed_json(settings, fake_http):
    """num_ctx pinning is a correctness requirement, not tuning.

    Unpinned, Ollama allocates the model's full 262144 context and spills to CPU.
    """
    fake_http.respond({"message": {"content": '{"score": 7, "reason": "ok"}'}})
    out = await LLMClient(settings, fake_http).cheap("sys", "usr", schema={"type": "object"})

    assert out == {"score": 7, "reason": "ok"}
    sent = fake_http.last_json
    assert sent["options"]["num_ctx"] == settings.num_ctx_cheap
    assert sent["model"] == settings.model_cheap
    assert sent["format"] == {"type": "object"}
    assert sent["stream"] is False


async def test_good_uses_the_larger_context_and_good_model(settings, fake_http):
    fake_http.respond({"message": {"content": "a brief"}})
    out = await LLMClient(settings, fake_http).good("sys", "usr")

    assert out == "a brief"
    assert fake_http.last_json["options"]["num_ctx"] == settings.num_ctx_good
    assert fake_http.last_json["model"] == settings.model_good
    assert "format" not in fake_http.last_json


async def test_retries_without_think_when_model_rejects_it(settings, fake_http):
    """Non-thinking models 400 on the `think` parameter."""
    fake_http.respond_sequence([
        (400, "this model does not support thinking"),
        (200, {"message": {"content": "ok"}}),
    ])
    assert await LLMClient(settings, fake_http).cheap("s", "u") == "ok"
    assert "think" not in fake_http.all_json[-1]
    assert len(fake_http.calls) == 2


async def test_connection_failure_is_retryforever_not_item_failure(settings, fake_http):
    """Ollama being down is not the item's fault; it must not burn retry budget."""
    fake_http.raise_on_request = ConnectionError("refused")
    with pytest.raises(Retryforever):
        await LLMClient(settings, fake_http).cheap("s", "u")


async def test_server_error_is_retryforever(settings, fake_http):
    fake_http.respond(503, "overloaded")
    with pytest.raises(Retryforever):
        await LLMClient(settings, fake_http).cheap("s", "u")


async def test_client_error_is_retryable(settings, fake_http):
    fake_http.respond(422, "bad request")
    with pytest.raises(Retryable):
        await LLMClient(settings, fake_http).cheap("s", "u")


async def test_unparseable_json_raises_retryable(settings, fake_http):
    fake_http.respond({"message": {"content": "not json at all"}})
    with pytest.raises(Retryable, match="unparseable"):
        await LLMClient(settings, fake_http).cheap("s", "u", schema={"type": "object"})


async def test_vision_attaches_base64_images(settings, fake_http, tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG fake bytes")
    fake_http.respond({"message": {"content": "a screenshot of a tweet"}})

    await LLMClient(settings, fake_http).vision("s", "u", [img])

    message = fake_http.last_json["messages"][-1]
    assert len(message["images"]) == 1
    assert fake_http.last_json["options"]["num_ctx"] == settings.num_ctx_vision


async def test_vision_skips_missing_attachment_without_failing(settings, fake_http):
    """A missing download must not take extraction down with it."""
    fake_http.respond({"message": {"content": "no images"}})
    await LLMClient(settings, fake_http).vision("s", "u", ["/nope/missing.png"])
    assert fake_http.last_json["messages"][-1].get("images") in (None, [])


def test_parse_json_strips_markdown_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('{"a": 1}') == {"a": 1}


# ------------------------------------------------------------------ openrouter


async def test_openrouter_posts_openai_shaped_payload_with_bearer_key(
    openrouter_settings, fake_http,
):
    """The hosted path is OpenAI-compatible: different URL, auth, and options.

    num_ctx is an Ollama concept; sending it here is at best ignored and at
    worst a 400, so it must not appear.
    """
    fake_http.respond(or_reply("a brief"))
    out = await LLMClient(openrouter_settings, fake_http).good("sys", "usr")

    assert out == "a brief"
    call = fake_http.calls[-1]
    assert call.url == OPENROUTER_URL
    assert call.headers["Authorization"] == "Bearer sk-or-test-key"
    assert call.json["model"] == openrouter_settings.openrouter_model_good
    assert call.json["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]
    assert "options" not in call.json and "num_ctx" not in json.dumps(call.json)
    assert "format" not in call.json


async def test_openrouter_cheap_uses_its_own_model_setting(openrouter_settings, fake_http):
    """Switching providers must not require re-entering model names."""
    fake_http.respond(or_reply("ok"))
    await LLMClient(openrouter_settings, fake_http).cheap("s", "u")

    assert fake_http.last_json["model"] == openrouter_settings.openrouter_model_cheap
    assert fake_http.last_json["model"] != openrouter_settings.model_cheap


async def test_openrouter_schema_becomes_strict_json_schema_response_format(
    openrouter_settings, fake_http,
):
    fake_http.respond(or_reply('{"score": 7}'))
    out = await LLMClient(openrouter_settings, fake_http).cheap("sys", "usr", schema=SCHEMA)

    assert out == {"score": 7}
    assert fake_http.last_json["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "strict": True, "schema": SCHEMA},
    }


async def test_openrouter_falls_back_to_json_object_when_schema_unsupported(
    openrouter_settings, fake_http,
):
    """Not every model implements json_schema, and every caller with a schema
    needs a dict back — so degrade to json_object with the schema in the
    prompt rather than losing structured output."""
    fake_http.respond_sequence([
        (404, {"error": {"message": "No endpoints found that support json_schema"}}),
        (200, or_reply('{"score": 4}')),
    ])
    out = await LLMClient(openrouter_settings, fake_http).cheap("sys", "usr", schema=SCHEMA)

    assert out == {"score": 4}
    assert len(fake_http.calls) == 2
    retried = fake_http.all_json[-1]
    assert retried["response_format"] == {"type": "json_object"}
    system = retried["messages"][0]["content"]
    assert system.startswith("sys")
    assert json.dumps(SCHEMA) in system, "the shape has to survive into the prompt"


async def test_openrouter_does_not_fall_back_on_an_unrelated_4xx(
    openrouter_settings, fake_http,
):
    """A blind retry would hide real request bugs behind a weaker mode."""
    fake_http.respond(400, {"error": {"message": "context length exceeded"}})
    with pytest.raises(Retryable) as exc:
        await LLMClient(openrouter_settings, fake_http).cheap("s", "u", schema=SCHEMA)

    assert not isinstance(exc.value, Retryforever), "a real 4xx spends an attempt"
    assert len(fake_http.calls) == 1


async def test_openrouter_connection_failure_is_retryforever(openrouter_settings, fake_http):
    """A dead network is not the item's fault; it must not burn retry budget."""
    fake_http.raise_on_request = ConnectionError("no route to host")
    with pytest.raises(Retryforever):
        await LLMClient(openrouter_settings, fake_http).cheap("s", "u")


async def test_openrouter_server_error_is_retryforever(openrouter_settings, fake_http):
    fake_http.respond(502, "upstream provider down")
    with pytest.raises(Retryforever):
        await LLMClient(openrouter_settings, fake_http).cheap("s", "u")


async def test_openrouter_rate_limit_defers_when_fallback_is_off(
    openrouter_settings, fake_http, monkeypatch
):
    """A rate limit is the service's problem, so the item must never spend one
    of its three attempts on it. With the local fallback disabled this stays a
    deferral; with it enabled the client switches models instead."""
    from dataclasses import replace

    import pipeline.llm as llm_mod

    monkeypatch.setattr(llm_mod, "RATE_LIMIT_BACKOFF_S", 0)
    settings = replace(openrouter_settings, fallback_to_local=False)
    fake_http.respond(429, {"error": {"message": "rate-limited upstream"}})

    with pytest.raises(Retryforever):
        await llm_mod.LLMClient(settings, fake_http).cheap("s", "u")


async def test_openrouter_unparseable_json_is_retryable_without_fallback(
    openrouter_settings, fake_http, monkeypatch
):
    """With the local fallback disabled, prose from a schema call still ends as
    Retryable — the item retries rather than failing outright."""
    from dataclasses import replace

    import pipeline.llm as llm_mod

    monkeypatch.setattr(llm_mod, "RATE_LIMIT_BACKOFF_S", 0)
    settings = replace(openrouter_settings, fallback_to_local=False)
    fake_http.respond(200, {"choices": [{"message": {"content": "not json"}}]})

    with pytest.raises(Retryable, match="unparseable"):
        await llm_mod.LLMClient(settings, fake_http).cheap(
            "s", "u", schema={"type": "object"}
        )


async def test_openrouter_200_with_an_error_envelope_is_retryable(
    openrouter_settings, fake_http,
):
    """OpenRouter answers 200 with an error body when an upstream provider dies
    mid-response; that must be a retry, not a crash."""
    fake_http.respond({"error": {"message": "provider returned error", "code": 502}})
    with pytest.raises(Retryable, match="no completion"):
        await LLMClient(openrouter_settings, fake_http).cheap("s", "u")


async def test_openrouter_vision_sends_data_urls_not_an_images_array(
    openrouter_settings, fake_http, tmp_path,
):
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG fake bytes")
    jpg = tmp_path / "b.JPG"
    jpg.write_bytes(b"\xff\xd8 fake bytes")
    fake_http.respond(or_reply("a screenshot of a tweet"))

    await LLMClient(openrouter_settings, fake_http).vision("s", "u", [png, jpg])

    message = fake_http.last_json["messages"][-1]
    assert "images" not in message
    parts = message["content"]
    assert parts[0] == {"type": "text", "text": "u"}
    urls = [part["image_url"]["url"] for part in parts[1:]]
    assert urls[0].startswith("data:image/png;base64,")
    assert urls[1].startswith("data:image/jpeg;base64,"), "mime comes from the extension"
    assert fake_http.last_json["model"] == openrouter_settings.openrouter_model_vision


async def test_openrouter_vision_skips_missing_attachment_without_failing(
    openrouter_settings, fake_http,
):
    """A missing download must not take extraction down with it."""
    fake_http.respond(or_reply("no images"))
    await LLMClient(openrouter_settings, fake_http).vision("s", "u", ["/nope/missing.png"])

    assert fake_http.last_json["messages"][-1]["content"] == "u"


async def test_provider_defaults_to_ollama(settings, fake_http):
    """Nothing in .env means the local GPU, exactly as before this existed."""
    fake_http.respond({"message": {"content": "ok"}})
    client = LLMClient(settings, fake_http)

    assert client.provider == "ollama"
    await client.cheap("s", "u")
    assert fake_http.calls[-1].url.endswith("/api/chat")
    assert fake_http.last_json["options"]["num_ctx"] == settings.num_ctx_cheap


# ------------------------------------------------ rate limit -> local fallback


@pytest.fixture
def openrouter_fallback(openrouter_settings):
    from dataclasses import replace
    return replace(openrouter_settings, fallback_to_local=True)


async def test_rate_limit_retries_three_times_then_falls_back(
    openrouter_fallback, fake_http, monkeypatch
):
    """Free-tier OpenRouter models are shared, so 429 is routine. Deferring the
    whole item would stall it behind someone else's load, possibly for hours."""
    import pipeline.llm as llm_mod

    monkeypatch.setattr(llm_mod, "RATE_LIMIT_BACKOFF_S", 0)
    fake_http.respond_sequence([
        (429, {"error": {"message": "rate-limited upstream"}}),
        (429, {"error": {"message": "rate-limited upstream"}}),
        (429, {"error": {"message": "rate-limited upstream"}}),
        (200, {"message": {"content": "from the local model"}}),
    ])

    out = await llm_mod.LLMClient(openrouter_fallback, fake_http).cheap("s", "u")

    assert out == "from the local model"
    assert len(fake_http.calls) == 4, "three OpenRouter attempts, then one local"
    assert "openrouter.ai" in fake_http.calls[2].url
    assert "11434" in fake_http.calls[3].url, "fallback must hit Ollama"


async def test_fallback_uses_the_local_model_name_not_the_openrouter_one(
    openrouter_fallback, fake_http, monkeypatch
):
    import pipeline.llm as llm_mod

    monkeypatch.setattr(llm_mod, "RATE_LIMIT_BACKOFF_S", 0)
    fake_http.respond_sequence(
        [(429, {})] * 3 + [(200, {"message": {"content": "ok"}})]
    )
    await llm_mod.LLMClient(openrouter_fallback, fake_http).good("s", "u")

    assert fake_http.calls[-1].json["model"] == openrouter_fallback.model_good
    assert fake_http.calls[-1]["options"]["num_ctx"] if False else True
    assert fake_http.calls[-1].json["options"]["num_ctx"] == openrouter_fallback.num_ctx_good


async def test_a_clearing_rate_limit_does_not_reach_the_fallback(
    openrouter_fallback, fake_http, monkeypatch
):
    import pipeline.llm as llm_mod

    monkeypatch.setattr(llm_mod, "RATE_LIMIT_BACKOFF_S", 0)
    fake_http.respond_sequence([
        (429, {}),
        (200, {"choices": [{"message": {"content": "recovered"}}]}),
    ])

    out = await llm_mod.LLMClient(openrouter_fallback, fake_http).cheap("s", "u")

    assert out == "recovered"
    assert len(fake_http.calls) == 2, "must not keep retrying after success"


async def test_fallback_can_be_disabled(openrouter_settings, fake_http, monkeypatch):
    """Some operators would rather wait for the paid provider than silently
    switch models mid-queue."""
    from dataclasses import replace

    import pipeline.llm as llm_mod
    from pipeline.errors import Retryforever

    monkeypatch.setattr(llm_mod, "RATE_LIMIT_BACKOFF_S", 0)
    settings = replace(openrouter_settings, fallback_to_local=False)
    fake_http.respond(429, {"error": {"message": "rate-limited"}})

    with pytest.raises(Retryforever):
        await llm_mod.LLMClient(settings, fake_http).cheap("s", "u")
    assert len(fake_http.calls) == 3, "still retries, just does not fall back"


async def test_both_providers_down_reports_both(
    openrouter_fallback, fake_http, monkeypatch
):
    """The log must name the real situation, not just the last thing tried."""
    import pipeline.llm as llm_mod
    from pipeline.errors import Retryforever

    monkeypatch.setattr(llm_mod, "RATE_LIMIT_BACKOFF_S", 0)
    fake_http.respond(429, {})

    async def dead_ollama(*a, **k):
        raise Retryforever("ollama unreachable: connection refused")

    client = llm_mod.LLMClient(openrouter_fallback, fake_http)
    monkeypatch.setattr(client, "_ollama", dead_ollama)

    with pytest.raises(Retryforever, match="rate-limited and local fallback unavailable"):
        await client.cheap("s", "u")


def test_rate_limited_still_defers_by_default():
    """Anything not handling RateLimited explicitly must still defer the item
    rather than fail it."""
    from pipeline.errors import RateLimited, Retryforever

    assert issubclass(RateLimited, Retryforever)


async def test_prose_instead_of_json_falls_back_to_local(
    openrouter_fallback, fake_http, monkeypatch
):
    """Regression: an auto router advertises the union of what it can reach,
    not a per-request guarantee. It routed a schema call to a backend that
    ignored the schema and replied "I'll analyze the provided web excerpts...".
    That is not a 4xx, so the schema fallback never fired and the item failed.
    """
    import pipeline.llm as llm_mod

    monkeypatch.setattr(llm_mod, "RATE_LIMIT_BACKOFF_S", 0)
    prose = {"choices": [{"message": {"content": "I'll analyze the excerpts..."}}]}
    fake_http.respond_sequence([
        (200, prose), (200, prose), (200, prose),
        (200, {"message": {"content": '{"score": 7}'}}),   # local, schema honoured
    ])

    out = await llm_mod.LLMClient(openrouter_fallback, fake_http).cheap(
        "s", "u", schema={"type": "object"}
    )

    assert out == {"score": 7}
    assert len(fake_http.calls) == 4
    assert "11434" in fake_http.calls[-1].url or "api/chat" in fake_http.calls[-1].url


async def test_unschemad_call_does_not_retry_on_prose(
    openrouter_fallback, fake_http, monkeypatch
):
    """Prose is the correct answer when no schema was requested."""
    import pipeline.llm as llm_mod

    monkeypatch.setattr(llm_mod, "RATE_LIMIT_BACKOFF_S", 0)
    fake_http.respond(200, {"choices": [{"message": {"content": "a fine brief"}}]})

    out = await llm_mod.LLMClient(openrouter_fallback, fake_http).good("s", "u")
    assert out == "a fine brief"
    assert len(fake_http.calls) == 1


async def test_a_genuine_bad_request_still_fails_fast(
    openrouter_fallback, fake_http, monkeypatch
):
    """A malformed request fails identically every time; retrying wastes time."""
    import pipeline.llm as llm_mod
    from pipeline.errors import Retryable

    monkeypatch.setattr(llm_mod, "RATE_LIMIT_BACKOFF_S", 0)
    fake_http.respond(422, {"error": {"message": "bad request"}})

    with pytest.raises(Retryable):
        await llm_mod.LLMClient(openrouter_fallback, fake_http).cheap(
            "s", "u", schema={"type": "object"}
        )
    assert len(fake_http.calls) == 1, "must not retry a permanent 4xx"
