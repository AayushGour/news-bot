import pytest

from pipeline.errors import Retryable, Retryforever
from pipeline.llm import LLMClient, parse_json


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
