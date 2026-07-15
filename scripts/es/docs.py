"""Pure ES document construction for the agentmon lens (design 10).

Pure functions only: (CalibratedVerdict, Provenance, run meta) -> dict. No client,
no network, no I/O. That is what lets ``tests/test_es_docs.py`` cover this with no
Docker, no Elasticsearch, and no API key.

THE ADDITIVITY INVARIANT: this module imports FROM agentmon. agentmon never imports
this. ES is a derived, disposable projection; the committed artifacts stay the source
of truth.

THE JOIN INVARIANT: ground truth (`tag`, `flagged`, `off_mode`) is taken VERBATIM
from ``agentmon.eval.report.build_rows`` — never recomputed here. Forking that logic
would let the lens and the HTML report disagree about what a false positive is, and
the subtle one would win: ``build_rows`` defines `flagged` as ``fraction_flagged > 0``,
i.e. POST-verification, so a flag that verification flipped is NOT an FP.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from agentmon.eval.report import build_rows  # noqa: E402
from agentmon.eval.split import Provenance  # noqa: E402
from agentmon.schemas import CalibratedVerdict  # noqa: E402

DRAWS_INDEX = "agentmon-draws"


class UnmappedSubstrate(KeyError):
    """Raised when a model has no entry in substrates.yaml — the export fails closed.

    A silent ``unknown`` bucket in a Qwen-vs-Gemma comparison is a wrong answer, not
    a missing one. See scripts/es/substrates.yaml for why this cannot be inferred.
    """


def resolve_substrate(model: str, substrates: dict[str, Any]) -> dict[str, str]:
    entry = (substrates.get("models") or {}).get(model)
    if entry is None:
        known = ", ".join(sorted(substrates.get("models") or {}))
        raise UnmappedSubstrate(
            f"model {model!r} has no entry in substrates.yaml (known: {known}). "
            "Add it deliberately — substrate must never be inferred from paths."
        )
    return {
        "family": entry["family"],
        "served_alias": model,
        "model_id": entry["model_id"],
        "quant": entry["quant"],
        "hosting": entry["hosting"],
    }


def doc_id(run_key: str, monitor_id: str, transcript_id: str, sample_index: int) -> str:
    """Stable, legible, greppable _id so re-export updates rather than duplicates."""
    return f"{run_key}::{monitor_id}::{transcript_id}::s{sample_index}"


def _eval_block(row: dict[str, Any], prov: Provenance) -> dict[str, Any]:
    """Ground truth, in a QUARANTINED namespace.

    split.py calls this eval-side input, "never for monitors". Keeping it under
    agentmon.eval.* makes label-conditioning obvious on sight in Kibana: a panel
    filtering on eval.label and presenting the result as monitor telemetry is the
    leakage split.py forbids. A visible namespace is the only control an unlicensed
    localhost cluster gives us.
    """
    return {
        "label": prov.label,
        "is_failure": prov.is_failure,
        "stratum": prov.stratum,
        "tier": prov.tier,
        "base": prov.base,
        "hn_pattern": prov.hn_pattern,
        "pair_with": prov.pair_with,
        "failure_event_indices": list(prov.failure_event_indices),
        "tag": row["tag"],  # caught|missed|fp|tn — from build_rows, NEVER recomputed
        "off_mode": row["off_mode"],
    }


def draw_docs(
    verdict: CalibratedVerdict,
    row: dict[str, Any],
    prov: Provenance,
    *,
    run_meta: dict[str, Any],
    time_fields: dict[str, Any],
    substrates: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """One doc per DRAW (k per verdict). Returns [(_id, source)].

    Verdict-level fields are denormalized onto every draw, so the decision grain is
    recoverable exactly by filtering ``agentmon.draw.sample_index: 0`` — that doc
    carries every calibrated field. Counting FPs WITHOUT that filter returns k x.
    """
    run_key = run_meta["run_key"]
    substrate = resolve_substrate(verdict.model, substrates)
    time_block = time_fields["agentmon"]["time"]

    docs: list[tuple[str, dict[str, Any]]] = []
    for i, (sample, flagged) in enumerate(
        zip(verdict.samples, verdict.sample_flagged, strict=True)
    ):
        verification = verdict.verifications[i] if i < len(verdict.verifications) else None
        evidence = list(sample.evidence)
        source: dict[str, Any] = {
            "@timestamp": time_fields["@timestamp"],
            # -- ECS subset: adopted only where it is TRUE. See docs/designs/10-es-lens.md
            #    for what is deliberately refused and why (host.*/user.*/threat.* do not
            #    exist here; fabricating them to fill a SIEM UI would be reward_hacking).
            "ecs": {"version": "8.11.0"},
            "event": {
                "kind": "alert" if flagged else "event",
                "module": "agentmon",
                "dataset": "agentmon.draws",
                "severity": sample.suspicion_score,
                "risk_score": verdict.calibrated_score,
                # The MONITOR CALL's outcome (did it parse?) — NOT TP/FP, which is a
                # ground-truth evaluation and lives in agentmon.eval.tag.
                "outcome": "failure" if sample.parse_error else "success",
                "duration": int(sample.latency_ms * 1e6),  # ns per ECS; real measured data
            },
            "rule": {
                # A monitor genuinely IS a detection rule.
                "id": verdict.monitor_id,
                "name": verdict.monitor_id,
                "ruleset": "agentmon-monitors",
            },
            "agentmon": {
                "time": time_block,
                "run": {
                    "key": run_key,
                    "committed": run_meta["committed"],
                    "slice": run_meta.get("slice"),
                    "k": verdict.k,
                    "temperature": verdict.temperature,
                    "verify": verdict.verification_enabled,
                },
                "substrate": substrate,
                "transcript": {"id": verdict.transcript_id, "source": prov.source},
                "draw": {
                    "sample_index": i,
                    "suspicion_score": sample.suspicion_score,
                    "categories": [c.value for c in sample.categories],
                    "flagged": bool(flagged),
                    "reasoning": sample.reasoning,
                    "evidence_indices": [e.event_index for e in evidence],
                    "evidence_quotes": [e.quote for e in evidence],
                    "input_tokens": sample.input_tokens,
                    "output_tokens": sample.output_tokens,
                    "latency_ms": sample.latency_ms,
                    "parse_error": sample.parse_error,
                    "verification": {
                        "supported": verification.supported if verification else None,
                        "quote_match": verification.quote_match if verification else None,
                        "reasoning": verification.reasoning if verification else None,
                    },
                },
                # Verdict-level (denormalized). VERBATIM from build_rows.
                "verdict": {
                    "flagged": row["flagged"],
                    "fraction_flagged": row["fraction_flagged"],
                    "calibrated_score": row["calibrated_score"],
                    "mean_suspicion": row["mean_suspicion"],
                    "flips": row["flips"],
                    "mech_flips": row["mech_flips"],
                },
                "eval": _eval_block(row, prov),
            },
        }
        docs.append((doc_id(run_key, verdict.monitor_id, verdict.transcript_id, i), source))
    return docs


def build_run_docs(
    verdicts: list[CalibratedVerdict],
    provenances: dict[str, Provenance],
    *,
    run_meta: dict[str, Any],
    time_fields: dict[str, Any],
    substrates: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """All draw docs for one run. The join comes from build_rows; we only project it."""
    rows = build_rows(verdicts, provenances)
    by_key = {(r["monitor"], r["transcript"]): r for r in rows}
    out: list[tuple[str, dict[str, Any]]] = []
    for verdict in verdicts:
        row = by_key[(verdict.monitor_id, verdict.transcript_id)]
        out.extend(
            draw_docs(
                verdict,
                row,
                provenances[verdict.transcript_id],
                run_meta=run_meta,
                time_fields=time_fields,
                substrates=substrates,
            )
        )
    return out


def bulk_ndjson(docs: list[tuple[str, dict[str, Any]]], index: str = DRAWS_INDEX) -> str:
    """Serialize to ES _bulk NDJSON. ``index`` action = idempotent upsert by _id."""
    import json

    lines: list[str] = []
    for _id, source in docs:
        lines.append(json.dumps({"index": {"_index": index, "_id": _id}}, sort_keys=True))
        lines.append(json.dumps(source, sort_keys=True))
    return "\n".join(lines) + "\n"
