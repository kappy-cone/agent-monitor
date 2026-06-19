"""OpenAI-compatible chat-completions client (Google AI Studio, Groq, and friends).

The $0 experiment's primary path: a free-tier Gemini Flash-class monitor via
Google's OpenAI-compatible endpoint, plus the post-freeze transfer row on a
second free-tier provider. The client implements the same
:class:`~agentmon.llm.client.LLMClient` protocol the rest of the harness
codes against; the model id flows from the runner (which puts it in every
cache key), never from edited prompt frontmatter.

Mock-first discipline: unit tests exercise :meth:`parse_payload` and the
transport behavior against canned fixtures via ``httpx.MockTransport``; the
only live-network path is a skip-marked integration test. API keys are read
from the environment, held privately, and never written to logs, cache
entries, or error messages.
"""

from __future__ import annotations

import os
import random
import time
from typing import Any

import httpx

from agentmon.llm.client import LLMResponse

GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GOOGLE_API_KEY_ENV = "GEMINI_API_KEY"
DEFAULT_BASE_URL = GOOGLE_BASE_URL
API_KEY_ENV = GOOGLE_API_KEY_ENV
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _strip_think_blocks(text: str) -> str:
    """Drop ``<think>...</think>`` reasoning prefixes some open models emit.

    Reasoning mode is disabled in the request, but providers vary in honoring
    it; leftover think blocks would put stray prose (and occasionally
    JSON-shaped fragments) ahead of the verdict object.
    """
    while True:
        start = text.find(_THINK_OPEN)
        if start == -1:
            return text
        end = text.find(_THINK_CLOSE, start)
        if end == -1:
            # Unterminated block: keep the text as-is rather than guess.
            return text
        text = text[:start] + text[end + len(_THINK_CLOSE) :]


class OpenAICompatClient:
    """LLMClient over an OpenAI-compatible ``/chat/completions`` endpoint.

    Provider-agnostic: ``base_url``/``api_key_env`` select the provider
    (Google AI Studio, OpenRouter, Groq, ...), and ``extra_body`` carries any
    provider-specific request fields (reasoning/thinking knobs, routing pins)
    verbatim. ``min_interval_seconds`` is RPM-aware pacing for free-tier
    quotas: successive requests are spaced at least that far apart. 429s and
    5xx retry with exponential backoff plus jitter, honoring ``Retry-After``
    when the provider sends one. ``transport`` exists for tests only.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        api_key_env: str = API_KEY_ENV,
        extra_body: dict[str, Any] | None = None,
        min_interval_seconds: float = 0.0,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 90.0,
        timeout_seconds: float = 180.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get(api_key_env)
        if not key:
            raise ValueError(f"no API key: pass api_key or set {api_key_env} (key is never logged)")
        self._extra_body = extra_body or {}
        self._min_interval_seconds = min_interval_seconds
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._last_request_at: float | None = None
        self.request_count = 0
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
            **self._extra_body,
        }
        start = time.monotonic()
        payload = self._post_with_retries(body)
        latency_ms = (time.monotonic() - start) * 1000
        return self.parse_payload(payload, fallback_model=model, latency_ms=latency_ms)

    def _pace(self) -> None:
        """Space successive requests at least min_interval_seconds apart."""
        if self._min_interval_seconds <= 0:
            return
        now = time.monotonic()
        if self._last_request_at is not None:
            wait = self._min_interval_seconds - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _backoff_delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after is not None:
            try:
                return min(float(retry_after), self._max_backoff_seconds)
            except ValueError:
                pass  # HTTP-date form or junk: fall through to exponential
        base = min(self._backoff_seconds * 2 ** (attempt - 1), self._max_backoff_seconds)
        return base * random.uniform(0.5, 1.5)

    def _post_with_retries(self, body: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        retry_after: str | None = None
        for attempt in range(self._max_attempts):
            if attempt:
                time.sleep(self._backoff_delay(attempt, retry_after))
            self._pace()
            self.request_count += 1
            try:
                response = self._client.post("/chat/completions", json=body)
            except httpx.TransportError as exc:
                last_error = exc
                retry_after = None
                continue
            if response.status_code in _RETRYABLE_STATUS:
                # Keep the body: provider 429s name the violated quota, which
                # drives the run plan (and is scanned for billing markers).
                last_error = RuntimeError(
                    f"HTTP {response.status_code} from completions endpoint: {response.text[:500]}"
                )
                retry_after = response.headers.get("retry-after")
                continue
            if response.status_code != 200:
                # Error bodies are provider prose, safe to surface; the key
                # lives only in request headers and is never echoed here.
                raise RuntimeError(
                    f"HTTP {response.status_code} from completions endpoint: {response.text[:500]}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError(f"completions endpoint returned non-JSON body: {exc}") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("completions endpoint returned a non-object JSON body")
            return payload
        raise RuntimeError(f"completions request failed after {self._max_attempts} attempts") from (
            last_error
        )

    @staticmethod
    def parse_payload(
        payload: dict[str, Any], *, fallback_model: str, latency_ms: float = 0.0
    ) -> LLMResponse:
        """Normalize an OpenAI-format completion payload into an LLMResponse.

        Raises ValueError on a malformed envelope (no choices / no message
        content). Truncated responses (``finish_reason: length``) are returned
        as-is — the calibration pipeline's parse hardening owns recovery.
        """
        if "error" in payload:
            error = payload["error"]
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise ValueError(f"completions payload carries an error: {message}")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("completions payload has no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise ValueError("completions payload choice has no message object")
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("completions payload message has no string content")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        model = payload.get("model")
        return LLMResponse(
            text=_strip_think_blocks(content).strip(),
            model=model if isinstance(model, str) and model else fallback_model,
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            latency_ms=latency_ms,
        )
