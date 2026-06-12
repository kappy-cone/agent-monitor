"""The single authorized pre-Round-0 probe: auth + thinking-knob + quota behavior.

Negotiates the thinking knob with at most three request attempts (a rejected
parameter returns 400 without serving a completion): ``reasoning_effort:
"none"`` -> ``"low"`` -> no knob. Records the accepted configuration, model
echo, token usage, and latency to ``out/phase3/probe.json``. Never prints or
persists the API key.

HARD RULE (billing safety): if any response mentions billing or payment setup,
this script prints it and exits nonzero — never proceed past it.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentmon.llm.openai_compat import OpenAICompatClient  # noqa: E402

MODEL = "gemini-3.5-flash"
KNOB_CANDIDATES: list[dict | None] = [
    {"reasoning_effort": "none"},
    {"reasoning_effort": "low"},
    None,
]
_BILLING_MARKERS = ("billing", "payment", "upgrade your plan", "enable billing")


def main() -> None:
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY is not set; cannot probe.")
    attempts: list[dict] = []
    for extra_body in KNOB_CANDIDATES:
        client = OpenAICompatClient(extra_body=extra_body, max_attempts=2, backoff_seconds=2.0)
        try:
            response = client.complete(
                "Reply with exactly this JSON and nothing else: "
                '{"suspicion_score": 0, "categories": [], "evidence": [], "reasoning": "probe"}',
                model=MODEL,
                max_tokens=256,
                temperature=0.0,
            )
        except (RuntimeError, ValueError) as exc:
            message = str(exc)
            attempts.append({"extra_body": extra_body, "error": message[:300]})
            if any(marker in message.lower() for marker in _BILLING_MARKERS):
                print(json.dumps(attempts, indent=2))
                sys.exit("BILLING MARKER IN RESPONSE — stopping per the hard rule. Do not proceed.")
            continue
        result = {
            "accepted_extra_body": extra_body,
            "model_echo": response.model,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "latency_ms": round(response.latency_ms, 1),
            "text": response.text[:300],
            "requests_used": client.request_count,
            "attempts": attempts,
            "date": datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds"),
        }
        out = REPO / "out" / "phase3"
        out.mkdir(parents=True, exist_ok=True)
        (out / "probe.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))
        return
    print(json.dumps(attempts, indent=2))
    sys.exit("probe failed under every knob candidate — see attempts above")


if __name__ == "__main__":
    main()
