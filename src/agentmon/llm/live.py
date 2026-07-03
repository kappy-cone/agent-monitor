"""Live-substrate client construction and pins — behind the mock-first seam.

Importable with no key set: only *calling* :func:`build_live_client`
constructs a real client, and tests never call it (they use
``agentmon.llm.mock.MockLLMClient``, or monkeypatch this function on the
adapters). The pins moved here verbatim from ``scripts/dev_eval.py``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentmon.llm.client import LLMClient

# Pinned primary (DECISIONS 34): gemini-3.1-flash-lite, free tier, via the
# OpenAI-compatible endpoint. Dashboard-confirmed project limits 2026-06-12:
# 10 RPM / 500 RPD — the dashboard plus observed 429 behavior are the
# authority; all pins are conditional on observed quota. Actual spend is
# $0.00 — the cost column reports the list-price equivalent from PRICING.
PRIMARY_MODEL = "gemini-3.1-flash-lite"
PRIMARY_RPM = 10
PRIMARY_RPD = 500
MIN_INTERVAL_SECONDS = 60.0 / PRIMARY_RPM + 0.2
#: Probe-verified 2026-06-12 (out/phase3/probe.json): reasoning_effort "none"
#: is accepted — thinking fully disabled, matching the no-extended-thinking
#: monitor design. See DECISIONS 28.
PRIMARY_EXTRA_BODY: dict | None = {"reasoning_effort": "none"}

# Local target (DECISIONS 36): Qwen 3.6 35B A3B (NVFP4) served by vLLM on the
# omen, reached over the SSH tunnel. agentmon is a pure HTTP client, so it runs
# on the Mac; only inference runs on the GPU box. No rate limit -> no pacing.
# Thinking is suppressed via Qwen's chat-template kwarg (parity with the
# primary's reasoning-off config); top_p/top_k are Qwen's non-thinking defaults.
LOCAL_MODEL = os.environ.get("LOCAL_LLM_MODEL", "agentnom-local")
LOCAL_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
LOCAL_EXTRA_BODY: dict = {
    "chat_template_kwargs": {"enable_thinking": False},
    "top_p": 0.8,
    "top_k": 20,
}


def build_live_client(local: bool) -> tuple[LLMClient, str | None]:
    """Return (client, default_model_override) for a live run.

    Local uses the omen tunnel with no pacing and the served model name as the
    override (the frozen frontmatter still names gemini, so the override is how
    the local model gets addressed). The cloud path returns None so the frozen
    frontmatter model is used unchanged.
    """
    from agentmon.llm.openai_compat import OpenAICompatClient

    if local:
        client = OpenAICompatClient(
            base_url=LOCAL_BASE_URL,
            api_key="local",
            extra_body=LOCAL_EXTRA_BODY,
            min_interval_seconds=0.0,
            max_attempts=5,
            backoff_seconds=5.0,
        )
        return client, LOCAL_MODEL
    client = OpenAICompatClient(
        extra_body=PRIMARY_EXTRA_BODY,
        min_interval_seconds=MIN_INTERVAL_SECONDS,
        max_attempts=5,
        backoff_seconds=5.0,
    )
    return client, None
