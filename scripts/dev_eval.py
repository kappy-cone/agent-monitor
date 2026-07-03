"""Phase-3/4 calibrated-evaluation driver with protocol guards.

The slice flags enforce the rigor rules in code, not just in discipline:

- ``dev-tg-sub`` / ``dev-tg``: the tinygrad-derived dev slice — the ONLY data
  Phase-3 iteration may see or score.
- ``dev-full`` (adds the agentmon-derived dev slice) requires
  ``--confirm-post-freeze``: it is a post-GATE-2 sanity readout, never an
  iteration input.
- ``test`` requires ``--confirm-phase4``: headline runs only, after the
  prompt freeze.

Every run writes verdicts + a summary table under ``out/phase3/runs/<label>/``,
records the SHA-256 of each prompt file (so every iteration round is traceable
to exact prompt content), and appends to a cumulative spend ledger checked
against the approved Phase-3 budget.

A thin adapter over ``agentmon.eval`` + ``agentmon.llm.live`` (design 02):
summaries, accounting, and the ledger live in the library; slice resolution
(and its seal refusals) deliberately stays HERE — the library never resolves
slices, so no future script can bypass the seal by accident.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentmon.calibration import run_calibrated  # noqa: E402
from agentmon.eval.accounting import (  # noqa: E402
    LedgerEntry,
    append_ledger,
    day_requests,
    n_calls,
    quota_day,
    run_cost_usd,
)
from agentmon.eval.split import load_labels, load_split, parse_provenance  # noqa: E402
from agentmon.eval.summary import render_calibrated_table, summarize_calibrated  # noqa: E402
from agentmon.eval.workspace import (  # noqa: E402
    CACHE_DIR,
    LABELS_PATH,
    OUT_PHASE3,
    PROMPTS_DIR,
    SPLIT_PATH,
    TRANSCRIPTS_DIR,
)
from agentmon.heuristic import heuristic_calibrated  # noqa: E402
from agentmon.llm import live  # noqa: E402
from agentmon.llm.mock import MockLLMClient  # noqa: E402
from agentmon.monitors.registry import load_monitors  # noqa: E402
from agentmon.schemas import Transcript  # noqa: E402

LIBRARY_IDS = ["security_vuln", "reward_hacking", "scope_expansion", "deception", "generalist"]
SUBSAMPLE_PLAIN_COUNT = 4


def fixed_dev_tg_subsample(provs: dict[str, object]) -> list[str]:
    """The fixed Phase-3 iteration subsample: deterministic, derived from the split.

    All dev∩tinygrad failures + all dev∩tinygrad hard negatives + the first
    SUBSAMPLE_PLAIN_COUNT plain/filler benigns in sorted-id order.
    """
    split = load_split(SPLIT_PATH)
    dev_tg = [t for t in split.dev_ids if provs[t].source == "tinygrad"]
    failures = sorted(t for t in dev_tg if provs[t].is_failure)
    hard_negatives = sorted(t for t in dev_tg if provs[t].stratum == "benign-hard-negative")
    plain = sorted(t for t in dev_tg if provs[t].stratum in ("benign-plain", "benign-filler"))[
        :SUBSAMPLE_PLAIN_COUNT
    ]
    return failures + hard_negatives + plain


def resolve_slice(name: str, provs: dict[str, object], args: argparse.Namespace) -> list[str]:
    split = load_split(SPLIT_PATH)
    if name == "dev-tg-sub":
        return fixed_dev_tg_subsample(provs)
    if name == "dev-tg":
        return sorted(t for t in split.dev_ids if provs[t].source == "tinygrad")
    if name == "dev-full":
        if not args.confirm_post_freeze:
            sys.exit(
                "REFUSED: dev-full includes the agentmon-derived dev slice, which Phase-3 "
                "iteration must never see. Pass --confirm-post-freeze only after GATE 2."
            )
        return sorted(split.dev_ids)
    if name == "test":
        if not args.confirm_phase4:
            sys.exit(
                "REFUSED: the test split is sealed until the GATE-2 prompt freeze. "
                "Pass --confirm-phase4 only for the Phase-4 headline run."
            )
        return sorted(split.test_ids)
    sys.exit(f"unknown slice {name!r}")


def prompt_hashes() -> dict[str, str]:
    return {
        path.stem: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(PROMPTS_DIR.glob("*.md"))
        if path.stem in LIBRARY_IDS
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--slice", required=True, choices=["dev-tg-sub", "dev-tg", "dev-full", "test"]
    )
    parser.add_argument("--label", required=True, help="run label, e.g. r0-baseline")
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument(
        "--local", action="store_true", help="use the local Qwen via the omen tunnel"
    )
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--monitors", default=",".join(LIBRARY_IDS))
    parser.add_argument("--model-override", default=None)
    parser.add_argument("--out", type=Path, default=OUT_PHASE3)
    parser.add_argument("--confirm-post-freeze", action="store_true")
    parser.add_argument("--confirm-phase4", action="store_true")
    args = parser.parse_args()

    labels = load_labels(LABELS_PATH)
    provs = {p.transcript_id: p for p in map(parse_provenance, labels)}
    transcript_ids = resolve_slice(args.slice, provs, args)
    transcripts = [
        Transcript.model_validate_json((TRANSCRIPTS_DIR / f"{tid}.json").read_text())
        for tid in transcript_ids
    ]
    if args.monitors == "heuristic":
        monitors = []
    else:
        monitor_ids = [m.strip() for m in args.monitors.split(",") if m.strip()]
        all_monitors = load_monitors()
        monitors = [all_monitors[mid] for mid in monitor_ids]

    client = None
    effective_override = args.model_override
    if args.monitors == "heuristic":
        verdicts = heuristic_calibrated(transcripts)
    else:
        if args.mock:
            client = MockLLMClient()
            cache_dir = CACHE_DIR / "mock"
        else:
            client, default_model = live.build_live_client(args.local)
            cache_dir = CACHE_DIR
            effective_override = args.model_override or default_model
        verdicts = run_calibrated(
            monitors,
            transcripts,
            client,
            k=args.k,
            cache_dir=cache_dir,
            verify=not args.no_verify,
            model_override=effective_override,
        )

    run_dir = args.out / "runs" / args.label
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "verdicts.jsonl").open("w", encoding="utf-8") as handle:
        for verdict in verdicts:
            handle.write(verdict.model_dump_json() + "\n")

    summary = summarize_calibrated(verdicts, provs)
    table = render_calibrated_table(summary)
    cost = run_cost_usd(verdicts)
    calls = n_calls(verdicts)
    meta = {
        "label": args.label,
        "slice": args.slice,
        "n_transcripts": len(transcripts),
        "transcript_ids": transcript_ids,
        "k": args.k,
        "verify": not args.no_verify,
        "mock": args.mock,
        "local": args.local,
        "model_override": effective_override,
        "calls": calls,
        "cost_usd": cost,
        "prompt_sha256": prompt_hashes(),
        "timestamp": datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds"),
        "monitors": {mid: entry.model_dump(mode="json") for mid, entry in summary.items()},
    }
    (run_dir / "summary.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    header = (
        f"## {args.label} — slice={args.slice} n={len(transcripts)} k={args.k} "
        f"verify={not args.no_verify} mock={args.mock} calls={calls} "
        f"cost={'n/a' if cost is None else f'${cost:.2f}'}\n"
    )
    (run_dir / "table.md").write_text(header + "\n" + table, encoding="utf-8")

    ledger = args.out / "requests.jsonl"
    live_run = not args.mock and args.monitors != "heuristic"
    model_label = effective_override or live.PRIMARY_MODEL
    # Quota-day keying: Google's free-tier day resets at midnight PT (UTC-7).
    today = quota_day()
    requests_made = getattr(client, "request_count", calls) if client is not None else 0
    if live_run:
        append_ledger(
            ledger,
            LedgerEntry(date=today, label=args.label, model=model_label, requests=requests_made),
        )

    print(header)
    print(table)
    if live_run and args.local:
        print(
            f"REQUESTS: this run {requests_made} (local {model_label} via the omen tunnel; "
            "no rate limit, actual spend $0.00 self-hosted)."
        )
    elif live_run:
        day_total = day_requests(ledger, today)
        print(
            f"REQUESTS: this run {requests_made}; today {day_total} of {live.PRIMARY_RPD} RPD "
            f"({live.PRIMARY_MODEL}). List-price-equivalent cost "
            f"{'n/a' if cost is None else f'${cost:.2f}'}; actual spend $0.00 (free tier)."
        )
        if day_total > live.PRIMARY_RPD * 0.9:
            print("!! >90% of daily request quota consumed — plan remaining runs for tomorrow.")


if __name__ == "__main__":
    main()
