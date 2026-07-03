"""THE USER-GATED SEALED-TEST READ for one composite monitor (design 03 Phase 4).

Running this script with ``--go`` scores the sealed test split for ONE
composite — a declared, single-purpose gate event (DECISIONS 44's "separate
gated read"). Do NOT run it without an explicit user GO decision recorded in
DECISIONS.md. ``--help`` and the preflight-only mode (no ``--go``) are always
safe: they read nothing from the test rows and make no calls.

The read is a REPLAY ROW — zero live completions by construction:

- ``_NoCallClient`` cannot make a call (it raises), so a cache miss aborts
  with zero spend rather than buying fresh test completions;
- preflight 1: every leaf prompt must hash to its GATE-2 frozen SHA and the
  composite's structural manifest (member ids, pins, prompt SHAs, pipeline
  stage ids) is recorded — a silent member or pipeline swap changes the hash;
- preflight 2: the injected pipeline's stage ids must match ``--pipeline``
  (the duck-typed ``self_verifies`` divergence guard);
- preflight 3: ``verify=True`` is REFUSED on a non-self-verifying composite —
  driver-only verification is mis-keyed for cross-substrate pins and drops
  quiet OR-catches (evaluated and rejected, design 03);
- preflight 4: two-pass ``replay_coverage`` (draw keys AND reconstructed
  verification keys) must be full-coverage or the row HALTs (exit 2).

Post-row, ``composite_band_check`` gates the row under bands derived
blind-to-test from the DEV rates you pass (``--dev-rate-pct`` from
``results/dev-composite-probe-verified/summary.json``), the request count
must be 0, and the ledger gains a ``requests: 0`` row. Outputs land under
``out/phase4/runs/<served>/test-comp-<id>/`` — committed ``results/``
artifacts are never modified. Exit 2 on any HALT.

Run (after the user GO):
  env -u UV_PYTHON uv run python scripts/test_run_composite.py \\
      --composite vens4 --substrate qwen --dev-rate-pct <from the dev probe> --go
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentmon.calibration import run_calibrated  # noqa: E402
from agentmon.eval.accounting import LedgerEntry, append_ledger, quota_day  # noqa: E402
from agentmon.eval.bands import derive_band  # noqa: E402
from agentmon.eval.gate import (  # noqa: E402
    check_frozen,
    composite_band_check,
    composite_manifest,
    leaf_pins,
    own_modes,
    replay_coverage,
)
from agentmon.eval.split import load_labels, load_split, parse_provenance  # noqa: E402
from agentmon.eval.workspace import (  # noqa: E402
    CACHE_DIR,
    FREEZE_MANIFEST,
    LABELS_PATH,
    OUT_PHASE4,
    PHASE4_LEDGER,
    PROMPTS_DIR,
    SPLIT_PATH,
    TRANSCRIPTS_DIR,
)
from agentmon.monitors.composite import (  # noqa: E402
    CascadeMonitor,
    Member,
    VerifiedEnsembleMonitor,
)
from agentmon.monitors.registry import load_monitors  # noqa: E402
from agentmon.schemas import Transcript  # noqa: E402
from agentmon.verification import PIPELINES, VerificationPipeline  # noqa: E402

SPECIALISTS = ["security_vuln", "reward_hacking", "scope_expansion", "deception"]
LEAVES = [*SPECIALISTS, "generalist"]
#: served names as recorded in the cache namespacing (DECISIONS 37, 41).
SUBSTRATES = {"qwen": "agentnom-local", "gemma": "agentmon-local-gemma"}
K = 3


class _NoCallClient:
    """By construction, not by flag: a replay row cannot buy a completion."""

    def complete(self, *args: object, **kwargs: object) -> object:
        raise AssertionError(
            "cache MISS on a sealed-test replay row: a live call was attempted; "
            "aborting with zero spend"
        )


def build_composite(name: str, pipeline: VerificationPipeline) -> object:
    """The pre-registered gated-read candidates (anything else needs a new
    DECISIONS pre-registration before it may be read)."""
    monitors = load_monitors()
    if name == "vens4":
        return VerifiedEnsembleMonitor(
            "vens[4-specialists]",
            [Member(monitors[m]) for m in SPECIALISTS],
            pipeline=pipeline,
        )
    if name == "vens5":
        return VerifiedEnsembleMonitor(
            "vens[4-spec+generalist]",
            [Member(monitors[m]) for m in LEAVES],
            pipeline=pipeline,
        )
    if name == "vcasc-gen-vens4":
        deep = VerifiedEnsembleMonitor(
            "vens[4-specialists]",
            [Member(monitors[m]) for m in SPECIALISTS],
            pipeline=pipeline,
        )
        return CascadeMonitor("vcasc[gen->vens4]", Member(monitors["generalist"]), Member(deep))
    raise ValueError(f"unknown composite {name!r}")


def manifest_pipelines(node: dict) -> list[list[str]]:
    """Every self-verifying node's pipeline stage ids, recursively."""
    found: list[list[str]] = []
    if "pipeline" in node:
        found.append(node["pipeline"])
    for member in node["members"]:
        if isinstance(member, dict):
            found.extend(manifest_pipelines(member))
    return found


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--composite", required=True, choices=["vens4", "vens5", "vcasc-gen-vens4"])
    parser.add_argument("--substrate", required=True, choices=sorted(SUBSTRATES))
    parser.add_argument("--pipeline", default="v1", choices=sorted(PIPELINES))
    parser.add_argument(
        "--dev-rate-pct",
        type=float,
        required=True,
        help="the composite's DEV draw-level flag rate in percent, from "
        "results/dev-composite-probe-verified/summary.json — the band derives "
        "from it blind-to-test via the pinned D39/42 rule",
    )
    parser.add_argument(
        "--dev-escalation-rate-pct",
        type=float,
        default=None,
        help="cascades only: the DEV escalation rate in percent (same source)",
    )
    parser.add_argument(
        "--go",
        action="store_true",
        help="REQUIRED to score the sealed test split — an explicit user GO "
        "decision recorded in DECISIONS.md; without it only the preflight runs",
    )
    args = parser.parse_args()

    served = SUBSTRATES[args.substrate]
    pipeline = PIPELINES[args.pipeline]()
    composite = build_composite(args.composite, pipeline)

    # Preflight 3 first (cheapest): refuse driver-only verification outright.
    if not getattr(composite, "self_verifies", False):
        sys.exit(
            "REFUSED: this composite does not self-verify; verify=True would "
            "re-check only the driver under a possibly mis-keyed model (design 03)"
        )
    if isinstance(composite, CascadeMonitor) and args.dev_escalation_rate_pct is None:
        sys.exit("REFUSED: a cascade row needs --dev-escalation-rate-pct for its second band")

    # Preflight 1: GATE-2 leaf freeze + the composite's structural manifest.
    leaf_ids = sorted({monitor.config.id for monitor, _ in leaf_pins(composite, served)})
    drift = check_frozen(FREEZE_MANIFEST, PROMPTS_DIR, leaf_ids)
    if drift:
        print("*** HALT — GATE-2 freeze drift:")
        for reason in drift:
            print(f"  - {reason}")
        raise SystemExit(2)
    frozen = yaml.safe_load(FREEZE_MANIFEST.read_text(encoding="utf-8"))
    manifest = composite_manifest(composite, frozen)

    # Preflight 2: injected-pipeline identity (the self_verifies divergence guard).
    want_stages = [stage.stage_id for stage in pipeline.stages]
    for got in manifest_pipelines(manifest):
        if got != want_stages:
            sys.exit(
                f"REFUSED: manifest pipeline {got} != --pipeline {args.pipeline} "
                f"({want_stages}); mixed pipeline versions produce rows that look "
                "comparable and aren't"
            )

    split = load_split(SPLIT_PATH)
    provs = {p.transcript_id: p for p in map(parse_provenance, load_labels(LABELS_PATH))}
    test_ids = sorted(split.test_ids)
    transcripts = [
        Transcript.model_validate_json((TRANSCRIPTS_DIR / f"{t}.json").read_text(encoding="utf-8"))
        for t in test_ids
    ]

    # Preflight 4: two-pass replay coverage — full coverage or HALT.
    hits, expected = replay_coverage(composite, transcripts, K, served, CACHE_DIR)
    print(
        f"preflight: manifest sha256={manifest['sha256'][:16]}… coverage {hits}/{expected} "
        f"(draw + verification keys), pipeline={args.pipeline} {want_stages}"
    )
    if hits != expected:
        print(f"*** HALT — replay coverage {hits}/{expected}: the row is not fully cached")
        raise SystemExit(2)
    if not args.go:
        print(
            "\nPREFLIGHT ONLY — the sealed-test read needs --go (an explicit user "
            "GO decision recorded in DECISIONS.md)."
        )
        return

    # The read: pure cache replay; _NoCallClient makes live spend impossible.
    verdicts = run_calibrated(
        [composite],  # type: ignore[list-item]
        transcripts,
        _NoCallClient(),  # type: ignore[arg-type]
        k=K,
        cache_dir=CACHE_DIR,
        verify=True,
        model_override=served,
    )

    comp_id = composite.config.id  # type: ignore[attr-defined]
    band = derive_band(args.dev_rate_pct)
    escalation_band = None
    if args.dev_escalation_rate_pct is not None:
        esc = derive_band(args.dev_escalation_rate_pct)
        escalation_band = (esc.lo, esc.hi)
    result = composite_band_check(
        comp_id,
        verdicts,
        provs,
        flag_band=(band.lo, band.hi),
        escalation_band=escalation_band,
        modes=own_modes(composite),  # type: ignore[arg-type]
        request_count=0,  # _NoCallClient by construction; any call already raised
        coverage=(hits, expected),
    )

    run_dir = OUT_PHASE4 / "runs" / served / f"test-comp-{comp_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "verdicts.jsonl").open("w", encoding="utf-8") as handle:
        for verdict in verdicts:
            handle.write(verdict.model_dump_json() + "\n")
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "composite": comp_id,
                "substrate": served,
                "pipeline": args.pipeline,
                "pipeline_stage_ids": want_stages,
                "manifest": manifest,
                "gate_ok": result.ok,
                "halt_reasons": result.reasons,
                "report": result.report,
            },
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    append_ledger(
        PHASE4_LEDGER,
        LedgerEntry(date=quota_day(), label=f"test-comp-{comp_id}", model=served, requests=0),
    )
    print(f"wrote {run_dir} (ledger row: requests=0)")
    if not result.ok:
        print("\n*** BANDS HALT — do not read another composite:")
        for reason in result.reasons:
            print(f"  - {reason}")
        raise SystemExit(2)
    print("row PASSED the composite QC gate")


if __name__ == "__main__":
    main()
