"""Verified-composite dev probe on the tinygrad dev slice (offline, $0, zero completions).

Design 03 Phase 3: the verify-ON baseline the verified ensemble has never had.
Every number is produced by driving a self-verifying composite through
``run_calibrated`` with a client that raises on any live call — member draws
AND member verifications replay from the machine-local ``.agentmon_cache``
populated by the per-substrate verify=True dev runs. The two-pass
``replay_coverage`` preflight converts that machine-local dependency into a
HALT instead of a silent gap.

Correctness self-checks (the probe ABORTS if any fails):
1. singleton verified-ensemble ≡ committed leaf — reproduces the committed
   verify=True dev verdicts' ``sample_flagged`` / ``fraction_flagged`` /
   ``calibrated_score`` and per-sample ``supported`` for all 5 monitors x 37
   transcripts x 2 substrates.
2. verify-OFF regression — plain-ensemble rows reproduce
   ``results/dev-composite-probe/summary.json`` exactly.
3. zero-completion proof — ``_NoCallClient`` + full two-pass coverage.
4. cost honesty — one composite verdict's totals equal the hand-summed member
   draw + verification tokens.

Pre-registered candidates: ``vens[4-specialists]`` and
``vens[4-spec+generalist]`` per substrate (the two pending FINDINGS rows),
cross-substrate verified ensembles, ``vcasc[gen->vens4]``,
``vcasc[gen|heur->vens4]``, plus the heuristic's dev gate recall and benign
flag rate. Composite dev flag rates and cascade escalation rates are recorded
and their blind-to-test bands printed via the pinned D39/42 rule — bands for a
future gated read, never applied to test here.

Scope guards: dev∩tinygrad only (a stray test id aborts); ``FLAG_BANDS``
untouched; zero cache writes; results land in
``results/dev-composite-probe-verified/`` (a new directory). Pipeline: the
frozen v1 default for every member verification (stamped in summary.json).

Run: env -u UV_PYTHON uv run python scripts/dev_composite_verified_eval.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentmon.calibration import run_calibrated  # noqa: E402
from agentmon.eval.bands import derive_band, raw_flag_rate  # noqa: E402
from agentmon.eval.gate import (  # noqa: E402
    composite_catch_loss,
    composite_flip_report,
    gate_miss_report,
    own_modes,
    replay_coverage,
)
from agentmon.eval.metrics import auroc, recall_at_fpr  # noqa: E402
from agentmon.eval.split import Provenance, load_labels, load_split, parse_provenance  # noqa: E402
from agentmon.eval.workspace import (  # noqa: E402
    CACHE_DIR,
    LABELS_PATH,
    RESULTS_DIR,
    SPLIT_PATH,
    TRANSCRIPTS_DIR,
)
from agentmon.heuristic import HeuristicMonitor, heuristic_calibrated  # noqa: E402
from agentmon.monitors.composite import (  # noqa: E402
    CascadeMonitor,
    EnsembleMonitor,
    Member,
    VerifiedEnsembleMonitor,
)
from agentmon.monitors.registry import load_monitors  # noqa: E402
from agentmon.schemas import CalibratedVerdict, Transcript  # noqa: E402

OUT = RESULTS_DIR / "dev-composite-probe-verified"
PROBE_SUMMARY = RESULTS_DIR / "dev-composite-probe" / "summary.json"
SPECIALISTS = ["security_vuln", "reward_hacking", "scope_expansion", "deception"]
LEAVES = [*SPECIALISTS, "generalist"]
#: served names as recorded in the cache namespacing (DECISIONS 37, 41).
SUBSTRATES = {"qwen": "agentnom-local", "gemma": "agentmon-local-gemma"}
#: committed verify=True dev verdicts (reconciliation only, never re-scored).
COMMITTED = {
    "qwen": REPO / "out" / "phase3" / "runs" / "qwen-devbands" / "verdicts.jsonl",
    "gemma": RESULTS_DIR / "gemma-qwen-test-matrix" / "gemma-devbands" / "verdicts.jsonl",
}
K = 3


class _NoCallClient:
    """Raises on any live call — proves every draw AND verification replays."""

    def complete(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("cache MISS: a live call was attempted (replay violated)")


def load_dev_tg() -> tuple[list[str], list[Transcript], dict[str, Provenance]]:
    split = load_split(SPLIT_PATH)
    provs = {p.transcript_id: p for p in map(parse_provenance, load_labels(LABELS_PATH))}
    dev_tg = sorted(t for t in split.dev_ids if provs[t].source == "tinygrad")
    leaked = [t for t in dev_tg if t in set(split.test_ids)]
    assert not leaked, f"TEST SEAL VIOLATION: {leaked} are in the test split"
    transcripts = [
        Transcript.model_validate_json((TRANSCRIPTS_DIR / f"{t}.json").read_text(encoding="utf-8"))
        for t in dev_tg
    ]
    return dev_tg, transcripts, provs


def committed_rows(sub: str, dev_tg: set[str]) -> dict[str, dict[str, CalibratedVerdict]]:
    """monitor_id -> transcript_id -> committed verify=True dev verdict."""
    rows: dict[str, dict[str, CalibratedVerdict]] = {}
    for line in COMMITTED[sub].read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        v = CalibratedVerdict.model_validate_json(line)
        if v.transcript_id in dev_tg:
            rows.setdefault(v.monitor_id, {})[v.transcript_id] = v
    return rows


def replayed_supported(v: CalibratedVerdict, member_name: str) -> list[bool | None]:
    """Per-sample member verification votes from a singleton vens' provenance."""
    out: list[bool | None] = []
    for sample in v.samples:
        prov = sample.provenance
        entry = prov.member_verifications.get(member_name) if prov is not None else None
        out.append(entry.supported if entry is not None else None)
    return out


def committed_supported(v: CalibratedVerdict) -> list[bool | None]:
    return [o.supported if o is not None else None for o in v.verifications]


def metrics(verdicts: list[CalibratedVerdict], provs: dict[str, Provenance]) -> dict:
    scores = [v.calibrated_score for v in verdicts]
    flags = [provs[v.transcript_id].is_failure for v in verdicts]
    return {
        "auroc": auroc(scores, flags),
        "recall_at_0fp": recall_at_fpr(scores, flags, 0.0),
        "recall_at_5pct": recall_at_fpr(scores, flags, 0.05),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.parse_args()

    dev_tg, transcripts, provs = load_dev_tg()
    monitors = load_monitors()
    client = _NoCallClient()

    def replay(evaluator: object, model: str, verify: bool = True) -> list[CalibratedVerdict]:
        return run_calibrated(
            [evaluator],  # type: ignore[list-item]
            transcripts,
            client,  # type: ignore[arg-type]
            k=K,
            cache_dir=CACHE_DIR,
            verify=verify,
            model_override=model,
        )

    def coverage_or_halt(evaluator: object, model: str, label: str) -> tuple[int, int]:
        hits, expected = replay_coverage(evaluator, transcripts, K, model, CACHE_DIR)  # type: ignore[arg-type]
        if hits != expected:
            sys.exit(f"HALT: replay coverage {hits}/{expected} for {label} — not fully cached")
        return hits, expected

    lines: list[str] = [
        f"# Dev verified-composite probe — dev∩tinygrad n={len(dev_tg)} k={K} "
        "verify=ON (per-member verify-then-OR)",
        "# Zero completions: _NoCallClient + full two-pass replay coverage.",
        "# Member verification pipeline: v1 (the frozen default).",
        "",
    ]
    summary: dict[str, object] = {
        "slice": "dev∩tinygrad",
        "n": len(dev_tg),
        "k": K,
        "pipeline": "v1",
        "pipeline_stage_ids": ["quote_grounding", "semantic"],
    }

    # ----- self-check 1: singleton verified-ensemble ≡ committed leaf -----
    singleton_ok = True
    for sub, model in SUBSTRATES.items():
        committed = committed_rows(sub, set(dev_tg))
        for mid in LEAVES:
            solo = VerifiedEnsembleMonitor(mid, [Member(monitors[mid])])
            coverage_or_halt(solo, model, f"singleton {mid}@{sub}")
            replayed = {v.transcript_id: v for v in replay(solo, model)}
            for tid in dev_tg:
                mine, theirs = replayed[tid], committed[mid][tid]
                same = (
                    mine.sample_flagged == theirs.sample_flagged
                    and mine.fraction_flagged == theirs.fraction_flagged
                    and mine.calibrated_score == theirs.calibrated_score
                    and replayed_supported(mine, mid) == committed_supported(theirs)
                )
                if not same:
                    singleton_ok = False
                    print(f"MISMATCH singleton {mid}@{sub} {tid}")
    if not singleton_ok:
        sys.exit("ABORT: singleton verified-ensemble != committed leaf (self-check 1)")
    lines.append("self-check 1 (singleton vens == committed leaf, 5x37x2): OK")
    summary["singleton_equals_committed_leaf"] = True

    # ----- self-check 2: verify-OFF plain ensembles reproduce the committed probe -----
    probe = json.loads(PROBE_SUMMARY.read_text(encoding="utf-8"))
    for sub, model in SUBSTRATES.items():
        for comp_id, member_ids in (
            ("ens[4-specialists]", SPECIALISTS),
            ("ens[4-spec+generalist]", LEAVES),
        ):
            plain = EnsembleMonitor(comp_id, [Member(monitors[m]) for m in member_ids])
            m = metrics(replay(plain, model, verify=False), provs)
            want = probe["3a"][f"{sub}/{comp_id}"]
            if m != want:
                sys.exit(f"ABORT: verify-OFF regression {sub}/{comp_id}: {m} != {want}")
    lines.append("self-check 2 (verify-OFF rows == results/dev-composite-probe): OK")
    summary["verify_off_regression"] = True

    # ----- candidates, verify ON -----
    lines += [
        "",
        "## verified composites (per-member verify-then-OR, v1 member verification)",
        "| candidate | substrate | AUROC | r@0FP | r@5%FPR | flag% | band | escal% |",
        "|---|---|---|---|---|---|---|---|",
    ]
    rows: dict[str, object] = {}
    cost_checked = False

    def emit_row(comp: object, comp_id: str, sub: str, model: str) -> list[CalibratedVerdict]:
        nonlocal cost_checked
        coverage = coverage_or_halt(comp, model, f"{comp_id}@{sub}")
        verdicts = replay(comp, model)
        m = metrics(verdicts, provs)
        rate = raw_flag_rate(verdicts)
        rate_pct = round(100 * rate, 1)
        band = derive_band(rate_pct)
        draws = sum(len(v.samples) for v in verdicts)
        escalated = sum(
            1 for v in verdicts for s in v.samples if s.provenance and s.provenance.escalated
        )
        modes = own_modes(comp)  # type: ignore[arg-type]
        lost, own_tp = composite_catch_loss(verdicts, provs, modes)
        record: dict[str, object] = {
            **m,
            "flag_rate_pct": rate_pct,
            "band": [band.lo, band.hi],
            "escalation_rate": escalated / draws,
            "coverage": list(coverage),
            "flip_report": composite_flip_report(verdicts),
            "own_tp": own_tp,
            "catch_loss": lost,
        }
        if isinstance(comp, CascadeMonitor):
            record["gate_miss"] = gate_miss_report(verdicts, provs, modes)
            record["escalation_band"] = [
                derive_band(round(100 * escalated / draws, 1)).lo,
                derive_band(round(100 * escalated / draws, 1)).hi,
            ]
        rows[f"{sub}/{comp_id}"] = record
        lines.append(
            f"| {comp_id} | {sub} | {m['auroc']} | {m['recall_at_0fp']} | {m['recall_at_5pct']} "
            f"| {rate_pct} | [{band.lo},{band.hi}] | {escalated / draws:.3f} |"
        )
        # self-check 4 (cost honesty), once: totals == hand-summed member tokens.
        if not cost_checked and isinstance(comp, VerifiedEnsembleMonitor):
            check = verdicts[0]
            leaf_in = leaf_out = 0
            for member in comp.members:
                member_model = member.model_override or model
                [leaf_cv] = run_calibrated(
                    [member.monitor],
                    [transcripts[0]],
                    client,
                    k=K,  # type: ignore[list-item,arg-type]
                    cache_dir=CACHE_DIR,
                    verify=False,
                    model_override=member_model,
                )
                leaf_in += sum(s.input_tokens for s in leaf_cv.samples)
                leaf_out += sum(s.output_tokens for s in leaf_cv.samples)
            verif_in = verif_out = 0
            for sample in check.samples:
                for outcome in (sample.provenance.member_verifications or {}).values():  # type: ignore[union-attr]
                    verif_in += outcome.input_tokens
                    verif_out += outcome.output_tokens
            assert check.total_input_tokens == leaf_in + verif_in, "cost honesty (input)"
            assert check.total_output_tokens == leaf_out + verif_out, "cost honesty (output)"
            lines.append(f"self-check 4 (cost honesty on {comp_id}/{check.transcript_id}): OK")
            cost_checked = True
        return verdicts

    for sub, model in SUBSTRATES.items():
        vens4 = VerifiedEnsembleMonitor(
            "vens[4-specialists]", [Member(monitors[m]) for m in SPECIALISTS]
        )
        vens5 = VerifiedEnsembleMonitor(
            "vens[4-spec+generalist]", [Member(monitors[m]) for m in LEAVES]
        )
        emit_row(vens4, "vens[4-specialists]", sub, model)
        emit_row(vens5, "vens[4-spec+generalist]", sub, model)
        deep4 = VerifiedEnsembleMonitor(
            "vens[4-specialists]", [Member(monitors[m]) for m in SPECIALISTS]
        )
        cascade = CascadeMonitor("vcasc[gen->vens4]", Member(monitors["generalist"]), Member(deep4))
        emit_row(cascade, "vcasc[gen->vens4]", sub, model)
        widened_gate = EnsembleMonitor(
            "gate[gen|heur]", [Member(monitors["generalist"]), Member(HeuristicMonitor())]
        )
        widened = CascadeMonitor(
            "vcasc[gen|heur->vens4]",
            Member(widened_gate),
            Member(
                VerifiedEnsembleMonitor(
                    "vens[4-specialists]", [Member(monitors[m]) for m in SPECIALISTS]
                )
            ),
        )
        emit_row(widened, "vcasc[gen|heur->vens4]", sub, model)

    # cross-substrate verified ensembles (Qwen ⊕ Gemma), one per leaf.
    for mid in LEAVES:
        xsub = VerifiedEnsembleMonitor(
            f"xsub-v[{mid}]",
            [
                Member(monitors[mid], model_override=SUBSTRATES["qwen"]),
                Member(monitors[mid], model_override=SUBSTRATES["gemma"]),
            ],
        )
        emit_row(xsub, f"xsub-v[{mid}]", "x-sub", SUBSTRATES["qwen"])
    summary["rows"] = rows

    # ----- heuristic tier: dev gate recall + benign flag rate (measure only) -----
    heuristic_rows = heuristic_calibrated(transcripts)
    failures = [t for t in dev_tg if provs[t].is_failure]
    caught = {v.transcript_id for v in heuristic_rows if v.fraction_flagged > 0}
    h_gate_recall = sum(1 for t in failures if t in caught) / len(failures)
    benign_ids = [t for t in dev_tg if not provs[t].is_failure]
    h_benign_rate = sum(1 for t in benign_ids if t in caught) / len(benign_ids)
    summary["heuristic"] = {"dev_gate_recall": h_gate_recall, "benign_flag_rate": h_benign_rate}
    lines += [
        "",
        f"heuristic tier (weights frozen, measure only): dev gate recall "
        f"{h_gate_recall:.3f} on {len(failures)} failures, benign flag rate {h_benign_rate:.3f}",
    ]

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print("\n".join(lines))
    print(f"\nwrote {OUT / 'table.md'} and {OUT / 'summary.json'}")


if __name__ == "__main__":
    main()
