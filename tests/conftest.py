"""Shared fakes.

The whole point of these is that the test suite never touches Ollama, SearXNG,
Telegram, Cloudflare, or Instagram. Everything runs offline and in milliseconds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from pipeline.config import Settings
from pipeline.db import Database

TEST_ENV = {
    "TELEGRAM_API_ID": "12345",
    "TELEGRAM_API_HASH": "hash",
    "TELEGRAM_BOT_TOKEN": "bot:token",
    "OPERATOR_USER_ID": "424242",
    "CHANNEL_IDS": "-1001526709058",
}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.load(env={**TEST_ENV, "DB_PATH": str(tmp_path / "t.db")})


@pytest.fixture
def openrouter_settings(tmp_path) -> Settings:
    """The same settings with the hosted provider selected."""
    return Settings.load(env={
        **TEST_ENV,
        "DB_PATH": str(tmp_path / "t.db"),
        "LLM_PROVIDER": "openrouter",
        "OPENROUTER_API_KEY": "sk-or-test-key",
    })


@pytest.fixture
async def db(tmp_path):
    d = await Database(tmp_path / "t.db").connect()
    yield d
    await d.close()


# ------------------------------------------------------------------- HTTP fake


class FakeResponse:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self) -> Any:
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@dataclass
class Call:
    method: str
    url: str
    json: Any = None
    params: Any = None
    data: Any = None
    headers: Any = None


class FakeHTTP:
    """Records requests and replays queued responses."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._queue: list[FakeResponse] = []
        self._default = FakeResponse(200, {})
        self._by_url: dict[str, FakeResponse] = {}
        self.raise_on_request: Exception | None = None

    # -- setup helpers -----------------------------------------------------

    def respond(self, status_or_body: Any = 200, body: Any = None) -> None:
        if isinstance(status_or_body, int):
            self._default = FakeResponse(status_or_body, body if body is not None else {})
        else:
            self._default = FakeResponse(200, status_or_body)

    def respond_sequence(self, pairs: list[tuple[int, Any]]) -> None:
        self._queue = [FakeResponse(s, b) for s, b in pairs]

    def respond_for(self, url_fragment: str, body: Any, status: int = 200) -> None:
        self._by_url[url_fragment] = FakeResponse(status, body)

    # -- httpx-compatible surface -----------------------------------------

    async def post(self, url: str, json: Any = None, data: Any = None,
                   timeout: Any = None, headers: Any = None, **kw) -> FakeResponse:
        self.calls.append(Call("POST", url, json=json, data=data, headers=headers))
        return self._next(url)

    async def get(self, url: str, params: Any = None, timeout: Any = None,
                  headers: Any = None, **kw) -> FakeResponse:
        self.calls.append(Call("GET", url, params=params, headers=headers))
        return self._next(url)

    def _next(self, url: str) -> FakeResponse:
        if self.raise_on_request is not None:
            raise self.raise_on_request
        for fragment, response in self._by_url.items():
            if fragment in url:
                return response
        if self._queue:
            return self._queue.pop(0)
        return self._default

    # -- assertions --------------------------------------------------------

    @property
    def last_json(self) -> Any:
        return self.calls[-1].json

    @property
    def all_json(self) -> list[Any]:
        return [c.json for c in self.calls]


@pytest.fixture
def fake_http() -> FakeHTTP:
    return FakeHTTP()


# -------------------------------------------------------------------- LLM fake


@dataclass
class LLMCall:
    role: str
    system: str
    user: str
    schema: dict | None = None
    images: list = field(default_factory=list)


class FakeLLM:
    """Same surface as :class:`pipeline.llm.LLMClient`, no network.

    Responses are consumed in FIFO order. An empty queue is a test bug, so it
    raises loudly rather than returning something plausible.
    """

    def __init__(self) -> None:
        self.calls: list[LLMCall] = []
        self._queue: list[Any] = []

    def queue(self, response: Any) -> None:
        self._queue.append(response)

    def queue_each(self, responses: list[Any]) -> None:
        self._queue.extend(responses)

    def _pop(self, role: str) -> Any:
        if not self._queue:
            raise AssertionError(
                f"FakeLLM.{role}() called with an empty queue "
                f"(after {len(self.calls)} calls)"
            )
        response = self._queue.pop(0)
        # Queueing an exception makes that specific call fail, which is how
        # tests exercise partial-failure paths.
        if isinstance(response, BaseException):
            raise response
        return response

    async def cheap(self, system: str, user: str, schema: dict | None = None,
                    temperature: float = 0.3) -> Any:
        self.calls.append(LLMCall("cheap", system, user, schema))
        return self._pop("cheap")

    async def good(self, system: str, user: str, schema: dict | None = None,
                   temperature: float = 0.5) -> Any:
        self.calls.append(LLMCall("good", system, user, schema))
        return self._pop("good")

    async def vision(self, system: str, user: str, image_paths: list,
                     schema: dict | None = None, temperature: float = 0.2) -> Any:
        self.calls.append(LLMCall("vision", system, user, schema, list(image_paths)))
        return self._pop("vision")


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()
