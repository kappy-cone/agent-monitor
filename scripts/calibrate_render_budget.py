"""Calibrate the chars-per-token ratio from the committed test-matrix sample records.

Offline, deterministic, zero model calls: for every committed SAMPLE record in
``results/gemma-qwen-test-matrix/{qwen,gemma}/test-*/verdicts.jsonl`` (990
usage-bearing records per substrate — 66 transcripts x 5 monitors x k=3;
verification records are EXCLUDED because their prompts are
``build_verification_prompt`` output, not ``build_prompt`` output), rebuild the
prompt via the frozen monitor + transcript (rendering is pure, so this
reproduces the historical prompt byte-exactly) and observe
``ratio = len(prompt) / input_tokens`` against the server-reported usage.

DERIVATION RULE — pinned here BEFORE any number is read (the DECISIONS-42
blind-derivation discipline):

- p05 = the empirical 5th percentile of the per-substrate ratios: sorted
  ascending, the value at index ``floor(0.05 * n)``.
- pinned chars-per-token = ``max(3.0, floor(p05 * 10) / 10)`` — p05 rounded
  DOWN to 0.1, floored at 3.0. This is what ``_CALIBRATED_CPT`` in
  ``src/agentmon/rendering.py`` records per served-model name; the ``""``
  (unknown-model) fallback pins to the MIN of the per-substrate values (the
  conservative side: a smaller ratio overestimates tokens).
- Report-only retrodiction coverage (the DECISIONS 35b/c precedent — recorded,
  never gated): the fraction of records whose ``estimate_tokens`` under the
  pinned ratio is >= the server-reported ``input_tokens`` (>= 0.95 expected by
  construction of p05; a p05-derived ratio cannot guarantee an all-quantifier
  over a tail-heavy distribution). The only HARD assertions live in
  ``tests/test_render_calibration.py`` (the two Gemma-overflow
  ``preflight_fits`` checks), which p05 does guarantee.

Writes the measured distribution and pinned values to
``results/dev-render-calibration/`` (a NEW directory; existing ``results/``
artifacts are never modified).

Run: env -u UV_PYTHON uv run python scripts/calibrate_render_budget.py
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentmon.eval.workspace import RESULTS_DIR, TRANSCRIPTS_DIR  # noqa: E402
from agentmon.monitors.registry import load_monitors  # noqa: E402
from agentmon.rendering import estimate_tokens  # noqa: E402
from agentmon.schemas import CalibratedVerdict, Transcript  # noqa: E402

MATRIX = RESULTS_DIR / "gemma-qwen-test-matrix"
OUT = RESULTS_DIR / "dev-render-calibration"
#: served name as recorded in the committed rows (DECISIONS 37, 41) — the
#: ``agentnom-local`` typo is canonical, never "fixed".
SUBSTRATES = {"qwen": "agentnom-local", "gemma": "agentmon-local-gemma"}
EXPECTED_RECORDS = 990  # 66 transcripts x 5 monitors x k=3, per substrate
CPT_FLOOR = 3.0


def pin_from_p05(p05: float) -> float:
    """The pinned ratio: p05 rounded DOWN to 0.1, floored at 3.0 (rule above)."""
    return max(CPT_FLOOR, math.floor(p05 * 10) / 10)


def empirical_p05(sorted_ratios: list[float]) -> float:
    """The value at index floor(0.05 * n) of the ascending-sorted ratios."""
    return sorted_ratios[math.floor(0.05 * len(sorted_ratios))]


def substrate_records(subdir: Path) -> list[tuple[str, str, int]]:
    """(monitor_id, transcript_id, input_tokens) per committed sample record."""
    records: list[tuple[str, str, int]] = []
    for path in sorted(subdir.glob("test-*/verdicts.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            verdict = CalibratedVerdict.model_validate_json(line)
            for sample in verdict.samples:
                records.append((verdict.monitor_id, verdict.transcript_id, sample.input_tokens))
    return records


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.parse_args()

    monitors = load_monitors()
    prompt_chars: dict[tuple[str, str], int] = {}

    def chars_for(monitor_id: str, transcript_id: str) -> int:
        key = (monitor_id, transcript_id)
        if key not in prompt_chars:
            transcript = Transcript.model_validate_json(
                (TRANSCRIPTS_DIR / f"{transcript_id}.json").read_text(encoding="utf-8")
            )
            prompt_chars[key] = len(monitors[monitor_id].build_prompt(transcript))
        return prompt_chars[key]

    summary: dict[str, object] = {
        "source": "results/gemma-qwen-test-matrix/{qwen,gemma}/test-*/verdicts.jsonl",
        "rule": "p05 (index floor(0.05*n) ascending) rounded DOWN to 0.1, floored at 3.0",
        "records_per_substrate": EXPECTED_RECORDS,
        "substrates": {},
    }
    lines = [
        "# Render-budget calibration — committed test-matrix sample records (offline, $0)",
        "",
        "Rule (pinned in scripts/calibrate_render_budget.py before the numbers were read):",
        "pinned cpt = max(3.0, floor(p05 * 10) / 10); p05 = sorted ratio at index floor(0.05*n).",
        "Coverage (estimate_tokens >= input_tokens under the pinned cpt) is REPORT-ONLY.",
        "",
        "| substrate | served model | n | mean | p05 | min | pinned cpt | coverage |",
        "|-----------|--------------|---|------|-----|-----|------------|----------|",
    ]
    pinned: dict[str, float] = {}
    for sub, served in SUBSTRATES.items():
        records = substrate_records(MATRIX / sub)
        usable = [(mid, tid, toks) for mid, tid, toks in records if toks > 0]
        assert len(usable) == EXPECTED_RECORDS, (
            f"{sub}: expected {EXPECTED_RECORDS} usage-bearing sample records, got {len(usable)} "
            f"(of {len(records)} total)"
        )
        ratios = sorted(chars_for(mid, tid) / toks for mid, tid, toks in usable)
        mean = sum(ratios) / len(ratios)
        p05 = empirical_p05(ratios)
        cpt = pin_from_p05(p05)
        coverage = sum(
            1
            for mid, tid, toks in usable
            if estimate_tokens("x" * chars_for(mid, tid), cpt) >= toks
        ) / len(usable)
        pinned[served] = cpt
        summary["substrates"] = {
            **summary["substrates"],  # type: ignore[dict-item]
            served: {
                "n_records": len(usable),
                "mean_ratio": round(mean, 4),
                "p05_ratio": round(p05, 4),
                "min_ratio": round(min(ratios), 4),
                "max_ratio": round(max(ratios), 4),
                "pinned_chars_per_token": cpt,
                "retrodiction_coverage": round(coverage, 4),
            },
        }
        lines.append(
            f"| {sub} | {served} | {len(usable)} | {mean:.4f} | {p05:.4f} "
            f"| {min(ratios):.4f} | {cpt} | {coverage:.4f} |"
        )
    fallback = min(pinned.values())
    summary["pinned"] = {**pinned, "": fallback}
    lines += [
        "",
        f'Unknown-model fallback ("" entry): min of the per-substrate pins = {fallback}.',
        "Pinned into `_CALIBRATED_CPT` in src/agentmon/rendering.py.",
    ]

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (OUT / "table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwrote {OUT / 'table.md'} and {OUT / 'summary.json'}")


if __name__ == "__main__":
    main()
