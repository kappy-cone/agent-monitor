"""LLM client protocol and the real Anthropic implementation.

``AnthropicClient`` is the only place in the codebase that talks to a live
API. Tests never construct it; they use ``agentmon.llm.mock.MockLLMClient``.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from pydantic import BaseModel


class LLMResponse(BaseModel):
    """A completed (non-streaming) model response."""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


class LLMClient(Protocol):
    """Minimal completion interface monitors run against."""

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse: ...


class AnthropicClient:
    """Real client backed by the anthropic SDK. Never used in tests."""

    def __init__(self, api_key: str | None = None) -> None:
        # Imported lazily so the package works without the SDK configured.
        import anthropic

        self._client: Any = anthropic.Anthropic(api_key=api_key)

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        start = time.monotonic()
        message = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = (time.monotonic() - start) * 1000
        text = "".join(block.text for block in message.content if block.type == "text")
        return LLMResponse(
            text=text,
            model=message.model,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            latency_ms=latency_ms,
        )
