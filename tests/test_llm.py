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


async def test_openrouter_rate_limit_is_retryforever_not_terminal(
    openrouter_settings, fake_http,
):
    """429 means "not now", so the item defers without spending an attempt.

    Classifying it as Terminal would permanently fail a queue of perfectly
    good items during a few minutes of upstream load.
    """
    fake_http.respond(429, {"error": {"message": "rate limit exceeded"}})
    with pytest.raises(Retryforever):
        await LLMClient(openrouter_settings, fake_http).cheap("s", "u")


@pytest.mark.parametrize("status", [401, 402, 403])
async def test_openrouter_auth_and_credit_failures_are_terminal(
    openrouter_settings, fake_http, status,
):
    """A bad key or an empty balance never fixes itself; retrying only delays
    the alert the operator actually needs."""
    fake_http.respond(status, {"error": {"message": "User not found"}})
    with pytest.raises(Terminal):
        await LLMClient(openrouter_settings, fake_http).cheap("s", "u")


async def test_openrouter_unparseable_json_is_retryable(openrouter_settings, fake_http):
    fake_http.respond(or_reply("here you go: not json"))
    with pytest.raises(Retryable, match="unparseable"):
        await LLMClient(openrouter_settings, fake_http).cheap("s", "u", schema=SCHEMA)


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
