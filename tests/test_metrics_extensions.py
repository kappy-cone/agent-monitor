"""Hand-computed checks for the calibrated-scoring eval metrics."""

from __future__ import annotations

import pytest

from agentmon.eval.metrics import (
    auroc,
    cluster_bootstrap_ci,
    expected_calibration_error,
    matched_pair_deltas,
    precision_at_prevalence,
    reliability_bins,
    reweighted_pr_curve,
)


class TestReweightedPrCurve:
    # scores [0.9, 0.8, 0.6, 0.4], positives [T, F, T, F], rho = 0.5.
    # n_pos = n_neg = 2 -> weight w = 2 * 0.5 / (0.5 * 2) = 1.0 (balanced already).
    def test_balanced_target_matches_plain_precision(self) -> None:
        points = reweighted_pr_curve([0.9, 0.8, 0.6, 0.4], [True, False, True, False], 0.5)
        assert points == [
            (0.9, 1.0, 0.5),  # tp=1, fp=0
            (0.8, 0.5, 0.5),  # tp=1, fp=1
            (0.6, pytest.approx(2 / 3), 1.0),  # tp=2, fp=1
            (0.4, 0.5, 1.0),  # tp=2, fp=2
        ]

    # Same data, rho = 0.01: w = 2 * 0.99 / (0.01 * 2) = 99.
    # At threshold 0.6: tp=2, fp=1 -> precision = 2 / (2 + 99) = 2/101.
    def test_one_percent_prevalence_hand_computed(self) -> None:
        points = reweighted_pr_curve([0.9, 0.8, 0.6, 0.4], [True, False, True, False], 0.01)
        by_threshold = {t: (p, r) for t, p, r in points}
        assert by_threshold[0.6][0] == pytest.approx(2 / 101)
        assert by_threshold[0.6][1] == 1.0
        assert by_threshold[0.9][0] == 1.0  # no false positives yet

    def test_single_class_returns_empty(self) -> None:
        assert reweighted_pr_curve([0.5, 0.6], [True, True], 0.01) == []
        assert reweighted_pr_curve([0.5, 0.6], [False, False], 0.01) == []

    def test_invalid_prevalence_raises(self) -> None:
        with pytest.raises(ValueError, match="target_prevalence"):
            reweighted_pr_curve([0.5], [True], 0.0)


class TestPrecisionAtPrevalence:
    def test_hand_computed(self) -> None:
        scores = [0.9, 0.8, 0.6, 0.4]
        positives = [True, False, True, False]
        assert precision_at_prevalence(scores, positives, 0.6, 0.01) == pytest.approx(2 / 101)

    def test_nothing_flagged_returns_none(self) -> None:
        assert precision_at_prevalence([0.1, 0.2], [True, False], 0.9, 0.01) is None

    def test_single_class_returns_none(self) -> None:
        assert precision_at_prevalence([0.1, 0.2], [True, True], 0.1, 0.01) is None


class TestMatchedPairDeltas:
    def test_deltas_are_injected_minus_clean(self) -> None:
        scores = {"clean-a": 0.2, "inj-a": 1.0, "clean-b": 0.4, "inj-b": 0.2}
        deltas = matched_pair_deltas(scores, [("clean-a", "inj-a"), ("clean-b", "inj-b")])
        assert deltas == [pytest.approx(0.8), pytest.approx(-0.2)]

    def test_missing_score_raises_with_ids(self) -> None:
        with pytest.raises(ValueError, match="inj-a"):
            matched_pair_deltas({"clean-a": 0.2}, [("clean-a", "inj-a")])


class TestClusterBootstrap:
    def test_deterministic_for_fixed_seed(self) -> None:
        scores = [0.1, 0.2, 0.8, 0.9, 0.3, 0.7]
        positives = [False, False, True, True, False, True]
        groups = ["a", "a", "b", "b", "c", "c"]
        ci_one = cluster_bootstrap_ci(scores, positives, groups, auroc, n_resamples=200, seed=1)
        ci_two = cluster_bootstrap_ci(scores, positives, groups, auroc, n_resamples=200, seed=1)
        assert ci_one is not None
        assert ci_one.model_dump() == ci_two.model_dump()  # type: ignore[union-attr]

    def test_identical_groups_collapse_to_point(self) -> None:
        # Both groups are identical, so every resample yields the same AUROC.
        scores = [0.1, 0.9, 0.1, 0.9]
        positives = [False, True, False, True]
        groups = ["a", "a", "b", "b"]
        ci = cluster_bootstrap_ci(scores, positives, groups, auroc, n_resamples=100, seed=3)
        assert ci is not None
        assert ci.lower == ci.upper == 1.0
        assert ci.n_resamples_used == 100

    def test_single_class_resamples_are_skipped(self) -> None:
        # Group "b" holds every positive: resamples drawing only "a" yield None.
        scores = [0.1, 0.2, 0.9]
        positives = [False, False, True]
        groups = ["a", "a", "b"]
        ci = cluster_bootstrap_ci(scores, positives, groups, auroc, n_resamples=100, seed=5)
        assert ci is not None
        assert ci.n_resamples_used < 100

    def test_all_invalid_returns_none(self) -> None:
        ci = cluster_bootstrap_ci([0.5], [True], ["a"], auroc, n_resamples=10, seed=0)
        assert ci is None

    def test_invalid_confidence_raises(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            cluster_bootstrap_ci([0.5], [True], ["a"], auroc, confidence=1.0)


class TestReliability:
    def test_bins_on_natural_fractions(self) -> None:
        # k=5; predictions sit on j/5 plus a sub-1e-3 epsilon.
        predicted = [0.0001, 0.2003, 0.2001, 1.0005, 0.8002]
        positives = [False, False, True, True, True]
        bins = reliability_bins(predicted, positives, k=5)
        by_center = {b.predicted: b for b in bins}
        assert set(by_center) == {0.0, 0.2, 0.8, 1.0}
        assert by_center[0.2].n == 2
        assert by_center[0.2].observed_rate == 0.5
        assert by_center[0.0].observed_rate == 0.0
        assert by_center[1.0].observed_rate == 1.0

    def test_ece_hand_computed(self) -> None:
        # Two bins: (n=2, mean_pred ~0.2, observed 0.5) and (n=2, mean_pred ~1.0, observed 1.0).
        predicted = [0.2, 0.2, 1.0, 1.0]
        positives = [False, True, True, True]
        bins = reliability_bins(predicted, positives, k=5)
        ece = expected_calibration_error(bins)
        # |0.2 - 0.5| * 2/4 + |1.0 - 1.0| * 2/4 = 0.15
        assert ece == pytest.approx(0.15)

    def test_empty_bins_omitted(self) -> None:
        bins = reliability_bins([0.0, 1.0], [False, True], k=5)
        assert [b.predicted for b in bins] == [0.0, 1.0]

    def test_ece_of_no_bins_is_none(self) -> None:
        assert expected_calibration_error([]) is None

    def test_out_of_range_prediction_raises(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            reliability_bins([1.5], [True], k=5)

    def test_k_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="k must be positive"):
            reliability_bins([0.5], [True], k=0)
