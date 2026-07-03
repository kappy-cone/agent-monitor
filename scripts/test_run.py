"""Monitor-major Phase-4 test runner with the refined anomaly bands (DECISIONS 35/35a).

One monitor per invocation, all 66 test transcripts, k=3, frozen bodies. Honors
"halt before further spend" at the monitor boundary: pre-flight HALT checks run
before any call, post-row band checks gate whether the *next* monitor may start
(this script exits nonzero on HALT so the orchestrator stops).

A thin adapter over ``agentmon.eval`` (design 02): the freeze manifest and the
anomaly bands are versioned config (``configs/freeze/gate2.yaml``,
``configs/bands/<substrate>.yaml`` — values byte-identical to the constants they
replaced, pinned as literals in tests), and the exact halt semantics — refined
retry band (a *recovered* retry is IN-BAND; an unrecovered parse failure or a
>5% retry surge HALTS), per-row flag-rate window, own-mode-TP catch-loss > 50%
(DECISIONS 39), request ceiling 320, zero pre-existing cache hits on a fresh
row — live in ``agentmon.eval.gate.run_gate``, pinned by the parity suite.
Verification flip-rate and mechanical-flip fraction are REPORT-ONLY (the
pre/post decomposition). This script keeps only argparse, client wiring,
run-dir layout, printing, and ``enforce`` (band halt -> exit 2).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentmon.calibration import run_calibrated  # noqa: E402
from agentmon.eval import workspace  # noqa: E402
from agentmon.eval.accounting import (  # noqa: E402
    LedgerEntry,
    append_ledger,
    day_requests,
    quota_day,
)
from agentmon.eval.bands import load_bands  # noqa: E402
from agentmon.eval.gate import (  # noqa: E402
    GatePolicy,
    RunCounters,
    check_frozen,
    enforce,
    preflight_cache_hits,
    run_gate,
)
from agentmon.eval.split import load_labels, load_split, parse_provenance  # noqa: E402
from agentmon.eval.summary import render_calibrated_table, summarize_calibrated  # noqa: E402
from agentmon.llm import live  # noqa: E402
from agentmon.monitors.registry import load_monitors  # noqa: E402
from agentmon.schemas import Transcript  # noqa: E402

CACHE = workspace.CACHE_DIR
OUT = workspace.OUT_PHASE4
LEDGER = workspace.PHASE4_LEDGER
K = 3
ORDER = ["security_vuln", "scope_expansion", "reward_hacking", "deception", "generalist"]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--monitor", required=True, choices=ORDER)
    parser.add_argument(
        "--local", action="store_true", help="use the local Qwen via the omen tunnel"
    )
    parser.add_argument("--resume", action="store_true", help="allow pre-existing cache hits")
    args = parser.parse_args()
    mid = args.monitor

    drift = check_frozen(workspace.FREEZE_MANIFEST, workspace.PROMPTS_DIR, [mid])
    if drift:
        sys.exit(f"HALT: {drift[0]} — do not run.")

    labels = load_labels(workspace.LABELS_PATH)
    provs = {p.transcript_id: p for p in map(parse_provenance, labels)}
    split = load_split(workspace.SPLIT_PATH)
    test_ids = sorted(split.test_ids)
    transcripts = [
        Transcript.model_validate_json((workspace.TRANSCRIPTS_DIR / f"{tid}.json").read_text())
        for tid in test_ids
    ]
    monitor = load_monitors()[mid]

    client, model_override = live.build_live_client(args.local)
    effective_model = model_override or monitor.config.model

    # Bands are per-substrate versioned config: a gated row runs under the
    # target substrate's band file, never a borrowed one (DECISIONS 41 -> 42).
    band_path = workspace.BANDS_DIR / f"{effective_model}.yaml"
    if not band_path.exists():
        sys.exit(
            f"HALT: no band file for substrate {effective_model!r} ({band_path}). "
            "A gated row needs a committed band file (configs/bands/<substrate>.yaml)."
        )
    bands = load_bands(band_path)
    if bands.substrate != effective_model:
        sys.exit(
            f"HALT: band file {band_path.name} declares substrate {bands.substrate!r} "
            f"!= effective model {effective_model!r}."
        )

    hits = preflight_cache_hits(monitor, transcripts, K, 0.7, CACHE, effective_model)
    if hits and not args.resume:
        sys.exit(
            f"HALT: {hits} pre-existing sample-cache hits for {mid} on the test set before any "
            "call. A fresh test row must be all-fresh; pass --resume only to continue a walled row."
        )
    print(
        f"preflight OK: {mid} frozen, model={effective_model}, {len(test_ids)} test transcripts, "
        f"{hits} pre-existing cache hits (resume={args.resume})"
    )

    verdicts = run_calibrated(
        [monitor],
        transcripts,
        client,
        k=K,
        cache_dir=CACHE,
        verify=True,
        model_override=model_override,
    )

    policy = GatePolicy(bands=bands, halt_on_preexisting_cache_hits=not args.resume)
    counters = RunCounters(request_count=client.request_count, preexisting_cache_hits=hits)
    result = run_gate(mid, verdicts, provs, policy, counters)

    OUT.mkdir(parents=True, exist_ok=True)
    # Model-keyed run dir (DECISIONS 40 fix): each substrate writes under its own
    # effective_model namespace so a new substrate never clobbers a prior one's
    # verdicts (Qwen's un-namespaced runs/test-* stay put; Gemma lands under
    # runs/agentmon-local-gemma/test-*). Cache keys were already model-keyed.
    run_dir = OUT / "runs" / effective_model / f"test-{mid}"
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "verdicts.jsonl").open("w", encoding="utf-8") as fh:
        for v in verdicts:
            fh.write(v.model_dump_json() + "\n")
    summary = summarize_calibrated(verdicts, provs)
    table = render_calibrated_table(summary)
    flips = result.flip_report
    today = quota_day()
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "monitor": mid,
                "n": len(test_ids),
                "k": K,
                "requests": result.requests,
                "calls_accounted": result.calls_accounted,
                "cost_usd": result.cost_usd,
                "bands_ok": result.passed,
                "halt_reasons": result.halt_reasons,
                # The committed rows' 5-key report-only shape (member_refutations
                # is a composite-row field, structurally 0 on a leaf row).
                "verification_report": flips.model_dump(exclude={"member_refutations"}),
                "date": today,
                "monitors": {m: entry.model_dump(mode="json") for m, entry in summary.items()},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    append_ledger(
        LEDGER,
        LedgerEntry(
            date=today, label=f"test-{mid}", model=effective_model, requests=result.requests
        ),
    )

    print(f"\n## test-{mid} — n={len(test_ids)} k={K} model={effective_model}")
    print(table)
    cost_str = "n/a" if result.cost_usd is None else f"${result.cost_usd:.2f}"
    if args.local:
        print(
            f"REQUESTS: row {result.requests} (local {effective_model} via omen tunnel; "
            "no rate limit, actual $0.00 self-hosted)."
        )
    else:
        day_total = day_requests(LEDGER, today)
        print(
            f"REQUESTS: row {result.requests}; quota-day {today} {day_total}/{live.PRIMARY_RPD}. "
            f"list-eq cost {cost_str}; actual $0.00."
        )
    print(
        f"VERIFICATION (report-only): {flips.flips}/{flips.verif_calls} flips "
        f"({flips.flip_rate:.1%}), {flips.mechanical} mechanical "
        f"({flips.mech_fraction:.0%} of flips)."
    )
    enforce(result)
    print("\nBANDS OK — row final; next monitor cleared.")


if __name__ == "__main__":
    main()
