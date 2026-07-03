"""Accounting: provenance- and stage-aware call counts, the cost convention,
the spend ledger, and the UTC-7 quota day."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from agentmon.eval.accounting import (
    LedgerEntry,
    append_ledger,
    day_requests,
    member_supported_entries,
    n_calls,
    quota_day,
    run_cost_usd,
)
from agentmon.schemas import (
    CalibratedVerdict,
    CompositeProvenance,
    FailureCategory,
    MonitorVerdict,
    StageOutcome,
    VerificationOutcome,
)


def _sample(
    tid: str,
    *,
    flagged: bool = False,
    provenance: CompositeProvenance | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    model: str = "agentnom-local",
) -> MonitorVerdict:
    return MonitorVerdict(
        monitor_id="deception",
        transcript_id=tid,
        suspicion_score=80 if flagged else 5,
        categories=[FailureCategory.DECEPTION] if flagged else [],
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provenance=provenance,
    )


def make_verdict(
    samples: list[MonitorVerdict],
    *,
    verifications: list[VerificationOutcome | None] | None = None,
    stage_outcomes: list[list[StageOutcome] | None] | None = None,
    parse_retries: int = 0,
    parse_repairs: int = 0,
    model: str = "agentnom-local",
) -> CalibratedVerdict:
    k = len(samples)
    flags = [bool(s.categories) for s in samples]
    mean_suspicion = sum(s.suspicion_score for s in samples) / k
    return CalibratedVerdict(
        monitor_id="deception",
        transcript_id=samples[0].transcript_id,
        model=model,
        k=k,
        temperature=0.7,
        samples=samples,
        verifications=verifications if verifications is not None else [None] * k,
        stage_outcomes=stage_outcomes,
        sample_flagged=flags,
        fraction_flagged=sum(flags) / k,
        mean_suspicion=mean_suspicion,
        calibrated_score=sum(flags) / k + mean_suspicion / 100 * 1e-3,
        verification_enabled=True,
        parse_retries=parse_retries,
        parse_repairs=parse_repairs,
    )


class TestNCalls:
    def test_leaf_counts_k_retries_repairs_and_folded_verifications(self) -> None:
        verdict = make_verdict(
            [_sample("t1", flagged=True), _sample("t1"), _sample("t1")],
            verifications=[VerificationOutcome(supported=True, model="agentnom-local"), None, None],
            parse_retries=1,
            parse_repairs=1,
        )
        assert n_calls([verdict]) == 3 + 1 + 1 + 1

    def test_recorded_stage_outcomes_count_per_model_stage_not_the_fold(self) -> None:
        # A 3-stage pipeline where 2 stages made model calls: 2, not 1 — the
        # folded outcome must NOT be double counted alongside the stages.
        stages = [
            StageOutcome(stage_id="quote", supported=None, model=""),
            StageOutcome(stage_id="claim_grounding", supported=True, model="agentnom-local"),
            StageOutcome(stage_id="semantic", supported=True, model="agentnom-local"),
        ]
        verdict = make_verdict(
            [_sample("t1", flagged=True)],
            verifications=[VerificationOutcome(supported=True, model="agentnom-local")],
            stage_outcomes=[stages],
        )
        assert n_calls([verdict]) == 1 + 2

    def test_ensemble_counts_member_fanout_and_member_verifications(self) -> None:
        # 4 members x k=3 = 12 member draws + 2 member verifications = 14, not 3:
        # calls_accounted tracks the fan-out a live ensemble row actually pays.
        scores = {"a": 10, "b": 20, "c": 30, "d": 40}
        verified = CompositeProvenance(
            composite="ens",
            driver="d",
            member_scores=scores,
            member_supported={"a": False, "b": True},
        )
        quiet = CompositeProvenance(composite="ens", driver="d", member_scores=scores)
        verdict = make_verdict(
            [
                _sample("t1", flagged=True, provenance=verified),
                _sample("t1", provenance=quiet),
                _sample("t1", provenance=quiet),
            ]
        )
        assert n_calls([verdict]) == 12 + 2

    def test_cascade_counts_one_plus_escalated(self) -> None:
        # Three samples, one escalated: 1 + 1 + 2 = 4 draws.
        short = CompositeProvenance(
            composite="casc", driver="triage", member_scores={"triage": 5}, escalated=False
        )
        deep = CompositeProvenance(
            composite="casc",
            driver="deep",
            member_scores={"triage": 60, "deep": 80},
            escalated=True,
        )
        verdict = make_verdict(
            [
                _sample("t1", provenance=short),
                _sample("t1", provenance=short),
                _sample("t1", flagged=True, provenance=deep),
            ]
        )
        assert n_calls([verdict]) == 4

    def test_member_supported_entries_walks_restamped_inner_provenance(self) -> None:
        # A cascade re-stamps the returned draw; the verified ensemble's votes
        # survive under ``inner`` and must still be visible to the guard.
        inner = CompositeProvenance(
            composite="vens",
            driver="deception",
            member_scores={"deception": 80},
            member_supported={"deception": False},
        )
        outer = CompositeProvenance(
            composite="casc",
            driver="vens",
            member_scores={"triage": 60, "vens": 80},
            escalated=True,
            inner=inner,
        )
        sample = _sample("t1", provenance=outer)
        assert member_supported_entries(sample) == {"deception": False}
        assert member_supported_entries(_sample("t1")) == {}


class TestRunCost:
    def test_none_when_any_model_is_unpriced(self) -> None:
        priced = make_verdict(
            [_sample("t1", model="gemini-3.1-flash-lite")], model="gemini-3.1-flash-lite"
        )
        unpriced = make_verdict([_sample("t2")])
        assert run_cost_usd([unpriced]) is None
        assert run_cost_usd([priced, unpriced]) is None  # whole-run, never partial

    def test_priced_run_sums_list_price_equivalent(self) -> None:
        verdict = make_verdict(
            [_sample("t1", model="gemini-3.1-flash-lite", input_tokens=1000, output_tokens=100)],
            model="gemini-3.1-flash-lite",
        )
        # gemini-3.1-flash-lite: (0.25, 1.5) USD per million tokens.
        expected = (1000 * 0.25 + 100 * 1.5) / 1e6
        assert run_cost_usd([verdict, verdict]) == pytest.approx(2 * expected)


class TestQuotaDayAndLedger:
    def test_quota_day_is_keyed_to_utc_minus_7(self) -> None:
        before = datetime.datetime(2026, 7, 3, 6, 59, tzinfo=datetime.UTC)
        after = datetime.datetime(2026, 7, 3, 7, 0, tzinfo=datetime.UTC)
        assert quota_day(before) == "2026-07-02"  # 23:59 the previous quota day
        assert quota_day(after) == "2026-07-03"  # midnight UTC-7: the day flips

    def test_ledger_roundtrip_and_day_totals(self, tmp_path: Path) -> None:
        ledger = tmp_path / "requests.jsonl"
        assert day_requests(ledger, "2026-07-03") == 0  # missing file: zero, no crash
        append_ledger(ledger, LedgerEntry(date="2026-07-03", label="a", model="m", requests=210))
        append_ledger(ledger, LedgerEntry(date="2026-07-03", label="b", model="m", requests=30))
        append_ledger(ledger, LedgerEntry(date="2026-07-04", label="c", model="m", requests=99))
        assert day_requests(ledger, "2026-07-03") == 240
        assert day_requests(ledger, "2026-07-04") == 99
        # The serialized line keeps the shape both committed ledgers share.
        first = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        assert list(first) == ["date", "label", "model", "requests"]
