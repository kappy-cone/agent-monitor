"""The calibrated render budget against the committed test-matrix artifacts.

Two kinds of pin, both over tracked files (``results/`` is committed, so these
are normal tests — no cache, no client, no model calls):

1. The design-01 Instrument-4 HARD assertions: the worst committed Gemma
   sample record (~19.8k server tokens on a 64k-char render, DECISIONS 41)
   rebuilt through the frozen monitor must FAIL ``preflight_fits`` at the
   16384 context that overflowed operationally and PASS at the 32768 re-serve
   — overflow becomes a detectable pre-spend failure.
2. ``_CALIBRATED_CPT`` must equal the values the calibration script derived
   and committed to ``results/dev-render-calibration/summary.json`` — the pin
   and its published derivation cannot drift apart silently.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentmon.monitors.registry import load_monitors
from agentmon.rendering import _CALIBRATED_CPT, preflight_fits
from agentmon.schemas import CalibratedVerdict, Transcript

REPO = Path(__file__).resolve().parents[1]
MATRIX = REPO / "results" / "gemma-qwen-test-matrix"
CALIBRATION_SUMMARY = REPO / "results" / "dev-render-calibration" / "summary.json"
TRANSCRIPTS = REPO / "datasets" / "synthetic" / "transcripts"
GEMMA_SERVED = "agentmon-local-gemma"


def _worst_gemma_record() -> tuple[str, str, int]:
    """(monitor_id, transcript_id, input_tokens) of the largest committed Gemma sample."""
    worst: tuple[str, str, int] | None = None
    for path in sorted((MATRIX / "gemma").glob("test-*/verdicts.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            verdict = CalibratedVerdict.model_validate_json(line)
            for sample in verdict.samples:
                if worst is None or sample.input_tokens > worst[2]:
                    worst = (verdict.monitor_id, verdict.transcript_id, sample.input_tokens)
    assert worst is not None, "no committed Gemma sample records found"
    return worst


def test_gemma_overflow_record_fails_16k_and_fits_32k() -> None:
    monitor_id, transcript_id, input_tokens = _worst_gemma_record()
    # The DECISIONS-41 overflow class: ~19.8k server tokens on a 16384 context.
    assert input_tokens > 16384, "the committed overflow record should exceed the 16k context"
    transcript = Transcript.model_validate_json(
        (TRANSCRIPTS / f"{transcript_id}.json").read_text(encoding="utf-8")
    )
    prompt = load_monitors()[monitor_id].build_prompt(transcript)
    cpt = _CALIBRATED_CPT[GEMMA_SERVED]
    assert not preflight_fits(prompt, 16384, chars_per_token=cpt)
    assert preflight_fits(prompt, 32768, chars_per_token=cpt)


def test_pinned_cpt_matches_the_committed_derivation() -> None:
    summary = json.loads(CALIBRATION_SUMMARY.read_text(encoding="utf-8"))
    assert summary["pinned"] == _CALIBRATED_CPT
