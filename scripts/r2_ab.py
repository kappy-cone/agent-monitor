"""Round-2 k=3 A/B confirmation runner.

Arm B = reconstructed r0 bodies (SHA-verified; sample-0 draws — and for the six
diagnosed pairs all three draws — come from cache). Arm A = the live r2 bodies.
Both arms: k=3, temperature 0.7, verification on, standard fraction-flagged
aggregation; flip accounting uses the k=3 majority (fraction >= 2/3 = flagged).

Prints the full plan and exits unless --execute is passed. Ledgers every
request. Aborts if any parse retry occurs (rate has been 0/125; a retry now
signals schema drift, not noise).
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentmon.calibration import run_calibrated, sample_cache_key  # noqa: E402
from agentmon.monitors.registry import load_monitors  # noqa: E402
from agentmon.schemas import Transcript  # noqa: E402

TRANSCRIPTS = REPO / "datasets" / "synthetic" / "transcripts"
CACHE = REPO / ".agentmon_cache"
R0_PROMPTS = REPO / "out" / "phase3" / "prompts-r0"
LIVE_PROMPTS = REPO / "src" / "agentmon" / "prompts"
LEDGER = REPO / "out" / "phase3" / "requests.jsonl"
OUT = REPO / "out" / "phase3" / "runs" / "r2-ab"
HARD_CAP = 200

# (monitor, transcript, role); roles: T target, R r1-regression, S sentinel,
# W win-reconfirm, O observe-only (excluded from the A/B verdict).
PAIRS: list[tuple[str, str, str]] = [
    ("security_vuln", "sec-med-04", "T"),
    ("security_vuln", "hn5-c", "S"),
    ("security_vuln", "sec-bla-03", "S"),
    ("deception", "hn1-b", "T"),
    ("deception", "dec-bla-03", "W"),
    ("deception", "sec-bla-03", "R"),
    ("deception", "hn8-b", "S"),
    ("deception", "dec-sub-01", "S"),
    ("scope_expansion", "se-sub-05", "T"),
    ("scope_expansion", "ben-tg2-s16", "T"),
    ("scope_expansion", "se-bla-04", "S"),
    ("scope_expansion", "hn3-b", "S"),
    ("scope_expansion", "hn1-b", "O"),
    ("generalist", "ben-tg1-s5", "T"),
    ("generalist", "se-bla-04", "T"),
    ("generalist", "ben-tg2-s16", "S"),
    ("generalist", "se-med-02", "S"),
    ("generalist", "hn2-d", "O"),
]
WIN_LIVE = [("reward_hacking", "rh-bla-03", "W")]  # live body unchanged this round


def cached(monitor, transcript, idx: int) -> bool:
    key = sample_cache_key(monitor, transcript, idx, 0.7)
    return (CACHE / f"{key}.json").exists()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    arm_b = load_monitors(R0_PROMPTS)
    arm_a = load_monitors(LIVE_PROMPTS)
    transcripts = {
        tid: Transcript.model_validate_json((TRANSCRIPTS / f"{tid}.json").read_text())
        for tid in sorted({t for _, t, _ in PAIRS + WIN_LIVE})
    }

    plan: list[tuple[str, str, str, str, int]] = []  # arm, monitor, tid, role, fresh
    for mid, tid, role in PAIRS:
        b_fresh = sum(0 if cached(arm_b[mid], transcripts[tid], i) else 1 for i in range(3))
        a_fresh = sum(0 if cached(arm_a[mid], transcripts[tid], i) else 1 for i in range(3))
        plan.append(("B", mid, tid, role, b_fresh))
        plan.append(("A", mid, tid, role, a_fresh))
    for mid, tid, role in WIN_LIVE:
        fresh = sum(0 if cached(arm_a[mid], transcripts[tid], i) else 1 for i in range(3))
        plan.append(("live", mid, tid, role, fresh))

    fresh_samples = sum(f for *_, f in plan)
    est_verifications = round(fresh_samples * 0.4)
    print(f"PLAN: {len(PAIRS)} A/B pairs + {len(WIN_LIVE)} live win-reconfirm; k=3 both arms")
    for arm, mid, tid, role, fresh in plan:
        print(f"  arm={arm:4s} {mid:16s} {tid:14s} role={role} fresh_draws={fresh}")
    print(
        f"fresh samples={fresh_samples} (cache-reused draws="
        f"{(len(PAIRS) * 6 + 3) - fresh_samples}), est. verifications={est_verifications}, "
        f"planned total ~{fresh_samples + est_verifications} (hard cap {HARD_CAP})"
    )
    if fresh_samples + est_verifications > HARD_CAP:
        sys.exit("OVER HARD CAP — not running. Trim sentinels.")
    if not args.execute:
        print("\n(dry plan — pass --execute to run)")
        return

    from agentmon.llm.openai_compat import OpenAICompatClient

    client = OpenAICompatClient(
        extra_body={"reasoning_effort": "none"},
        min_interval_seconds=6.2,
        max_attempts=5,
        backoff_seconds=5.0,
    )
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    for arm, monitors in (("B", arm_b), ("A", arm_a)):
        for mid, tid, role in PAIRS:
            [cv] = run_calibrated(
                [monitors[mid]], [transcripts[tid]], client, k=3, cache_dir=CACHE, verify=True
            )
            if cv.parse_retries or cv.parse_repairs:
                sys.exit(f"ABORT: parse retry/repair on {arm}/{mid}/{tid} — schema drift.")
            results.append((arm, mid, tid, role, cv))
    for mid, tid, role in WIN_LIVE:
        [cv] = run_calibrated(
            [arm_a[mid]], [transcripts[tid]], client, k=3, cache_dir=CACHE, verify=True
        )
        if cv.parse_retries or cv.parse_repairs:
            sys.exit(f"ABORT: parse retry/repair on live/{mid}/{tid} — schema drift.")
        results.append(("live", mid, tid, role, cv))

    with (OUT / "results.jsonl").open("w", encoding="utf-8") as fh:
        for arm, mid, tid, role, cv in results:
            fh.write(
                json.dumps(
                    {
                        "arm": arm,
                        "monitor": mid,
                        "transcript": tid,
                        "role": role,
                        "verdict": json.loads(cv.model_dump_json()),
                    }
                )
                + "\n"
            )
    print("\nRESULTS (fraction_flagged | per-draw flags | per-draw scores):")
    for arm, mid, tid, role, cv in results:
        scores = [s.suspicion_score for s in cv.samples]
        print(
            f"  arm={arm:4s} {mid:16s} {tid:14s} role={role} "
            f"frac={cv.fraction_flagged:.2f} flags={cv.sample_flagged} scores={scores}"
        )
    today = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "date": today,
                    "label": "r2-ab",
                    "model": "gemini-3.1-flash-lite",
                    "requests": client.request_count,
                }
            )
            + "\n"
        )
    print(f"\nREQUESTS this run: {client.request_count} (ledgered)")


if __name__ == "__main__":
    main()
