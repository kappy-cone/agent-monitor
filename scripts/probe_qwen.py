"""Probe the local Qwen 3.6 35B A3B (NVFP4) on the omen, via the SSH tunnel.

One call to localhost:8000 (forwarded to vLLM on the omen). Confirms OpenAI
format, model echo, that a verdict-shaped JSON parses, that thinking is
suppressed (no <think> leak), and reports latency + tokens. Local and free, so
there is no billing rule — but the run is otherwise the analog of probe_gemini.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentmon.llm.openai_compat import OpenAICompatClient  # noqa: E402

BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
MODEL = os.environ.get("LOCAL_LLM_MODEL", "agentnom-local")
# Qwen non-thinking parity: enable_thinking goes through the chat template; the
# sampling defaults are Qwen's recommended non-thinking values.
EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}, "top_p": 0.8, "top_k": 20}


def main() -> None:
    client = OpenAICompatClient(
        base_url=BASE_URL,
        api_key="local",  # vLLM ignores the bearer unless started with --api-key
        extra_body=EXTRA_BODY,
        min_interval_seconds=0.0,
        max_attempts=2,
        backoff_seconds=2.0,
    )
    prompt = (
        "Reply with exactly this JSON object and nothing else: "
        '{"suspicion_score": 0, "categories": [], "evidence": [], "reasoning": "probe"}'
    )
    response = client.complete(prompt, model=MODEL, max_tokens=256, temperature=0.0)
    try:
        parsed = json.loads(response.text)
        json_ok = isinstance(parsed, dict) and "suspicion_score" in parsed
    except (ValueError, TypeError):
        json_ok = False
    result = {
        "base_url": BASE_URL,
        "model_arg": MODEL,
        "model_echo": response.model,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "latency_ms": round(response.latency_ms, 1),
        "think_leak": "<think>" in response.text.lower(),
        "json_parses": json_ok,
        "text": response.text[:300],
    }
    out = REPO / "out" / "phase4"
    out.mkdir(parents=True, exist_ok=True)
    (out / "probe_qwen.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
