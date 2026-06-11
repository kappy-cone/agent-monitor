"""Deterministic mock LLM client used by all tests and ``--mock`` runs."""

from __future__ import annotations

import json

from agentmon.llm.client import LLMResponse

DEFAULT_VERDICT_JSON = json.dumps(
    {
        "suspicion_score": 10,
        "categories": [],
        "evidence": [{"event_index": 0, "quote": "mock evidence"}],
        "reasoning": "Mock verdict: nothing suspicious found.",
    }
)


class MockLLMClient:
    """Returns canned responses and records every call.

    ``script`` maps a substring to a response: the first entry whose key
    appears in the prompt wins. Otherwise ``default_response`` is returned.
    Fully deterministic — same prompts in, same responses out.
    """

    def __init__(
        self,
        script: dict[str, str] | None = None,
        default_response: str = DEFAULT_VERDICT_JSON,
    ) -> None:
        self.script = script or {}
        self.default_response = default_response
        self.calls: list[dict[str, object]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        text = self.default_response
        for needle, response in self.script.items():
            if needle in prompt:
                text = response
                break
        return LLMResponse(
            text=text,
            model=model,
            input_tokens=len(prompt) // 4,
            output_tokens=len(text) // 4,
            latency_ms=0.0,
        )
