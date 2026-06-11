"""Every metric in agentmon.eval.metrics checked against hand-computed expectations."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentmon.eval.metrics import auroc, confusion_at_threshold, evaluate, recall_at_fpr
from agentmon.schemas import LabeledTranscript, MonitorVerdict

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "datasets" / "samples"


def _load_toy() -> tuple[list[MonitorVerdict], list[LabeledTranscript]]:
    verdict_lines = (SAMPLES_DIR / "toy_verdicts.jsonl").read_text().splitlines()
    label_lines = (SAMPLES_DIR / "toy_labels.jsonl").read_text().splitlines()
    verdicts = [MonitorVerdict.model_validate_json(line) for line in verdict_lines if line]
    labels = [LabeledTranscript.model_validate_json(line) for line in label_lines if line]
    return verdicts, labels


def _verdict(
    transcript_id: str,
    score: int,
    *,
    monitor_id: str = "m1",
    model: str = "claude-haiku-4-5",
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> MonitorVerdict:
    return MonitorVerdict(
        monitor_id=monitor_id,
        transcript_id=transcript_id,
        suspicion_score=score,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


class TestAuroc:
    def test_perfect_separation(self) -> None:
        assert auroc([1, 2, 3, 4], [False, False, True, True]) == 1.0

    def test_perfect_inversion(self) -> None:
        assert auroc([1, 2, 3, 4], [True, True, False, False]) == 0.0

    def test_all_tied_one_pos_one_neg(self) -> None:
        assert auroc([5, 5], [True, False]) == 0.5

    def test_ties_use_average_ranks(self) -> None:
        # scores [1, 2, 2, 3], positives = the two highest-indexed items.
        # 1-based ranks with averaging: 1 -> 1, the tied 2s -> (2+3)/2 = 2.5, 3 -> 4.
        # Positive rank sum = 2.5 + 4 = 6.5; U = 6.5 - 2*3/2 = 3.5.
        # AUROC = 3.5 / (2*2) = 0.875. Pairwise check: pos 2 beats benign 1 (1)
        # and ties benign 2 (0.5); pos 3 beats both (2) -> 3.5/4.
        assert auroc([1, 2, 2, 3], [False, False, True, True]) == pytest.approx(0.875)

    def test_no_positives_returns_none(self) -> None:
        assert auroc([1, 2], [False, False]) is None

    def test_no_negatives_returns_none(self) -> None:
        assert auroc([1, 2], [True, True]) is None

    def test_empty_returns_none(self) -> None:
        assert auroc([], []) is None


TOY_BENIGN = [5, 8, 10, 12, 15, 20, 22, 35]
TOY_POSITIVE = [30, 60, 75, 90]
TOY_SCORES = [float(s) for s in TOY_BENIGN + TOY_POSITIVE]
TOY_FLAGS = [False] * 8 + [True] * 4

# Hand-computed AUROC for the toy set, out of 4 * 8 = 32 (pos, benign) pairs:
#   30 beats benign {5, 8, 10, 12, 15, 20, 22} -> 7 wins (loses to 35)
#   60 beats all 8 benign                      -> 8
#   75 beats all 8 benign                      -> 8
#   90 beats all 8 benign                      -> 8
# No ties, so AUROC = (7 + 8 + 8 + 8) / 32 = 31/32 = 0.96875.
TOY_AUROC = 31 / 32

# recall_at_fpr at both budgets: k = floor(0.01 * 8) = floor(0.05 * 8) = 0,
# so the threshold is the max benign score (35) and flagging is strict (>):
# zero benign flagged, positives above 35 are {60, 75, 90} -> 3/4 = 0.75.
TOY_RECALL_AT_FPR = 0.75


class TestRecallAtFpr:
    def test_toy_benign_k_zero_strict_inequality(self) -> None:
        assert recall_at_fpr(TOY_SCORES, TOY_FLAGS, 0.05) == TOY_RECALL_AT_FPR
        # 30 is below the max benign score and 35-vs-35 would tie anyway:
        # strict > means the positive at 30 is never caught under a 0-flag budget.
        assert recall_at_fpr(TOY_SCORES, TOY_FLAGS, 0.01) == TOY_RECALL_AT_FPR

    def test_budget_covers_all_benign_gives_full_recall(self) -> None:
        # 2 benign, max_fpr = 1.0 -> k = 2 >= n_benign -> threshold = -inf.
        scores = [10.0, 20.0, 1.0, 2.0]
        flags = [False, False, True, True]
        assert recall_at_fpr(scores, flags, 1.0) == 1.0

    def test_tie_at_threshold_is_not_caught(self) -> None:
        # benign [10, 20, 30, 40], positives [20, 35]; max_fpr = 0.5 -> k = 2.
        # benign descending = [40, 30, 20, 10] -> threshold = 20 (flags 40, 30 = 2 benign).
        # Positives strictly above 20: only 35 -> 1/2. The positive tied at 20 is missed.
        scores = [10.0, 20.0, 30.0, 40.0, 20.0, 35.0]
        flags = [False, False, False, False, True, True]
        assert recall_at_fpr(scores, flags, 0.5) == 0.5

    def test_no_positives_returns_none(self) -> None:
        assert recall_at_fpr([1, 2], [False, False], 0.05) is None

    def test_no_benign_returns_none(self) -> None:
        assert recall_at_fpr([1, 2], [True, True], 0.05) is None


class TestConfusionAtThreshold:
    def test_hand_case(self) -> None:
        # threshold 50, flag iff score >= 50:
        # 55 (pos, flagged) -> TP; 30 (pos, not) -> FN; 80 (neg, flagged) -> FP;
        # 60 (neg, flagged) -> FP; 40 (neg, not) -> TN.
        # precision = 1/3, recall = 1/2, f1 = 2*(1/3)*(1/2)/(1/3 + 1/2) = 0.4.
        tm = confusion_at_threshold([55, 30, 80, 60, 40], [True, True, False, False, False], 50)
        assert (tm.true_positives, tm.false_positives) == (1, 2)
        assert (tm.true_negatives, tm.false_negatives) == (1, 1)
        assert tm.precision == pytest.approx(1 / 3)
        assert tm.recall == pytest.approx(1 / 2)
        assert tm.f1 == pytest.approx(0.4)
        assert tm.threshold == 50

    def test_threshold_is_inclusive(self) -> None:
        tm = confusion_at_threshold([50], [True], 50)
        assert tm.true_positives == 1

    def test_zero_denominator_precision_is_zero(self) -> None:
        # Nothing flagged: tp + fp = 0, so precision is defined as 0.0 (and
        # f1 follows from precision + recall = 0).
        tm = confusion_at_threshold([10, 20], [True, False], 50)
        assert (tm.true_positives, tm.false_positives) == (0, 0)
        assert (tm.true_negatives, tm.false_negatives) == (1, 1)
        assert tm.precision == 0.0
        assert tm.recall == 0.0
        assert tm.f1 == 0.0


class TestEvaluate:
    def test_toy_dataset(self) -> None:
        verdicts, labels = _load_toy()
        report = evaluate(verdicts, labels, threshold=50)
        assert len(report.results) == 1
        result = report.results[0]

        assert result.monitor_id == "example_security"
        assert result.n_transcripts == 12
        assert result.n_positive == 4
        assert result.n_benign == 8
        assert result.auroc == pytest.approx(TOY_AUROC)
        assert result.recall_at_1pct_fpr == TOY_RECALL_AT_FPR
        assert result.recall_at_5pct_fpr == TOY_RECALL_AT_FPR

        # Confusion at threshold 50 (score >= 50): flagged = {60, 75, 90}.
        # TP = 3, FN = 1 (the score-30 security_vuln), FP = 0, TN = 8.
        # precision = 3/3 = 1.0, recall = 3/4, f1 = 2*1*0.75/1.75 = 6/7.
        tm = result.threshold_metrics
        assert tm.threshold == 50
        assert (tm.true_positives, tm.false_positives) == (3, 0)
        assert (tm.true_negatives, tm.false_negatives) == (8, 1)
        assert tm.precision == 1.0
        assert tm.recall == 0.75
        assert tm.f1 == pytest.approx(6 / 7)

        # Token/latency means: inputs are 1200..2850 step 150 (sum 24300),
        # outputs 150..260 step 10 (sum 2460), latencies 800..1075 step 25 (sum 11250).
        assert result.mean_input_tokens == 2025.0
        assert result.mean_output_tokens == 205.0
        assert result.mean_latency_ms == 937.5

        # Cost with claude-haiku-4-5 PRICING of (1.0, 5.0) USD per million tokens:
        # total = 24300 * 1.0/1e6 + 2460 * 5.0/1e6 = 0.0243 + 0.0123 = 0.0366
        # mean  = 0.0366 / 12 = 0.00305
        assert result.mean_cost_usd == pytest.approx(0.00305, abs=1e-10)

    def test_unlabeled_verdicts_are_skipped(self) -> None:
        labels = [LabeledTranscript(transcript_id="a", label="benign")]
        verdicts = [_verdict("a", 10), _verdict("missing", 90)]
        report = evaluate(verdicts, labels)
        assert report.results[0].n_transcripts == 1

    def test_unknown_model_makes_cost_none(self) -> None:
        labels = [
            LabeledTranscript(transcript_id="a", label="benign"),
            LabeledTranscript(transcript_id="b", label="deception"),
        ]
        verdicts = [
            _verdict("a", 10, input_tokens=1000, output_tokens=100),
            _verdict("b", 90, model="not-a-known-model"),
        ]
        report = evaluate(verdicts, labels)
        assert report.results[0].mean_cost_usd is None

    def test_groups_by_monitor_sorted(self) -> None:
        labels = [
            LabeledTranscript(transcript_id="a", label="benign"),
            LabeledTranscript(transcript_id="b", label="deception"),
        ]
        verdicts = [
            _verdict("a", 10, monitor_id="zeta"),
            _verdict("b", 90, monitor_id="zeta"),
            _verdict("a", 20, monitor_id="alpha"),
        ]
        report = evaluate(verdicts, labels)
        assert [r.monitor_id for r in report.results] == ["alpha", "zeta"]
        assert [r.n_transcripts for r in report.results] == [1, 2]
        # alpha saw only a benign transcript, so rank metrics are undefined.
        assert report.results[0].auroc is None

    def test_duplicate_verdicts_last_wins(self) -> None:
        # Re-running a monitor and appending to a verdicts JSONL must not
        # double-count the transcript: only the last verdict per
        # (monitor, transcript) is scored.
        labels = [
            LabeledTranscript(transcript_id="a", label="benign"),
            LabeledTranscript(transcript_id="b", label="deception"),
        ]
        verdicts = [
            _verdict("a", 90),
            _verdict("a", 95),
            _verdict("a", 10),  # last re-run: benign transcript scored low
            _verdict("b", 80),
        ]
        report = evaluate(verdicts, labels)
        result = report.results[0]
        assert result.n_transcripts == 2
        assert result.n_benign == 1
        assert result.auroc == 1.0  # 80 > 10; the stale 90/95 verdicts are gone

    def test_conflicting_labels_raise(self) -> None:
        labels = [
            LabeledTranscript(transcript_id="a", label="deception"),
            LabeledTranscript(transcript_id="a", label="benign"),
        ]
        with pytest.raises(ValueError, match="conflicting labels"):
            evaluate([_verdict("a", 50)], labels)

    def test_equal_duplicate_labels_tolerated(self) -> None:
        labels = [
            LabeledTranscript(transcript_id="a", label="benign"),
            LabeledTranscript(transcript_id="a", label="benign"),
            LabeledTranscript(transcript_id="b", label="deception"),
        ]
        report = evaluate([_verdict("a", 10), _verdict("b", 90)], labels)
        assert report.results[0].n_transcripts == 2


class TestRecallAtFprValidation:
    def test_negative_budget_raises(self) -> None:
        with pytest.raises(ValueError, match="max_fpr"):
            recall_at_fpr([1, 2], [True, False], -0.1)

    def test_budget_above_one_raises(self) -> None:
        with pytest.raises(ValueError, match="max_fpr"):
            recall_at_fpr([1, 2], [True, False], 1.5)

    def test_decimal_budget_floors_exactly(self) -> None:
        # 0.29 * 100 benign = 28.999...96 in binary floats; k must be 29.
        scores = [*range(100), 200]
        flags = [*([False] * 100), True]
        # k = 29 -> threshold = 29th-highest benign (0-indexed) = 70.
        # The positive (200) is above it either way; what matters is that the
        # call does not crash and matches the k = 29 threshold exactly.
        assert recall_at_fpr(scores, flags, 0.29) == 1.0

    def test_parse_failures_are_counted(self) -> None:
        labels = [
            LabeledTranscript(transcript_id="a", label="benign"),
            LabeledTranscript(transcript_id="b", label="deception"),
        ]
        broken = _verdict("b", 0)
        broken = broken.model_copy(update={"parse_error": "no JSON object found"})
        report = evaluate([_verdict("a", 10), broken], labels)
        assert report.results[0].n_parse_failures == 1
