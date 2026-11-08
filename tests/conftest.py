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


@pytest.fixture(autouse=True)
def isolated_tunnel_origin(tmp_path, monkeypatch):
    """No test may read the real tunnel origin file.

    publish.tunnel resolves the media base from data/tunnel-origin.txt, which
    exists whenever a tunnel is running on this machine. A test that read it
    would pass or fail depending on whether the developer happened to have the
    pipeline up — and one did: the loopback-rejection test started passing a
    live tunnel hostname instead of the loopback it had configured.
    """
    from pipeline.publish import tunnel
    monkeypatch.setattr(tunnel, "ORIGIN_FILE", tmp_path / "no-tunnel.txt")


@pytest.fixture(autouse=True)
def fresh_publish_pacing(monkeypatch):
    """Publish pacing keeps process-wide state; no test may inherit another's."""
    from pipeline.publish import instagram
    monkeypatch.setattr(instagram, "_last_publish_mono", None)


@pytest.fixture(autouse=True)
def fresh_ig_alerts():
    """The Instagram throttle alerter is process-wide; start each test armed."""
    from pipeline.faults import ig_alerts
    ig_alerts._seen.clear()
    ig_alerts.send = None
    yield
    ig_alerts._seen.clear()
    ig_alerts.send = None


PUBLIC_TEST_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    """No test resolves a name for real: a literal is itself, any other name is public.

    The guarded fetchers (safe_download.check_url) resolve before every request;
    without this a fake host like ``okf.example`` would hit the network. Tests
    about the guard install their own table over it.
    """
    import ipaddress

    from pipeline import safe_download

    async def resolve(host: str, port: int) -> list[str]:
        try:
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            return [PUBLIC_TEST_IP]

    monkeypatch.setattr(safe_download, "_resolve", resolve)


class FakeResponse:
    def __init__(self, status_code: int, body: Any, headers: Any = None) -> None:
        self.status_code = status_code
        self._body = body
        self.explicit_headers = headers is not None
        self.text = body if isinstance(body, str) else json.dumps(body)
        # Only the media-host preflight reads headers; Meta's JSON endpoints
        # never look. Defaulting to an image keeps every publish test testing
        # what it means to test — a test about carousel containers should not
        # have to declare that the image host serves images. A test that cares
        # passes its own headers.
        self.headers = {"content-type": "image/png"} if headers is None else headers

    def json(self) -> Any:
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeStream:
    """``async with http.stream(...)`` over a FakeResponse. A string body is a page
    (text/html) unless the test declared its own headers."""

    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.status_code = response.status_code
        if response.explicit_headers:
            self.headers = response.headers
        else:
            self.headers = {"content-type": "text/html"} if isinstance(response._body, str) else response.headers

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def aiter_raw(self):
        body = self._response._body
        if isinstance(body, bytes):
            yield body
        else:
            yield (body if isinstance(body, str) else json.dumps(body)).encode()


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

    def stream(self, method: str, url: str, headers: Any = None, **kw) -> "FakeStream":
        """The guarded fetchers' surface (safe_download): same replay as ``get``."""
        self.calls.append(Call(method, url, headers=headers))
        return FakeStream(self._next(url))

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
    degraded_schema: dict | None = None


class FakeLLM:
    """Same surface as :class:`pipeline.llm.LLMClient`, no network.

    Responses are consumed in FIFO order. An empty queue is a test bug, so it
    raises loudly rather than returning something plausible.
    """

    def __init__(self) -> None:
        self.calls: list[LLMCall] = []
        self._queue: list[Any] = []
        # T224: verify() gets its own call log + queue, deliberately NOT the
        # shared ``calls``/``_queue`` above. It is a real, separate model
        # call (compose() makes it after its own good() call, when
        # settings.verify_enabled), but dozens of existing compose tests
        # assert on `fake_llm.calls[-1]`/`len(fake_llm.calls)` to inspect
        # compose's OWN prompt — those predate the verifier and are not
        # testing it. Keeping verify() off that shared list is what makes
        # "every pre-T224 compose test passes unchanged" actually true
        # rather than "true until a test happens to use the real `settings`
        # fixture" (verify_enabled defaults on).
        self.verify_calls: list[LLMCall] = []
        self._verify_queue: list[Any] = []

    def queue(self, response: Any) -> None:
        self._queue.append(response)

    def queue_each(self, responses: list[Any]) -> None:
        self._queue.extend(responses)

    def queue_verify(self, response: Any) -> None:
        """Queue a reply for the NEXT verify() call only (see __init__)."""
        self._verify_queue.append(response)

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
                    temperature: float = 0.3,
                    degraded_schema: dict | None = None) -> Any:
        self.calls.append(LLMCall("cheap", system, user, schema,
                                   degraded_schema=degraded_schema))
        return self._pop("cheap")

    async def good(self, system: str, user: str, schema: dict | None = None,
                   temperature: float = 0.5,
                   degraded_schema: dict | None = None) -> Any:
        self.calls.append(LLMCall("good", system, user, schema,
                                   degraded_schema=degraded_schema))
        return self._pop("good")

    async def vision(self, system: str, user: str, image_paths: list,
                     schema: dict | None = None, temperature: float = 0.2,
                     degraded_schema: dict | None = None) -> Any:
        self.calls.append(LLMCall("vision", system, user, schema, list(image_paths),
                                   degraded_schema=degraded_schema))
        return self._pop("vision")

    async def verify(self, system: str, user: str, schema: dict | None = None,
                     temperature: float = 0.3) -> Any:
        """T224: the verifier's own local-only call — see __init__ for why
        this uses its own log/queue rather than the shared one. A test that
        never calls ``queue_verify`` gets a loud empty-queue AssertionError
        here, which verify.py's own "never raises into compose" guarantee
        turns into a harmless status="unavailable" record — this is what
        keeps every pre-T224 compose test passing unchanged without ever
        knowing the verifier exists."""
        self.verify_calls.append(LLMCall("verify", system, user, schema))
        if not self._verify_queue:
            raise AssertionError(
                f"FakeLLM.verify() called with an empty queue "
                f"(after {len(self.verify_calls)} call(s)) -- use "
                "fake_llm.queue_verify(...)"
            )
        response = self._verify_queue.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture(autouse=True)
def isolated_lease(tmp_path, monkeypatch):
    """No test may read or write the real data/worker.lease (T348).

    Database.connect fences the host on a container's lease; a test that read
    the real file would fail whenever the Docker worker is running.
    """
    monkeypatch.setenv("LEASE_PATH", str(tmp_path / "test-worker.lease"))
