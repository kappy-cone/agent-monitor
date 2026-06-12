"""OpenAI-compatible client tests: canned fixtures only, zero network, zero spend.

The only live-network path is the skip-marked integration test at the bottom,
which requires OPENROUTER_API_KEY and is excluded from normal runs.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest

from agentmon.llm.openai_compat import OpenAICompatClient, _strip_think_blocks

VERDICT = json.dumps(
    {
        "suspicion_score": 40,
        "categories": [],
        "evidence": [],
        "reasoning": "nothing conclusive",
    }
)


def payload(
    content: str,
    *,
    model: str = "qwen/qwen3.6-35b-a3b",
    finish_reason: str = "stop",
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "id": "gen-123",
        "model": model,
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}
        ],
        "usage": usage if usage is not None else {"prompt_tokens": 100, "completion_tokens": 20},
    }


def make_client(handler, **kwargs) -> OpenAICompatClient:
    return OpenAICompatClient(
        api_key="test-key-not-real",
        transport=httpx.MockTransport(handler),
        backoff_seconds=0.0,
        **kwargs,
    )


class TestParsePayload:
    def test_plain_verdict(self) -> None:
        response = OpenAICompatClient.parse_payload(payload(VERDICT), fallback_model="fb")
        assert response.text == VERDICT
        assert response.model == "qwen/qwen3.6-35b-a3b"
        assert response.input_tokens == 100
        assert response.output_tokens == 20

    def test_fenced_output_passes_through(self) -> None:
        # Fences are the verdict parser's job (it already tolerates them);
        # the client must not mangle the content.
        fenced = f"```json\n{VERDICT}\n```"
        response = OpenAICompatClient.parse_payload(payload(fenced), fallback_model="fb")
        assert response.text == fenced

    def test_reasoning_prefixed_output_is_stripped(self) -> None:
        content = f"<think>\nlet me look at event 3...\n</think>\n{VERDICT}"
        response = OpenAICompatClient.parse_payload(payload(content), fallback_model="fb")
        assert response.text == VERDICT

    def test_unterminated_think_block_kept_verbatim(self) -> None:
        content = "<think>never closed " + VERDICT
        response = OpenAICompatClient.parse_payload(payload(content), fallback_model="fb")
        assert response.text == content

    def test_truncated_response_returned_as_is(self) -> None:
        # finish_reason "length": recovery belongs to the parse-hardening layer.
        truncated = VERDICT[:30]
        response = OpenAICompatClient.parse_payload(
            payload(truncated, finish_reason="length"), fallback_model="fb"
        )
        assert response.text == truncated

    def test_missing_model_falls_back(self) -> None:
        data = payload(VERDICT)
        del data["model"]
        response = OpenAICompatClient.parse_payload(data, fallback_model="fb")
        assert response.model == "fb"

    def test_missing_usage_yields_zero_tokens(self) -> None:
        data = payload(VERDICT)
        del data["usage"]
        response = OpenAICompatClient.parse_payload(data, fallback_model="fb")
        assert response.input_tokens == 0
        assert response.output_tokens == 0

    def test_no_choices_raises(self) -> None:
        with pytest.raises(ValueError, match="no choices"):
            OpenAICompatClient.parse_payload({"choices": []}, fallback_model="fb")

    def test_error_payload_raises(self) -> None:
        with pytest.raises(ValueError, match="rate limit"):
            OpenAICompatClient.parse_payload(
                {"error": {"message": "rate limit exceeded"}}, fallback_model="fb"
            )

    def test_non_string_content_raises(self) -> None:
        data = payload(VERDICT)
        data["choices"][0]["message"]["content"] = None
        with pytest.raises(ValueError, match="no string content"):
            OpenAICompatClient.parse_payload(data, fallback_model="fb")


class TestStripThink:
    def test_multiple_blocks(self) -> None:
        assert _strip_think_blocks("<think>a</think>x<think>b</think>y") == "xy"

    def test_no_blocks(self) -> None:
        assert _strip_think_blocks("plain") == "plain"


class TestTransport:
    def test_request_shape_and_extra_body_passthrough(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=payload(VERDICT))

        client = make_client(handler, extra_body={"reasoning_effort": "low"})
        response = client.complete("judge this", model="gemini-3.5-flash", temperature=0.7)

        assert response.text == VERDICT
        body = json.loads(seen[0].content)
        assert body["model"] == "gemini-3.5-flash"
        assert body["temperature"] == 0.7
        assert body["reasoning_effort"] == "low"
        assert seen[0].headers["authorization"] == "Bearer test-key-not-real"

    def test_retries_on_429_then_succeeds(self) -> None:
        calls = iter([429, 200])

        def handler(request: httpx.Request) -> httpx.Response:
            status = next(calls)
            if status == 200:
                return httpx.Response(200, json=payload(VERDICT))
            return httpx.Response(status, text="slow down")

        client = make_client(handler)
        assert client.complete("p", model="m").text == VERDICT
        assert client.request_count == 2

    def test_retry_after_header_is_honored(self) -> None:
        calls = iter([429, 200])

        def handler(request: httpx.Request) -> httpx.Response:
            status = next(calls)
            if status == 200:
                return httpx.Response(200, json=payload(VERDICT))
            return httpx.Response(status, text="slow down", headers={"Retry-After": "0"})

        client = make_client(handler)
        assert client.complete("p", model="m").text == VERDICT

    def test_exhausted_retries_raise(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="busy")

        client = make_client(handler)
        with pytest.raises(RuntimeError, match="failed after 3 attempts"):
            client.complete("p", model="m")

    def test_non_retryable_error_raises_without_key_leak(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="bad token")

        client = make_client(handler)
        with pytest.raises(RuntimeError) as excinfo:
            client.complete("p", model="m")
        assert "test-key-not-real" not in str(excinfo.value)

    def test_missing_key_raises_before_any_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(ValueError, match="no API key"):
            OpenAICompatClient()

    def test_min_interval_paces_successive_requests(self) -> None:
        import time

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload(VERDICT))

        client = make_client(handler, min_interval_seconds=0.05)
        start = time.monotonic()
        client.complete("a", model="m")
        client.complete("b", model="m")
        assert time.monotonic() - start >= 0.05


@pytest.mark.skipif(
    os.environ.get("AGENTMON_LIVE_TESTS") != "1" or not os.environ.get("GEMINI_API_KEY"),
    reason="live integration test: opt in with AGENTMON_LIVE_TESTS=1 plus GEMINI_API_KEY — "
    "a resolvable key alone must never make `uv run pytest` go live (mock-first rule)",
)
def test_live_endpoint_smoke() -> None:  # pragma: no cover - quota, not money
    client = OpenAICompatClient()
    response = client.complete(
        "Reply with the single word: ok", model="gemini-3.1-flash-lite", max_tokens=64
    )
    assert response.text
