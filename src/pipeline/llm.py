"""Ollama chat client.

Every call pins ``num_ctx``. This is not tuning: unpinned, Ollama 0.33 loads a
model at its full declared context (262144 for qwen3:4b-instruct), reports ~43GB
of allocation and spills to CPU, making the machine unusable. Pinned to 8192 the
same model is 3.9GB and fully GPU-resident.

The class is also the seam that keeps the test suite off the GPU — tests inject
``FakeLLM`` with the same three methods.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import Retryable, Retryforever

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json(raw: str) -> Any:
    """Parse a model's JSON reply, tolerating a stray markdown fence."""
    cleaned = _FENCE.sub("", (raw or "").strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise Retryable(f"model returned unparseable JSON: {cleaned[:200]!r}") from exc


class LLMClient:
    """Thin async wrapper over ``POST {ollama_host}/api/chat``."""

    def __init__(self, settings: Settings, http: Any) -> None:
        self.settings = settings
        self.http = http
        self.url = f"{settings.ollama_host.rstrip('/')}/api/chat"

    async def cheap(
        self, system: str, user: str, schema: dict | None = None,
        temperature: float = 0.3,
    ) -> Any:
        return await self._chat(
            self.settings.model_cheap, self.settings.num_ctx_cheap,
            system, user, schema, temperature,
        )

    async def good(
        self, system: str, user: str, schema: dict | None = None,
        temperature: float = 0.5,
    ) -> Any:
        return await self._chat(
            self.settings.model_good, self.settings.num_ctx_good,
            system, user, schema, temperature,
        )

    async def vision(
        self, system: str, user: str, image_paths: list[str | Path],
        schema: dict | None = None, temperature: float = 0.2,
    ) -> Any:
        images = []
        for path in image_paths:
            try:
                images.append(base64.b64encode(Path(path).read_bytes()).decode())
            except OSError:
                continue  # a missing attachment must not kill extraction
        return await self._chat(
            self.settings.model_vision, self.settings.num_ctx_vision,
            system, user, schema, temperature, images=images,
        )

    # ------------------------------------------------------------- internals

    async def _chat(
        self, model: str, num_ctx: int, system: str, user: str,
        schema: dict | None, temperature: float,
        images: list[str] | None = None,
    ) -> Any:
        message: dict[str, Any] = {"role": "user", "content": user}
        if images:
            message["images"] = images

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
                response = await self.http.post(self.url, json=payload, timeout=900)
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


def _body_text(response: Any) -> str:
    try:
        return response.text or ""
    except Exception:  # pragma: no cover
        return ""
