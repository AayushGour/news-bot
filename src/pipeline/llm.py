"""Chat client for the pipeline's two inference backends.

Ollama (local, the default) and OpenRouter (hosted, OpenAI-compatible) live
behind one class because every stage calls exactly ``cheap`` / ``good`` /
``vision``, and the test suite swaps the whole thing for ``FakeLLM`` with the
same three methods. Adding a provider must never change that surface.

Every Ollama call pins ``num_ctx``. This is not tuning: unpinned, Ollama 0.33
loads a model at its full declared context (262144 for qwen3:4b-instruct),
reports ~43GB of allocation and spills to CPU, making the machine unusable.
Pinned to 8192 the same model is 3.9GB and fully GPU-resident. ``num_ctx`` is
an Ollama option with no OpenAI-API equivalent, so it is never sent there.

Failure classification is the other thing that must not drift between the two
paths: the worker defers a ``Retryforever`` without spending one of the item's
three attempts, and gives up permanently on a ``Terminal``. Mapping a rate
limit or a dead upstream to the wrong class turns a ten-minute outage into a
queue of permanently failed items.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import (
    BadCompletion,
    RateLimited,
    Retryable,
    Retryforever,
    Terminal,
)

#: How many times to try OpenRouter before falling back to the local model.
OPENROUTER_ATTEMPTS = 3
#: Multiplied by the attempt number, so waits are 5s then 10s.
OPENROUTER_BACKOFF_S = 5

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

#: Hard ceiling on one model call, wall-clock.
#:
#: httpx applies its ``timeout`` to connect, read and write individually, and
#: the read timeout measures the gap BETWEEN bytes — not how long the whole
#: response takes. A provider trickling one token a minute never trips it, so a
#: 900s read timeout allowed a single compose to run 983s and would have
#: allowed it to run forever. Generous, because a large local model on CPU is
#: legitimately slow; finite, because "no cap at all" is how an item hangs.
CALL_DEADLINE_S = 900

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

#: OpenRouter rejects ``response_format: json_schema`` for models — or for the
#: upstream provider it happened to route to — that do not implement it, and
#: says so by name. Matching the message is what keeps the json_object fallback
#: narrow: a blind retry would also paper over genuinely malformed requests.
log = logging.getLogger(__name__)

_SCHEMA_UNSUPPORTED = re.compile(
    r"json[_ ]?schema|response_format|structured[_ ]?output", re.IGNORECASE
)

#: 4xx codes that no retry can fix: a wrong or revoked key, an exhausted
#: balance, a key without access to the model. These need a human, so they must
#: not sit in the retry queue pretending to be transient.
_TERMINAL_STATUSES = frozenset({401, 402, 403})

_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def parse_json(raw: str) -> Any:
    """Parse a model's JSON reply, tolerating a stray markdown fence."""
    cleaned = _FENCE.sub("", (raw or "").strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise BadCompletion(
            f"model returned unparseable JSON: {cleaned[:200]!r}"
        ) from exc


def _mime_type(path: Path) -> str:
    """Data URLs must declare the image type; Ollama's ``images`` array does not.

    Defaulting to PNG keeps a screenshot with no extension from being dropped
    outright — vision models tolerate a wrong label far better than a missing
    attachment.
    """
    return _MIME_TYPES.get(path.suffix.lower(), "image/png")


def _schema_prompt(system: str, schema: dict) -> str:
    """Restate a JSON Schema in the system prompt for json_object mode.

    ``json_object`` only guarantees that *something* parses as JSON; the shape
    has to come from the prompt, or the fallback returns valid JSON of the
    wrong form and the stage fails on a missing key instead.
    """
    return (
        f"{system}\n\n"
        "Reply with a single JSON object and nothing else — no prose, no "
        "markdown fence. It must conform to this JSON Schema:\n"
        f"{json.dumps(schema)}"
    )


class LLMClient:
    """Async chat over Ollama or OpenRouter, chosen by ``LLM_PROVIDER``."""

    def __init__(self, settings: Settings, http: Any) -> None:
        self.settings = settings
        self.http = http
        self.provider = settings.llm_provider
        # Each transport owns its endpoint. A single shared self.url meant the
        # local fallback posted an Ollama-shaped body to OpenRouter, so the
        # fallback could never have worked.
        self.ollama_url = f"{settings.ollama_host.rstrip('/')}/api/chat"
        self.url = OPENROUTER_URL if self.provider == "openrouter" else self.ollama_url

    async def cheap(
        self, system: str, user: str, schema: dict | None = None,
        temperature: float = 0.3,
    ) -> Any:
        return await self._chat("cheap", system, user, schema, temperature)

    async def good(
        self, system: str, user: str, schema: dict | None = None,
        temperature: float = 0.5,
    ) -> Any:
        return await self._chat("good", system, user, schema, temperature)

    async def vision(
        self, system: str, user: str, image_paths: list[str | Path],
        schema: dict | None = None, temperature: float = 0.2,
    ) -> Any:
        images: list[tuple[str, str]] = []
        for path in image_paths:
            file = Path(path)
            try:
                encoded = base64.b64encode(file.read_bytes()).decode()
            except OSError:
                continue  # a missing attachment must not kill extraction
            images.append((_mime_type(file), encoded))
        return await self._chat(
            "vision", system, user, schema, temperature, images=images,
        )

    # ------------------------------------------------------------- internals

    def _model(self, role: str) -> str:
        """Per-provider model for a role.

        Each provider keeps its own three names so switching back and forth
        never means re-entering models the owner already configured.
        """
        s = self.settings
        if self.provider == "openrouter":
            return {
                "cheap": s.openrouter_model_cheap,
                "good": s.openrouter_model_good,
                "vision": s.openrouter_model_vision,
            }[role]
        return {
            "cheap": s.model_cheap,
            "good": s.model_good,
            "vision": s.model_vision,
        }[role]

    def _num_ctx(self, role: str) -> int:
        s = self.settings
        return {
            "cheap": s.num_ctx_cheap,
            "good": s.num_ctx_good,
            "vision": s.num_ctx_vision,
        }[role]

    async def _chat(
        self, role: str, system: str, user: str,
        schema: dict | None, temperature: float,
        images: list[tuple[str, str]] | None = None,
    ) -> Any:
        if self.provider != "openrouter":
            return await self._ollama(
                self._model(role), self._num_ctx(role),
                system, user, schema, temperature, images,
            )

        # Everything transient gets the same treatment: three attempts at
        # OpenRouter, then this one call runs on the local model. Free-tier
        # backends fail in several ways and every one of them has now cost an
        # item — a rate limit, a completion that is empty or prose where a
        # schema was required, and the service being unreachable. Deferring
        # instead would stall the queue behind someone else's load while a
        # working local model sits idle.
        #
        # A 4xx is deliberately excluded. It fails identically every time, so
        # retrying only delays the real error reaching the operator.
        last: Exception | None = None
        reason = "unavailable"
        for attempt in range(1, OPENROUTER_ATTEMPTS + 1):
            try:
                return await self._openrouter(
                    self._model(role), system, user, schema, temperature, images,
                )
            except RateLimited as exc:
                last, reason = exc, "rate-limited"
            except BadCompletion as exc:
                last, reason = exc, "returning an unusable completion"
            except Retryforever as exc:
                # Unreachable or 5xx. RateLimited subclasses this, so it must
                # be caught after it.
                last, reason = exc, "unreachable"

            if attempt < OPENROUTER_ATTEMPTS:
                delay = OPENROUTER_BACKOFF_S * attempt
                log.warning(
                    "openrouter %s on %s (attempt %d/%d), retrying in %ss",
                    reason, role, attempt, OPENROUTER_ATTEMPTS, delay,
                )
                await asyncio.sleep(delay)

        if not self.settings.fallback_to_local:
            raise last

        chain = self._local_chain(role)
        log.warning(
            "openrouter still %s on %s after %d attempts; falling back to local %s",
            reason, role, OPENROUTER_ATTEMPTS, " then ".join(chain),
        )
        # Walk the local chain. A second local model earns its place only by
        # answering when the first cannot — a model that is absent from Ollama
        # or too degraded to return usable output should not end the item while
        # a working one sits behind it.
        local_error: Exception | None = None
        for position, model in enumerate(chain, start=1):
            try:
                return await self._ollama(
                    model, self._num_ctx(role),
                    system, user, schema, temperature, images,
                )
            except (Retryforever, BadCompletion) as exc:
                local_error = exc
                if position < len(chain):
                    log.warning(
                        "local %s failed on %s (%s); trying %s",
                        model, role, exc, chain[position],
                    )

        # Every provider is exhausted. Report the openrouter reason alongside
        # the local one so the log names the real situation rather than only
        # the last thing tried.
        raise Retryforever(
            f"openrouter {reason} and local chain "
            f"({', '.join(chain)}) unavailable: {local_error}"
        ) from local_error

    def _model_local(self, role: str) -> str:
        """The Ollama model for a role, whatever provider is selected."""
        s = self.settings
        return {"cheap": s.model_cheap, "good": s.model_good,
                "vision": s.model_vision}[role]

    def _local_chain(self, role: str) -> list[str]:
        """Local models to try in order: the primary, then any spare."""
        s = self.settings
        spare = {"cheap": s.model_cheap_fallback,
                 "good": s.model_good_fallback,
                 "vision": s.model_vision_fallback}[role]
        chain = [self._model_local(role)]
        if spare and spare != chain[0]:
            chain.append(spare)
        return chain

    async def _ollama(
        self, model: str, num_ctx: int, system: str, user: str,
        schema: dict | None, temperature: float,
        images: list[tuple[str, str]] | None = None,
    ) -> Any:
        message: dict[str, Any] = {"role": "user", "content": user}
        if images:
            message["images"] = [encoded for _, encoded in images]

        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, message],
            "stream": False,
            "think": False,
            "options": {"temperature": temperature, "num_ctx": num_ctx},
        }
        if schema is not None:
            payload["format"] = schema

        for attempt in (1, 2):
            try:
                response = await asyncio.wait_for(
                    self.http.post(
                        self.ollama_url, json=payload, timeout=CALL_DEADLINE_S
                    ),
                    timeout=CALL_DEADLINE_S,
                )
            except asyncio.TimeoutError as exc:
                raise Retryforever(
                    f"ollama exceeded {CALL_DEADLINE_S}s"
                ) from exc
            except Exception as exc:  # connection refused, DNS, timeout
                raise Retryforever(f"ollama unreachable: {exc}") from exc

            status = response.status_code
            if status == 400 and attempt == 1 and "think" in _body_text(response).lower():
                # Non-thinking models reject the `think` parameter outright.
                payload.pop("think", None)
                continue
            if status >= 500:
                raise Retryforever(f"ollama {status}: {_body_text(response)[:200]}")
            if status >= 400:
                raise Retryable(f"ollama {status}: {_body_text(response)[:200]}")

            content = response.json()["message"]["content"]
            return parse_json(content) if schema is not None else content

        raise Retryable("ollama call did not resolve")  # pragma: no cover

    async def _openrouter(
        self, model: str, system: str, user: str,
        schema: dict | None, temperature: float,
        images: list[tuple[str, str]] | None = None,
    ) -> Any:
        """POST the OpenAI-compatible chat endpoint.

        Same three differences from Ollama every time: structured output rides
        on ``response_format`` instead of ``format``, images ride in a content
        array as data URLs instead of a base64 ``images`` list, and ``num_ctx``
        does not exist here.
        """
        content: Any = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                }
                for mime, encoded in images
            ]

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "stream": False,
            "temperature": temperature,
        }
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": schema},
            }

        headers = {"Authorization": f"Bearer {self.settings.openrouter_api_key}"}

        for attempt in (1, 2):
            try:
                response = await asyncio.wait_for(
                    self.http.post(
                        OPENROUTER_URL, json=payload, headers=headers,
                        timeout=CALL_DEADLINE_S,
                    ),
                    timeout=CALL_DEADLINE_S,
                )
            except asyncio.TimeoutError as exc:
                # Wall-clock, unlike httpx's between-bytes read timeout: a
                # response that trickles forever is stopped here.
                raise Retryforever(
                    f"openrouter exceeded {CALL_DEADLINE_S}s"
                ) from exc
            except Exception as exc:  # connection refused, DNS, timeout
                raise Retryforever(f"openrouter unreachable: {exc}") from exc

            status = response.status_code
            body = _body_text(response)

            if (
                schema is not None
                and attempt == 1
                and 400 <= status < 500
                and _SCHEMA_UNSUPPORTED.search(body)
            ):
                # This model has no json_schema support. Degrade to json_object
                # with the schema in the prompt rather than dropping structure:
                # every caller that passes a schema needs a dict back.
                payload["response_format"] = {"type": "json_object"}
                payload["messages"][0]["content"] = _schema_prompt(system, schema)
                continue
            if status == 429:
                # A rate limit is the service saying "not now". The item is
                # fine, so it must not spend an attempt on someone else's load.
                raise RateLimited(f"openrouter 429: {body[:200]}")
            if status >= 500:
                raise Retryforever(f"openrouter {status}: {body[:200]}")
            if status in _TERMINAL_STATUSES:
                raise Terminal(f"openrouter {status}: {body[:200]}")
            if status >= 400:
                raise Retryable(f"openrouter {status}: {body[:200]}")

            try:
                content = response.json()["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                # A 200 carrying an error envelope instead of a completion is
                # normal here: OpenRouter answers that way when the upstream
                # provider dies after the response has started.
                raise BadCompletion(
                    f"openrouter returned no completion: {body[:200]}"
                ) from exc
            if content is None or not str(content).strip():
                # An empty string is as unusable as a missing key, and would
                # otherwise reach parse_json or be returned as a valid answer.
                raise BadCompletion(
                    f"openrouter returned an empty completion: {body[:200]}"
                )
            return parse_json(content) if schema is not None else content

        raise Retryable("openrouter call did not resolve")  # pragma: no cover


def _body_text(response: Any) -> str:
    try:
        return response.text or ""
    except Exception:  # pragma: no cover
        return ""
