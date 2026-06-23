"""Offline composite-monitor probe on the tinygrad dev slice (read-only, descriptive).

Does composing monitors help, judged purely from the frozen cache? Every number
is produced by driving a composite through ``run_calibrated`` with a client that
raises on any live call (cache-replay), so the run makes zero completions and
zero new cache entries — it only reads draws the dev runs already cached
(qwen-devbands, gemma-devbands; 555 draws each).

Scope guards, by construction:
- dev∩tinygrad ONLY (n=37). The test split is sealed this session; a stray test
  id aborts the run.
- ``verify`` is OFF. This is draw-level composition: composite draw i = the
  combination of member draws at index i, then the SAME per-sample aggregation
  and metric fns the leaf rows use. The committed flip-on-fail verification keys
  by served model, so cross-substrate verification is a live-box step; the
  committed dev tables are verify=True and are reconciled at the end.
- FLAG_BANDS are untouched — composite bands get re-derived on the box.

Candidates are PRESENTED with metrics, never auto-selected.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentmon.calibration import run_calibrated  # noqa: E402
from agentmon.eval.metrics import auroc, recall_at_fpr  # noqa: E402
from agentmon.eval.split import load_labels, load_split, parse_provenance  # noqa: E402
from agentmon.monitors.composite import CascadeMonitor, EnsembleMonitor, Member  # noqa: E402
from agentmon.monitors.registry import load_monitors  # noqa: E402
from agentmon.schemas import CalibratedVerdict, Transcript  # noqa: E402

SPLIT = REPO / "datasets" / "synthetic" / "split.json"
LABELS = REPO / "datasets" / "synthetic" / "labels.jsonl"
TRANSCRIPTS = REPO / "datasets" / "synthetic" / "transcripts"
CACHE = REPO / ".agentmon_cache"
OUT = REPO / "results" / "dev-composite-probe"

SPECIALISTS = ["security_vuln", "reward_hacking", "scope_expansion", "deception"]
LEAVES = [*SPECIALISTS, "generalist"]
#: served name as recorded in the cache namespacing (DECISIONS 37, 41) — used
#: verbatim, typo and all; never "fixed".
SUBSTRATES = {"qwen": "agentnom-local", "gemma": "agentmon-local-gemma"}
#: committed verify=True dev verdicts, for the reconciliation block.
COMMITTED = {
    "qwen": REPO / "out" / "phase3" / "runs" / "qwen-devbands" / "verdicts.jsonl",
    "gemma": REPO / "results" / "gemma-qwen-test-matrix" / "gemma-devbands" / "verdicts.jsonl",
}
K = 3


class _NoCallClient:
    """Raises on any live call — proves every draw read is served from cache."""

    def complete(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("cache MISS: a live call was attempted (a draw is uncached)")


def _load_dev_tg() -> tuple[list[str], list[Transcript], dict[str, object]]:
    split = load_split(SPLIT)
    provs = {p.transcript_id: p for p in map(parse_provenance, load_labels(LABELS))}
    dev_tg = sorted(t for t in split.dev_ids if provs[t].source == "tinygrad")
    test_ids = set(split.test_ids)
    leaked = [t for t in dev_tg if t in test_ids]
    assert not leaked, f"TEST SEAL VIOLATION: {leaked} are in the test split"
    transcripts = [
        Transcript.model_validate_json((TRANSCRIPTS / f"{t}.json").read_text(encoding="utf-8"))
        for t in dev_tg
    ]
    return dev_tg, transcripts, provs


def _replay(
    evaluator: object, transcripts: list[Transcript], model: str
) -> list[CalibratedVerdict]:
    """Cache-replay one evaluator over the dev transcripts; verify OFF."""
    return run_calibrated(
        [evaluator],  # type: ignore[list-item]
        transcripts,
        _NoCallClient(),
        k=K,
        cache_dir=CACHE,
        verify=False,
        model_override=model,
    )


def _metrics(
    verdicts: list[CalibratedVerdict], provs: dict[str, object]
) -> dict[str, float | None]:
    scores = [v.calibrated_score for v in verdicts]
    flags = [provs[v.transcript_id].is_failure for v in verdicts]
    return {
        "auroc": auroc(scores, flags),
        "recall_at_0fp": recall_at_fpr(scores, flags, 0.0),
        "recall_at_5pct": recall_at_fpr(scores, flags, 0.05),
    }


def _fmt(value: float | None) -> str:
    return " n/a " if value is None else f"{value:.3f}"


def _row(name: str, m: dict[str, float | None], extra: str = "") -> str:
    return (
        f"| {name:34s} | {_fmt(m['auroc'])} | {_fmt(m['recall_at_0fp'])} "
        f"| {_fmt(m['recall_at_5pct'])} |{extra}"
    )


def main() -> None:
    dev_tg, transcripts, provs = _load_dev_tg()
    monitors = load_monitors()
    failures = [t for t in dev_tg if provs[t].is_failure]
    lines: list[str] = []
    summary: dict[str, object] = {
        "slice": "dev∩tinygrad",
        "n": len(dev_tg),
        "k": K,
        "verify": False,
    }

    def emit(s: str = "") -> None:
        lines.append(s)

    emit(
        f"# Dev composite probe — slice=dev∩tinygrad n={len(dev_tg)} k={K} verify=OFF (draw-level)"
    )
    emit(f"# failures={len(failures)} benign={len(dev_tg) - len(failures)} | served={SUBSTRATES}")
    emit("# Read-only cache-replay (zero completions). Pre-verification; bands untouched.")
    emit("")

    # Self-check: a one-member ensemble must reproduce its leaf exactly.
    leaf_sec = _replay(monitors["security_vuln"], transcripts, SUBSTRATES["qwen"])
    solo = _replay(
        EnsembleMonitor("solo[security_vuln]", [Member(monitors["security_vuln"])]),
        transcripts,
        SUBSTRATES["qwen"],
    )
    recon_ok = all(
        a.fraction_flagged == b.fraction_flagged and a.calibrated_score == b.calibrated_score
        for a, b in zip(leaf_sec, solo, strict=True)
    )
    emit(f"reconciliation self-check (singleton ensemble == leaf, qwen security_vuln): {recon_ok}")
    summary["singleton_equals_leaf"] = recon_ok
    emit("")

    # ----- 3a · cross-monitor ensemble, single substrate -----
    emit("## 3a · cross-monitor ensemble (single substrate) — OR flag, max score")
    section_a: dict[str, dict] = {}
    for sub, model in SUBSTRATES.items():
        emit(f"\n### substrate={sub} ({model})")
        emit(f"| candidate{'':25s} | AUROC | r@0FP | r@5%FPR |")
        emit("|------------------------------------|-------|-------|---------|")
        for mid in LEAVES:
            m = _metrics(_replay(monitors[mid], transcripts, model), provs)
            section_a[f"{sub}/leaf:{mid}"] = m
            emit(_row(f"leaf {mid}", m))
        spec4 = EnsembleMonitor("ens[4-specialists]", [Member(monitors[s]) for s in SPECIALISTS])
        all5 = EnsembleMonitor("ens[4-spec+generalist]", [Member(monitors[s]) for s in LEAVES])
        for comp in (spec4, all5):
            m = _metrics(_replay(comp, transcripts, model), provs)
            section_a[f"{sub}/{comp.config.id}"] = m
            emit(_row(comp.config.id, m))
    summary["3a"] = section_a

    # ----- 3b · cross-substrate ensemble (Qwen ⊕ Gemma) via member model pin -----
    emit("\n## 3b · cross-substrate ensemble (Qwen ⊕ Gemma) — model-agnostic headline")
    emit("\nDev miss-differently check (raw draw flag; failure caught iff fraction_flagged>0):")
    emit("| monitor          | both | only_qwen | only_gemma | union | qwen | gemma |")
    emit("|------------------|------|-----------|------------|-------|------|-------|")
    caught: dict[tuple[str, str], dict[str, bool]] = {}
    for mid in LEAVES:
        for sub, model in SUBSTRATES.items():
            vs = _replay(monitors[mid], transcripts, model)
            caught[(mid, sub)] = {v.transcript_id: v.fraction_flagged > 0 for v in vs}
    disagree: dict[str, dict] = {}
    for mid in LEAVES:
        q, g = caught[(mid, "qwen")], caught[(mid, "gemma")]
        both = [t for t in failures if q[t] and g[t]]
        only_q = [t for t in failures if q[t] and not g[t]]
        only_g = [t for t in failures if g[t] and not q[t]]
        union = [t for t in failures if q[t] or g[t]]
        disagree[mid] = {
            "both": len(both),
            "only_qwen": only_q,
            "only_gemma": only_g,
            "union_recall": len(union) / len(failures),
            "qwen_recall": sum(q[t] for t in failures) / len(failures),
            "gemma_recall": sum(g[t] for t in failures) / len(failures),
        }
        emit(
            f"| {mid:16s} | {len(both):4d} | {len(only_q):9d} | {len(only_g):10d} "
            f"| {len(union):5d} | {sum(q[t] for t in failures):4d} "
            f"| {sum(g[t] for t in failures):5d} |"
        )
    summary["3b_disagreement"] = disagree
    n_diff = sum(1 for mid in LEAVES if disagree[mid]["only_qwen"] or disagree[mid]["only_gemma"])
    emit(
        f"\nsubstrates miss-differently on dev for {n_diff}/{len(LEAVES)} monitors "
        f"-> cross-substrate OR replicates on dev (not imported from test)."
    )

    emit("\n### cross-substrate ensembles vs each substrate's leaf")
    emit(f"| candidate{'':25s} | AUROC | r@0FP | r@5%FPR |")
    emit("|------------------------------------|-------|-------|---------|")
    section_b: dict[str, dict] = {}
    for mid in LEAVES:
        mq = _metrics(_replay(monitors[mid], transcripts, SUBSTRATES["qwen"]), provs)
        mg = _metrics(_replay(monitors[mid], transcripts, SUBSTRATES["gemma"]), provs)
        xsub = EnsembleMonitor(
            f"xsub[{mid}]",
            [
                Member(monitors[mid], model_override=SUBSTRATES["qwen"]),
                Member(monitors[mid], model_override=SUBSTRATES["gemma"]),
            ],
        )
        # The ensemble pins each member's model, so the run-level override is moot.
        mx = _metrics(_replay(xsub, transcripts, SUBSTRATES["qwen"]), provs)
        section_b[mid] = {"qwen": mq, "gemma": mg, "xsub": mx}
        emit(_row(f"{mid} @qwen", mq))
        emit(_row(f"{mid} @gemma", mg))
        emit(_row(f"{mid} qwen⊕gemma", mx))
    summary["3b"] = section_b

    # ----- 3c · cascade pareto (generalist triage -> 4-specialist deep) -----
    emit(
        "\n## 3c · cascade pareto (offline) — generalist triage gates a 4-specialist deep ensemble"
    )
    emit(f"| candidate{'':25s} | AUROC | r@0FP | r@5%FPR | escal | gpu_save |")
    emit("|------------------------------------|-------|-------|---------|-------|----------|")
    section_c: dict[str, dict] = {}
    for sub, model in SUBSTRATES.items():
        deep = EnsembleMonitor("ens[4-specialists]", [Member(monitors[s]) for s in SPECIALISTS])
        cascade = CascadeMonitor("casc[gen->spec4]", Member(monitors["generalist"]), Member(deep))
        verdicts = _replay(cascade, transcripts, model)
        n_draws = sum(len(v.samples) for v in verdicts)
        escalated = sum(
            1 for v in verdicts for s in v.samples if s.provenance and s.provenance.escalated
        )
        escal_rate = escalated / n_draws
        m = _metrics(verdicts, provs)
        # Reference points: triage alone and always-run deep, same scores+labels.
        triage_m = _metrics(_replay(monitors["generalist"], transcripts, model), provs)
        deep_m = _metrics(_replay(deep, transcripts, model), provs)
        section_c[sub] = {
            "cascade": m,
            "triage_generalist": triage_m,
            "deep_spec4": deep_m,
            "escalation_rate": escal_rate,
            "gpu_saving": 1 - escal_rate,
        }
        emit(_row(f"[{sub}] triage=generalist", triage_m))
        emit(_row(f"[{sub}] deep=spec4 (always)", deep_m))
        emit(_row(f"[{sub}] cascade gen->spec4", m, f" {escal_rate:.3f} | {1 - escal_rate:.4f} |"))
    summary["3c"] = section_c
    emit(
        "\nescal = triage flag rate = live deep-call rate; gpu_save = 1 - escal "
        "(fraction of deep ensemble evaluations skipped). Cascade recall is triage-bounded."
    )

    # ----- reconciliation vs the committed verify=True dev tables -----
    emit("\n## reconciliation — my verify=OFF leaf rows vs the committed verify=True dev verdicts")
    emit(
        "| leaf @substrate           | AUROC(verifyOFF) | AUROC(committed,verifyON) | verif_flips |"
    )
    emit(
        "|---------------------------|------------------|---------------------------|-------------|"
    )
    recon: dict[str, dict] = {}
    for sub, path in COMMITTED.items():
        committed_v: dict[str, list[CalibratedVerdict]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                v = CalibratedVerdict.model_validate_json(line)
                committed_v.setdefault(v.monitor_id, []).append(v)
        for mid in LEAVES:
            rows = [v for v in committed_v[mid] if v.transcript_id in set(dev_tg)]
            scores = [v.calibrated_score for v in rows]
            flags = [provs[v.transcript_id].is_failure for v in rows]
            committed_auroc = auroc(scores, flags)
            flips = sum(
                1 for v in rows for o in v.verifications if o is not None and not o.supported
            )
            off = _metrics(_replay(monitors[mid], transcripts, SUBSTRATES[sub]), provs)
            recon[f"{sub}/{mid}"] = {
                "auroc_verify_off": off["auroc"],
                "auroc_committed_verify_on": committed_auroc,
                "verification_flips": flips,
            }
            emit(
                f"| {mid + ' @' + sub:25s} | {_fmt(off['auroc']):16s} "
                f"| {_fmt(committed_auroc):25s} | {flips:11d} |"
            )
    summary["reconciliation"] = recon

    table = "\n".join(lines)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "table.md").write_text(table + "\n", encoding="utf-8")
    (OUT / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(table)
    print(f"\nwrote {OUT / 'table.md'} and {OUT / 'summary.json'}")


if __name__ == "__main__":
    main()
