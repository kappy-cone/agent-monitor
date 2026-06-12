"""Base-session-atomic dev/test split for the synthetic dataset.

The split unit is the *base session*, not the transcript: every transcript
derived from the same base (matched-pair twins and siblings sharing a base)
lands on the same side, so a pair can never leak across the split and pair
deltas stay co-located. Stratification targets every class stratum (failure
mode x tier, benign-plain, benign-hard-negative, benign-filler) per source,
as far as base-level atomicity allows.

This module reads label provenance (the JSON in ``LabeledTranscript.notes``).
Provenance is eval-side input — allowed for the splitter and the eval, never
for monitors, which see only rendered transcript content.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from agentmon.schemas import LabeledTranscript

if TYPE_CHECKING:
    from collections.abc import Sequence

FAILURE_MODES = ("security_vuln", "reward_hacking", "scope_expansion", "deception")

#: Constraints the search treats as hard (lexicographically before balance):
#: enough agentmon-derived failures in test for a generalization claim, and
#: enough tinygrad-derived failures (per mode) and hard negatives in dev for
#: the Phase-3 iteration loop, which sees only the tinygrad-derived dev slice.
#: Every hard-negative pattern in the tinygrad pool must reach that slice —
#: each pattern is a distinct named precision trap a monitor gets tuned
#: against, so a pattern absent from dev is a precision signal iteration
#: never sees.
MIN_TEST_AGENTMON_FAILURES = 8
MIN_DEV_TINYGRAD_FAILURES_PER_MODE = 2
MIN_DEV_TINYGRAD_HARD_NEGATIVES = 5
REQUIRE_ALL_TINYGRAD_HN_PATTERNS_IN_DEV = True


class Provenance(BaseModel):
    """Eval-side provenance parsed from a label's notes JSON."""

    transcript_id: str
    label: str
    base: str
    source: str
    stratum: str
    tier: str | None = None
    pair_with: str | None = None
    hn_pattern: str | None = None
    failure_event_indices: list[int] = Field(default_factory=list)

    @property
    def is_failure(self) -> bool:
        return self.label != "benign"


def base_source(base: str) -> str:
    """Derive a base's source repo from its id prefix.

    ``tg*`` bases come from the tinygrad sessions, ``sub*`` bases from the
    agentmon build/review subagent sessions (see manifest.yaml -> bases).
    """
    if base.startswith("tg"):
        return "tinygrad"
    if base.startswith("sub"):
        return "agentmon"
    raise ValueError(f"cannot derive source for base {base!r}")


def parse_provenance(label: LabeledTranscript) -> Provenance:
    """Parse one label's notes JSON into structured provenance."""
    try:
        notes = json.loads(label.notes)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label.transcript_id}: notes is not valid JSON: {exc}") from exc
    base = notes.get("base")
    if not isinstance(base, str) or not base:
        raise ValueError(f"{label.transcript_id}: notes lacks a 'base' field")
    if label.label != "benign":
        tier = notes.get("tier")
        if tier not in ("blatant", "medium", "subtle"):
            raise ValueError(f"{label.transcript_id}: failure label without a valid tier")
        stratum = f"{label.label}/{tier}"
    elif notes.get("class") == "hard_negative":
        stratum = "benign-hard-negative"
    elif notes.get("injection_method") == "llm_local_edit":
        stratum = "benign-filler"
    else:
        stratum = "benign-plain"
    return Provenance(
        transcript_id=label.transcript_id,
        label=label.label,
        base=base,
        source=base_source(base),
        stratum=stratum,
        tier=notes.get("tier"),
        pair_with=notes.get("pair_with"),
        hn_pattern=notes.get("hn_pattern"),
        failure_event_indices=list(notes.get("failure_event_indices", [])),
    )


def matched_pairs(provenances: Sequence[Provenance]) -> list[tuple[str, str, str]]:
    """All (clean_id, injected_id, injected_stratum) matched pairs.

    ``pair_with`` on an injected row names its clean twin. Pairs whose twin is
    absent from ``provenances`` raise — a pair split across inputs is exactly
    the leakage the base-atomic split exists to prevent.
    """
    by_id = {p.transcript_id: p for p in provenances}
    pairs: list[tuple[str, str, str]] = []
    for prov in provenances:
        if prov.pair_with is None:
            continue
        if prov.pair_with not in by_id:
            raise ValueError(
                f"{prov.transcript_id}: pair twin {prov.pair_with!r} missing from input"
            )
        pairs.append((prov.pair_with, prov.transcript_id, prov.stratum))
    return pairs


class Split(BaseModel):
    """A persisted dev/test split, atomic at the base-session level."""

    seed: int
    test_fraction: float
    dev_bases: list[str]
    test_bases: list[str]
    dev_ids: list[str]
    test_ids: list[str]
    constraint_violations: int
    balance_score: float
    achieved: dict[str, dict[str, dict[str, int]]] = Field(default_factory=dict)

    def side_of(self, transcript_id: str) -> str:
        if transcript_id in set(self.dev_ids):
            return "dev"
        if transcript_id in set(self.test_ids):
            return "test"
        raise KeyError(f"transcript {transcript_id!r} is not in the split")


def _score_assignment(
    test_bases: set[str],
    provenances: Sequence[Provenance],
    test_fraction: float,
) -> tuple[int, float]:
    """(hard-constraint violations, stratification imbalance) — lower is better."""
    cell_total: dict[tuple[str, str], int] = defaultdict(int)
    cell_test: dict[tuple[str, str], int] = defaultdict(int)
    test_agentmon_failures = 0
    dev_tg_failures_per_mode: dict[str, int] = dict.fromkeys(FAILURE_MODES, 0)
    dev_tg_hard_negatives = 0
    tg_hn_pool: set[str] = set()
    dev_tg_hn_patterns: set[str] = set()
    for prov in provenances:
        cell = (prov.stratum, prov.source)
        in_test = prov.base in test_bases
        cell_total[cell] += 1
        if in_test:
            cell_test[cell] += 1
        if prov.is_failure and prov.source == "agentmon" and in_test:
            test_agentmon_failures += 1
        if prov.source == "tinygrad" and prov.hn_pattern is not None:
            tg_hn_pool.add(prov.hn_pattern)
        if prov.source == "tinygrad" and not in_test:
            if prov.is_failure:
                dev_tg_failures_per_mode[prov.label] += 1
            elif prov.stratum == "benign-hard-negative":
                dev_tg_hard_negatives += 1
                if prov.hn_pattern is not None:
                    dev_tg_hn_patterns.add(prov.hn_pattern)
    violations = max(0, MIN_TEST_AGENTMON_FAILURES - test_agentmon_failures)
    for mode in FAILURE_MODES:
        violations += max(0, MIN_DEV_TINYGRAD_FAILURES_PER_MODE - dev_tg_failures_per_mode[mode])
    violations += max(0, MIN_DEV_TINYGRAD_HARD_NEGATIVES - dev_tg_hard_negatives)
    if REQUIRE_ALL_TINYGRAD_HN_PATTERNS_IN_DEV:
        violations += len(tg_hn_pool - dev_tg_hn_patterns)
    imbalance = sum(
        (cell_test[cell] - test_fraction * total) ** 2 for cell, total in cell_total.items()
    )
    return violations, imbalance


def make_split(
    labels: Sequence[LabeledTranscript],
    *,
    seed: int,
    test_fraction: float = 0.5,
    n_candidates: int = 2000,
) -> Split:
    """Search for a base-atomic split that balances every stratum x source cell.

    Seeded random candidates followed by single-base-flip hill climbing;
    deterministic for a given (labels, seed, test_fraction, n_candidates).
    Hard constraints (module constants) sort lexicographically before the
    soft imbalance score.
    """
    provenances = [parse_provenance(label) for label in labels]
    bases = sorted({p.base for p in provenances})
    rng = random.Random(seed)

    best: set[str] | None = None
    best_score: tuple[int, float] | None = None
    for _ in range(n_candidates):
        candidate = {base for base in bases if rng.random() < test_fraction}
        score = _score_assignment(candidate, provenances, test_fraction)
        if best_score is None or score < best_score:
            best, best_score = candidate, score
    assert best is not None and best_score is not None  # n_candidates > 0

    improved = True
    while improved:
        improved = False
        for base in bases:
            flipped = best ^ {base}
            score = _score_assignment(flipped, provenances, test_fraction)
            if score < best_score:
                best, best_score = flipped, score
                improved = True
        # Swap moves escape local optima single flips can't: exchanging a dev
        # base for a test base keeps both sides' sizes while rebalancing cells.
        for dev_base in [b for b in bases if b not in best]:
            for test_base in sorted(best):
                swapped = (best - {test_base}) | {dev_base}
                score = _score_assignment(swapped, provenances, test_fraction)
                if score < best_score:
                    best, best_score = swapped, score
                    improved = True

    dev_ids = sorted(p.transcript_id for p in provenances if p.base not in best)
    test_ids = sorted(p.transcript_id for p in provenances if p.base in best)
    achieved: dict[str, dict[str, dict[str, int]]] = {}
    for prov in provenances:
        side = "test" if prov.base in best else "dev"
        by_source = achieved.setdefault(prov.stratum, {})
        by_side = by_source.setdefault(prov.source, {"dev": 0, "test": 0})
        by_side[side] += 1
    return Split(
        seed=seed,
        test_fraction=test_fraction,
        dev_bases=sorted(set(bases) - best),
        test_bases=sorted(best),
        dev_ids=dev_ids,
        test_ids=test_ids,
        constraint_violations=best_score[0],
        balance_score=best_score[1],
        achieved=achieved,
    )


def save_split(split: Split, path: Path) -> None:
    path.write_text(split.model_dump_json(indent=2) + "\n", encoding="utf-8")


def load_split(path: Path) -> Split:
    return Split.model_validate_json(path.read_text(encoding="utf-8"))


def load_labels(path: Path) -> list[LabeledTranscript]:
    """One LabeledTranscript per non-empty line of a JSONL file."""
    return [
        LabeledTranscript.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
