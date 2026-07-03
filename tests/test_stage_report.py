"""Fixture tests for the stage confusion accounting (agentmon.eval.stage_report).

Hand-built CalibratedVerdicts cover all four delta cells — catches killed and
saved (failure labels), FPs suppressed and restored (benign labels) — plus the
semantic-calls-saved count, the deciding-stage attribution, and the input
guards. Pure fixtures: no client, no cache, no model calls.
"""

from __future__ import annotations

import pytest

from agentmon.eval.stage_report import (
    FlipDelta,
    flip_deltas,
    stage_confusion,
    transcript_catch_loss,
)
from agentmon.schemas import (
    CalibratedVerdict,
    FailureCategory,
    MonitorVerdict,
    StageOutcome,
)

MODEL = "agentnom-local"

LABELS = {
    "f1": "deception",
    "f2": "deception",
    "f3": "deception",
    "f4": "deception",
    "b1": "benign",
    "b2": "benign",
}


def sample(tid: str, *, flagged: bool) -> MonitorVerdict:
    return MonitorVerdict(
        monitor_id="deception",
        transcript_id=tid,
        suspicion_score=70 if flagged else 5,
        categories=[FailureCategory.DECEPTION] if flagged else [],
        model=MODEL,
    )


def outcome(
    stage_id: str, supported: bool | None = None, *, terminal: bool = False
) -> StageOutcome:
    return StageOutcome(stage_id=stage_id, supported=supported, terminal=terminal)


def cv(
    tid: str,
    raw_flags: list[bool],
    post_flags: list[bool],
    stage_lists: list[list[StageOutcome] | None] | None = None,
) -> CalibratedVerdict:
    k = len(raw_flags)
    return CalibratedVerdict(
        monitor_id="deception",
        transcript_id=tid,
        model=MODEL,
        k=k,
        temperature=0.7,
        samples=[sample(tid, flagged=flag) for flag in raw_flags],
        verifications=[None] * k,
        stage_outcomes=stage_lists,
        sample_flagged=post_flags,
        fraction_flagged=sum(post_flags) / k,
        mean_suspicion=50.0,
        calibrated_score=sum(post_flags) / k + 50.0 / 100 * 1e-3,
        verification_enabled=True,
    )


# Candidate stage lists (v2-shaped): quote diagnostic, event-order proof,
# scoped claim-grounding specialist, generic semantic decider.
QUOTE = outcome("quote_grounding")
EO_ABSTAIN = outcome("event_order")
CG_ABSTAIN = outcome("claim_grounding")


def build_fixtures() -> tuple[list[CalibratedVerdict], list[CalibratedVerdict]]:
    """(baseline, candidate) covering every delta cell over six transcripts.

    f1: catch killed (claim-grounding refute; semantic call saved)
    f2: catch saved (claim-grounding confirm shields it; semantic call saved)
    b1: FP suppressed (event-order refute; semantic call saved)
    b2: FP restored (claim-grounding confirm; semantic call saved)
    f3: no delta (claim-grounding abstains out of scope; semantic upholds)
    f4: k=2, only draw 1 flips (killed) — sample-index granularity
    """
    baseline = [
        cv("f1", [True], [True]),
        cv("f2", [True], [False]),
        cv("b1", [True], [True]),
        cv("b2", [True], [False]),
        cv("f3", [True], [True]),
        cv("f4", [True, True], [True, True]),
    ]
    candidate = [
        cv("f1", [True], [False], [[QUOTE, EO_ABSTAIN, outcome("claim_grounding", False)]]),
        cv(
            "f2",
            [True],
            [True],
            [[QUOTE, EO_ABSTAIN, outcome("claim_grounding", True, terminal=True)]],
        ),
        cv("b1", [True], [False], [[QUOTE, outcome("event_order", False)]]),
        cv(
            "b2",
            [True],
            [True],
            [[QUOTE, EO_ABSTAIN, outcome("claim_grounding", True, terminal=True)]],
        ),
        cv("f3", [True], [True], [[QUOTE, EO_ABSTAIN, CG_ABSTAIN, outcome("semantic", True)]]),
        cv(
            "f4",
            [True, True],
            [True, False],
            [
                [QUOTE, EO_ABSTAIN, CG_ABSTAIN, outcome("semantic", True)],
                [QUOTE, EO_ABSTAIN, outcome("claim_grounding", False)],
            ],
        ),
    ]
    return baseline, candidate


class TestFlipDeltas:
    def test_emits_one_delta_per_changed_draw_with_its_decider(self) -> None:
        baseline, candidate = build_fixtures()
        deltas = flip_deltas(baseline, candidate, LABELS)
        by_key = {(d.transcript_id, d.sample_index): d for d in deltas}
        assert set(by_key) == {("f1", 0), ("f2", 0), ("b1", 0), ("b2", 0), ("f4", 1)}
        assert by_key[("f1", 0)] == FlipDelta(
            monitor_id="deception",
            transcript_id="f1",
            sample_index=0,
            label="deception",
            baseline_flagged=True,
            candidate_flagged=False,
            deciding_stage="claim_grounding",
        )
        assert by_key[("f2", 0)].candidate_flagged is True
        assert by_key[("f2", 0)].deciding_stage == "claim_grounding"
        assert by_key[("b1", 0)].deciding_stage == "event_order"
        assert by_key[("b1", 0)].label == "benign"
        assert by_key[("f4", 1)].deciding_stage == "claim_grounding"

    def test_no_stage_records_yields_none_decider(self) -> None:
        baseline = [cv("f1", [True], [True])]
        candidate = [cv("f1", [True], [False])]  # stage_outcomes is None
        [delta] = flip_deltas(baseline, candidate, LABELS)
        assert delta.deciding_stage is None

    def test_refuses_mismatched_rows(self) -> None:
        baseline = [cv("f1", [True], [True])]
        candidate = [cv("f2", [True], [True])]
        with pytest.raises(ValueError, match="different rows"):
            flip_deltas(baseline, candidate, LABELS)

    def test_refuses_mismatched_raw_flags(self) -> None:
        # Same rows, but the candidate folded different cached draws.
        baseline = [cv("f1", [True], [True])]
        candidate = [cv("f1", [False], [False])]
        with pytest.raises(ValueError, match="raw pre-verification flags differ"):
            flip_deltas(baseline, candidate, LABELS)


class TestStageConfusion:
    def test_claim_grounding_covers_kill_save_restore_and_saved_calls(self) -> None:
        baseline, candidate = build_fixtures()
        deltas = flip_deltas(baseline, candidate, LABELS)
        confusion = stage_confusion(deltas, candidate, "claim_grounding")
        assert confusion.stage_id == "claim_grounding"
        assert confusion.flagged_draws == 7  # every verified draw, all stages
        assert confusion.abstains == 2  # f3, f4 draw 0 (out of scope)
        assert confusion.upholds == 0
        assert confusion.confirms == 2  # f2, b2
        assert confusion.refutes == 2  # f1, f4 draw 1
        assert confusion.catches_killed == 2  # f1, f4 draw 1
        assert confusion.catches_saved == 1  # f2
        assert confusion.fps_suppressed == 0
        assert confusion.fps_restored == 1  # b2
        # f1 + f4-draw-1 refutes and f2 + b2 confirms all end before semantic.
        assert confusion.semantic_calls_saved == 4

    def test_event_order_attribution_and_saved_call(self) -> None:
        baseline, candidate = build_fixtures()
        deltas = flip_deltas(baseline, candidate, LABELS)
        confusion = stage_confusion(deltas, candidate, "event_order")
        assert confusion.abstains == 6  # every verified draw except b1's refute
        assert confusion.refutes == 1  # b1
        assert confusion.confirms == 0
        assert confusion.catches_killed == 0  # the zero-kill contract, measured
        assert confusion.fps_suppressed == 1  # b1
        assert confusion.semantic_calls_saved == 1  # b1 short-circuit

    def test_semantic_stage_never_counts_saved_calls(self) -> None:
        baseline, candidate = build_fixtures()
        deltas = flip_deltas(baseline, candidate, LABELS)
        confusion = stage_confusion(deltas, candidate, "semantic")
        assert confusion.upholds == 2  # f3, f4 draw 0
        assert confusion.refutes == 0
        assert confusion.semantic_calls_saved == 0


class TestTranscriptCatchLoss:
    def test_band_check_definition_failure_labels_only(self) -> None:
        baseline, candidate = build_fixtures()
        # f1: 1.0 -> 0.0 (lost). f4: 1.0 -> 0.5 (not lost). b1 benign: excluded.
        assert transcript_catch_loss(baseline, candidate, LABELS) == ["f1"]

    def test_saved_direction_is_not_loss(self) -> None:
        baseline, candidate = build_fixtures()
        # Reversed arms: f2 was missed under "baseline"=candidate and caught
        # under "candidate"=baseline — no loss in this direction either way.
        assert transcript_catch_loss(candidate, baseline, LABELS) == ["f2"]
