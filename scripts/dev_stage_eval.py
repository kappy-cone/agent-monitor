"""Dev evidence runner for the promotion-candidate verification stages (design 04 Phase 4).

Two arms, each comparing an explicit-v1 baseline against a candidate pipeline
over dev∩tinygrad (n≈37, k=3) on both self-hosted substrates, with the
per-stage confusion accounting from ``agentmon.eval.stage_report``:

- ``--arm event-order`` (offline, zero completions): candidate =
  ``[QuoteGroundingStage, EventOrderStage, SemanticVerificationStage]``,
  replayed entirely from the existing verify=True caches through a client
  that raises on any live call. Expected result (measured during design
  review): ZERO flip deltas — the run certifies the zero-kill /
  metrics-identical insurance contract, never F-A suppression.
- ``--arm claim-grounding`` (live-dev, requires --execute): candidate =
  ``PIPELINES[--pipeline]`` (default v2). Fresh calls are exactly the
  ``kind="claim_grounding"`` entries on deception-category flags (ScopedStage
  gates on the FLAG's categories, so the generalist's deception flags are in
  scope); every other stage replays from cache. Also scores the 12-transcript
  claim-grounding panel (the six vendored injected cases plus their matched
  benign pairs) and reports confirms per side — a confirm on a benign twin is
  a shielded FP, the automatic-reject signal.

Scope guards: dev ids only (a stray test id aborts), FLAG_BANDS untouched,
the candidate pipeline's stage ids are stamped into summary.json (the
pipeline-version divergence guard). Writes
``results/dev-stage-probe/{table.md,summary.json}``.

Run: env -u UV_PYTHON uv run python scripts/dev_stage_eval.py --arm event-order [--execute]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentmon.calibration import run_calibrated  # noqa: E402
from agentmon.eval.metrics import auroc, recall_at_fpr  # noqa: E402
from agentmon.eval.split import Provenance, load_labels, load_split, parse_provenance  # noqa: E402
from agentmon.eval.stage_report import (  # noqa: E402
    flip_deltas,
    stage_confusion,
    transcript_catch_loss,
)
from agentmon.eval.workspace import (  # noqa: E402
    CACHE_DIR,
    LABELS_PATH,
    RESULTS_DIR,
    SPLIT_PATH,
    TRANSCRIPTS_DIR,
)
from agentmon.monitors.registry import load_monitors  # noqa: E402
from agentmon.schemas import CalibratedVerdict, Transcript  # noqa: E402
from agentmon.verification import (  # noqa: E402
    PIPELINES,
    EventOrderStage,
    QuoteGroundingStage,
    SemanticVerificationStage,
    VerificationPipeline,
)

OUT = RESULTS_DIR / "dev-stage-probe"
PANEL_DIR = REPO / "tests" / "fixtures" / "claim_grounding_dev"
LEAVES = ["security_vuln", "reward_hacking", "scope_expansion", "deception", "generalist"]
#: served names as recorded in the cache namespacing (DECISIONS 37, 41).
SUBSTRATES = {"qwen": "agentnom-local", "gemma": "agentmon-local-gemma"}
K = 3
STAGE_IDS = ("event_order", "claim_grounding", "grounded_semantic", "semantic")


class _NoCallClient:
    """Raises on any live call — proves a replay arm is served from cache."""

    def complete(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("cache MISS: a live call was attempted on a replay arm")


def event_order_candidate() -> VerificationPipeline:
    """The Phase-4a arm: v1 plus the deterministic order proof, nothing else."""
    return VerificationPipeline(
        [QuoteGroundingStage(), EventOrderStage(), SemanticVerificationStage()]
    )


def load_dev_tg() -> tuple[list[Transcript], dict[str, Provenance], set[str]]:
    split = load_split(SPLIT_PATH)
    provs = {p.transcript_id: p for p in map(parse_provenance, load_labels(LABELS_PATH))}
    dev_tg = sorted(t for t in split.dev_ids if provs[t].source == "tinygrad")
    test_ids = set(split.test_ids)
    leaked = [t for t in dev_tg if t in test_ids]
    assert not leaked, f"TEST SEAL VIOLATION: {leaked} are in the test split"
    transcripts = [
        Transcript.model_validate_json((TRANSCRIPTS_DIR / f"{t}.json").read_text(encoding="utf-8"))
        for t in dev_tg
    ]
    return transcripts, provs, test_ids


def load_panel(test_ids: set[str]) -> tuple[list[Transcript], dict[str, str]]:
    """The 12-transcript panel: six vendored injected cases + matched benign pairs.

    The injected transcripts are vendored fixtures (dataset-expansion ids, not
    in the split); the benign pairs read from the main dataset and must be
    dev-side. Every id is asserted against the sealed test split.
    """
    transcripts: list[Transcript] = []
    labels: dict[str, str] = {}
    for line in (PANEL_DIR / "ground_truth.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        tid, pair = row["transcript_id"], json.loads(row["notes"])["pair_with"]
        assert tid not in test_ids and pair not in test_ids, (
            f"TEST SEAL VIOLATION: panel id {tid}/{pair} is in the test split"
        )
        transcripts.append(
            Transcript.model_validate_json((PANEL_DIR / f"{tid}.json").read_text(encoding="utf-8"))
        )
        transcripts.append(
            Transcript.model_validate_json(
                (TRANSCRIPTS_DIR / f"{pair}.json").read_text(encoding="utf-8")
            )
        )
        labels[tid] = row["label"]
        labels[pair] = "benign"
    return transcripts, labels


def metric_row(verdicts: list[CalibratedVerdict], provs: dict[str, Provenance]) -> dict:
    scores = [v.calibrated_score for v in verdicts]
    flags = [provs[v.transcript_id].is_failure for v in verdicts]
    return {
        "auroc": auroc(scores, flags),
        "recall_at_5pct": recall_at_fpr(scores, flags, 0.05),
    }


def confirms_in(verdicts: list[CalibratedVerdict]) -> int:
    """Draws decided by a terminal uphold (the confirm count, from stage records)."""
    count = 0
    for v in verdicts:
        for outcomes in v.stage_outcomes or []:
            for outcome in outcomes or []:
                if outcome.supported is True and outcome.terminal:
                    count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--arm", required=True, choices=["event-order", "claim-grounding"])
    parser.add_argument(
        "--pipeline",
        default="v2",
        choices=sorted(PIPELINES),
        help="candidate pipeline for the claim-grounding arm (the event-order "
        "arm always runs the fixed [quote, event_order, semantic] assembly)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="required for the claim-grounding arm (fresh claim_grounding calls, $0 self-hosted)",
    )
    args = parser.parse_args()

    transcripts, provs, test_ids = load_dev_tg()
    monitors = load_monitors()
    labels = {p.transcript_id: p.label for p in provs.values()}

    if args.arm == "event-order":
        candidate_name = "quote+event_order+semantic"
        candidate = event_order_candidate()
        client: object = _NoCallClient()
    else:
        candidate_name = args.pipeline
        candidate = PIPELINES[args.pipeline]()
        if not args.execute:
            print(
                "PLAN: claim-grounding arm — fresh kind='claim_grounding' calls on "
                "deception-category dev∩tg flags plus the 12-transcript panel "
                "(<= ~90 per substrate, $0 self-hosted). Pass --execute to run."
            )
            return
        from agentmon.llm import live

        client, _ = live.build_live_client(local=True)

    stage_ids = [stage.stage_id for stage in candidate.stages]
    lines = [
        f"# Dev stage probe — arm={args.arm} candidate={candidate_name} "
        f"stages={stage_ids} n={len(transcripts)} k={K} verify=ON",
        "",
        "| substrate | monitor | AUROC v1 | AUROC cand | r@5% v1 | r@5% cand "
        "| deltas | catch-loss |",
        "|---|---|---|---|---|---|---|---|",
    ]
    summary: dict[str, object] = {
        "arm": args.arm,
        "candidate": candidate_name,
        "pipeline_stage_ids": stage_ids,  # the version-divergence guard
        "slice": "dev∩tinygrad",
        "n": len(transcripts),
        "k": K,
    }
    rows: dict[str, object] = {}
    for sub, served in SUBSTRATES.items():
        for mid in LEAVES:
            monitor = monitors[mid]
            baseline = run_calibrated(
                [monitor],
                transcripts,
                client,
                k=K,
                cache_dir=CACHE_DIR,  # type: ignore[arg-type]
                verify=True,
                pipeline=PIPELINES["v1"](),
                model_override=served,
            )
            cand = run_calibrated(
                [monitor],
                transcripts,
                client,
                k=K,
                cache_dir=CACHE_DIR,  # type: ignore[arg-type]
                verify=True,
                pipeline=candidate,
                model_override=served,
            )
            deltas = flip_deltas(baseline, cand, labels)
            lost = transcript_catch_loss(baseline, cand, labels)
            confusion = {
                sid: dataclasses.asdict(stage_confusion(deltas, cand, sid))
                for sid in STAGE_IDS
                if sid in stage_ids
            }
            base_m, cand_m = metric_row(baseline, provs), metric_row(cand, provs)
            rows[f"{sub}/{mid}"] = {
                "baseline": base_m,
                "candidate": cand_m,
                "deltas": len(deltas),
                "catch_loss": lost,
                "stage_confusion": confusion,
            }
            lines.append(
                f"| {sub} | {mid} | {base_m['auroc']} | {cand_m['auroc']} "
                f"| {base_m['recall_at_5pct']} | {cand_m['recall_at_5pct']} "
                f"| {len(deltas)} | {lost} |"
            )
    summary["rows"] = rows

    if args.arm == "claim-grounding":
        panel_transcripts, panel_labels = load_panel(test_ids)
        panel: dict[str, object] = {}
        for sub, served in SUBSTRATES.items():
            verdicts = run_calibrated(
                [monitors["deception"]],
                panel_transcripts,
                client,
                k=K,  # type: ignore[arg-type]
                cache_dir=CACHE_DIR,
                verify=True,
                pipeline=candidate,
                model_override=served,
            )
            panel[sub] = {
                v.transcript_id: {
                    "label": panel_labels[v.transcript_id],
                    "fraction_flagged": v.fraction_flagged,
                    "confirms": confirms_in([v]),
                }
                for v in verdicts
            }
        summary["panel"] = panel
        lines.append("")
        lines.append("Panel (six injected + matched benign pairs; a confirm on a benign twin")
        lines.append("is a shielded FP — automatic reject): see summary.json['panel'].")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print("\n".join(lines))
    print(f"\nwrote {OUT / 'table.md'} and {OUT / 'summary.json'}")


if __name__ == "__main__":
    main()
