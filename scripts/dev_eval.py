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
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentmon.calibration import run_calibrated  # noqa: E402
from agentmon.eval.metrics import PRICING, auroc, recall_at_fpr  # noqa: E402
from agentmon.eval.split import load_labels, load_split, parse_provenance  # noqa: E402
from agentmon.heuristic import heuristic_calibrated  # noqa: E402
from agentmon.llm.mock import MockLLMClient  # noqa: E402
from agentmon.monitors.registry import load_monitors  # noqa: E402
from agentmon.schemas import CalibratedVerdict, Transcript  # noqa: E402

LIBRARY_IDS = ["security_vuln", "reward_hacking", "scope_expansion", "deception", "generalist"]
LABELS_PATH = REPO / "datasets" / "synthetic" / "labels.jsonl"
SPLIT_PATH = REPO / "datasets" / "synthetic" / "split.json"
TRANSCRIPTS_DIR = REPO / "datasets" / "synthetic" / "transcripts"
PROMPTS_DIR = REPO / "src" / "agentmon" / "prompts"
SUBSAMPLE_PLAIN_COUNT = 4

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


def run_cost_usd(verdicts: list[CalibratedVerdict]) -> float | None:
    total = 0.0
    for verdict in verdicts:
        prices = PRICING.get(verdict.model)
        if prices is None:
            return None
        total += (
            verdict.total_input_tokens * prices[0] + verdict.total_output_tokens * prices[1]
        ) / 1e6
    return total


def n_calls(verdicts: list[CalibratedVerdict]) -> int:
    calls = 0
    for v in verdicts:
        calls += v.k + v.parse_retries + v.parse_repairs
        calls += sum(1 for outcome in v.verifications if outcome is not None and outcome.model)
    return calls


def summarize(
    verdicts: list[CalibratedVerdict], provs: dict[str, object], k: int
) -> tuple[str, dict]:
    by_monitor: dict[str, list[CalibratedVerdict]] = defaultdict(list)
    for v in verdicts:
        by_monitor[v.monitor_id].append(v)

    lines: list[str] = []
    summary: dict[str, dict] = {}
    for monitor_id in sorted(by_monitor):
        rows = by_monitor[monitor_id]
        scores = [r.calibrated_score for r in rows]
        flags = [provs[r.transcript_id].is_failure for r in rows]
        own = [
            (r, p)
            for r in rows
            if ((p := provs[r.transcript_id]).is_failure and p.label == monitor_id)
            or not p.is_failure
        ]
        own_scores = [r.calibrated_score for r, _ in own]
        own_flags = [p.is_failure for _, p in own]
        hn_rows = [r for r in rows if provs[r.transcript_id].stratum == "benign-hard-negative"]
        hn_flagged = [r.transcript_id for r in hn_rows if r.fraction_flagged > 0]
        benign_flagged = [
            r.transcript_id
            for r in rows
            if not provs[r.transcript_id].is_failure and r.fraction_flagged > 0
        ]
        missed = [
            (r.transcript_id, provs[r.transcript_id].stratum)
            for r in rows
            if provs[r.transcript_id].is_failure and r.fraction_flagged == 0
        ]
        own_missed = [(t, s) for t, s in missed if s.split("/")[0] == monitor_id]
        per_mode: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            p = provs[r.transcript_id]
            if p.is_failure:
                per_mode[p.stratum].append(r.fraction_flagged)
        entry = {
            "n": len(rows),
            "auroc_all": auroc(scores, flags),
            "auroc_own": auroc(own_scores, own_flags) if monitor_id != "generalist" else None,
            "recall_at_0fp": recall_at_fpr(scores, flags, 0.0),
            "recall_at_5pct": recall_at_fpr(scores, flags, 0.05),
            "hard_negatives_flagged": hn_flagged,
            "benign_flagged": benign_flagged,
            "failures_missed": missed,
            "own_failures_missed": own_missed,
            "mean_fraction_by_cell": {
                cell: sum(vals) / len(vals) for cell, vals in sorted(per_mode.items())
            },
            "parse_retries": sum(r.parse_retries for r in rows),
            "parse_repairs": sum(r.parse_repairs for r in rows),
            "verification_flips": sum(
                1 for r in rows for o in r.verifications if o is not None and not o.supported
            ),
            "mechanical_flips": sum(
                1
                for r in rows
                for o in r.verifications
                if o is not None and not o.supported and o.quote_match is False
            ),
        }
        summary[monitor_id] = entry

        def fmt(value: float | None) -> str:
            return "n/a" if value is None else f"{value:.3f}"

        lines.append(f"### {monitor_id}")
        lines.append(
            f"- AUROC all/own: {fmt(entry['auroc_all'])} / {fmt(entry['auroc_own'])} | "
            f"recall@0FP: {fmt(entry['recall_at_0fp'])} | recall@5%: {fmt(entry['recall_at_5pct'])}"
        )
        lines.append(
            f"- benign flagged ({len(benign_flagged)}): {', '.join(benign_flagged) or '-'} "
            f"(hard negatives: {', '.join(hn_flagged) or '-'})"
        )
        lines.append(f"- own-mode misses: {', '.join(f'{t}({s})' for t, s in own_missed) or '-'}")
        lines.append(
            "- mean fraction by cell: "
            + ("; ".join(f"{c}={v:.2f}" for c, v in entry["mean_fraction_by_cell"].items()) or "-")
        )
        lines.append(
            f"- flips: {entry['verification_flips']} "
            f"(mechanical {entry['mechanical_flips']}) | "
            f"retries {entry['parse_retries']} repairs {entry['parse_repairs']}"
        )
        lines.append("")
    return "\n".join(lines), summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--slice", required=True, choices=["dev-tg-sub", "dev-tg", "dev-full", "test"]
    )
    parser.add_argument("--label", required=True, help="run label, e.g. r0-baseline")
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--monitors", default=",".join(LIBRARY_IDS))
    parser.add_argument("--model-override", default=None)
    parser.add_argument("--out", type=Path, default=REPO / "out" / "phase3")
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
    if args.monitors == "heuristic":
        verdicts = heuristic_calibrated(transcripts)
    else:
        if args.mock:
            client = MockLLMClient()
            cache_dir = REPO / ".agentmon_cache" / "mock"
        else:
            from agentmon.llm.openai_compat import OpenAICompatClient

            client = OpenAICompatClient(
                extra_body=PRIMARY_EXTRA_BODY,
                min_interval_seconds=MIN_INTERVAL_SECONDS,
                max_attempts=5,
                backoff_seconds=5.0,
            )
            cache_dir = REPO / ".agentmon_cache"
        verdicts = run_calibrated(
            monitors,
            transcripts,
            client,
            k=args.k,
            cache_dir=cache_dir,
            verify=not args.no_verify,
            model_override=args.model_override,
        )

    run_dir = args.out / "runs" / args.label
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "verdicts.jsonl").open("w", encoding="utf-8") as handle:
        for verdict in verdicts:
            handle.write(verdict.model_dump_json() + "\n")

    table, summary = summarize(verdicts, provs, args.k)
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
        "model_override": args.model_override,
        "calls": calls,
        "cost_usd": cost,
        "prompt_sha256": prompt_hashes(),
        "timestamp": datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds"),
        "monitors": summary,
    }
    (run_dir / "summary.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    header = (
        f"## {args.label} — slice={args.slice} n={len(transcripts)} k={args.k} "
        f"verify={not args.no_verify} mock={args.mock} calls={calls} "
        f"cost={'n/a' if cost is None else f'${cost:.2f}'}\n"
    )
    (run_dir / "table.md").write_text(header + "\n" + table, encoding="utf-8")

    ledger = args.out / "requests.jsonl"
    live = not args.mock and args.monitors != "heuristic"
    today = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
    requests_made = getattr(client, "request_count", calls) if client is not None else 0
    if live:
        args.out.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "date": today,
                        "label": args.label,
                        "model": args.model_override or PRIMARY_MODEL,
                        "requests": requests_made,
                    }
                )
                + "\n"
            )

    print(header)
    print(table)
    if live:
        day_total = sum(
            entry["requests"]
            for line in ledger.read_text().splitlines()
            if line and (entry := json.loads(line))["date"] == today
        )
        print(
            f"REQUESTS: this run {requests_made}; today {day_total} of {PRIMARY_RPD} RPD "
            f"({PRIMARY_MODEL}). List-price-equivalent cost "
            f"{'n/a' if cost is None else f'${cost:.2f}'}; actual spend $0.00 (free tier)."
        )
        if day_total > PRIMARY_RPD * 0.9:
            print("!! >90% of daily request quota consumed — plan remaining runs for tomorrow.")


if __name__ == "__main__":
    main()
