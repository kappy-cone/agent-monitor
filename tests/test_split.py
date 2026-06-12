"""Tests for the base-session-atomic dev/test split."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentmon.eval.split import (
    Split,
    base_source,
    load_labels,
    load_split,
    make_split,
    matched_pairs,
    parse_provenance,
    save_split,
)
from agentmon.schemas import LabeledTranscript

LABELS_PATH = Path(__file__).resolve().parents[1] / "datasets" / "synthetic" / "labels.jsonl"
SPLIT_PATH = LABELS_PATH.parent / "split.json"


def _label(
    transcript_id: str,
    label: str = "benign",
    **notes: object,
) -> LabeledTranscript:
    return LabeledTranscript(transcript_id=transcript_id, label=label, notes=json.dumps(notes))


class TestParseProvenance:
    def test_failure_stratum_is_label_slash_tier(self) -> None:
        prov = parse_provenance(
            _label("sec-bla-01", "security_vuln", base="sub-cli", tier="blatant")
        )
        assert prov.stratum == "security_vuln/blatant"
        assert prov.source == "agentmon"
        assert prov.is_failure

    def test_hard_negative_stratum(self) -> None:
        prov = parse_provenance(
            _label("hn1-a", base="tg2-s07", **{"class": "hard_negative", "hn_pattern": "HN-1"})
        )
        assert prov.stratum == "benign-hard-negative"
        assert prov.hn_pattern == "HN-1"
        assert not prov.is_failure

    def test_filler_stratum_keyed_on_injection_method(self) -> None:
        prov = parse_provenance(
            _label(
                "benf-01", base="tg2-s04", **{"class": "benign"}, injection_method="llm_local_edit"
            )
        )
        assert prov.stratum == "benign-filler"

    def test_plain_benign_stratum(self) -> None:
        prov = parse_provenance(
            _label("ben-tg1-s1", base="tg1-s1", **{"class": "benign"}, injection_method="none")
        )
        assert prov.stratum == "benign-plain"
        assert prov.source == "tinygrad"

    def test_failure_without_tier_raises(self) -> None:
        with pytest.raises(ValueError, match="tier"):
            parse_provenance(_label("x", "deception", base="tg1-s1"))

    def test_missing_base_raises(self) -> None:
        with pytest.raises(ValueError, match="base"):
            parse_provenance(_label("x", "benign", **{"class": "benign"}))

    def test_unknown_base_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="source"):
            base_source("mystery-base")


class TestMatchedPairs:
    def test_pairs_resolve_to_clean_twin(self) -> None:
        labels = [
            _label("ben-x", base="tg1-s1", **{"class": "benign"}, injection_method="none"),
            _label("sec-x", "security_vuln", base="tg1-s1", tier="blatant", pair_with="ben-x"),
        ]
        provs = [parse_provenance(label) for label in labels]
        assert matched_pairs(provs) == [("ben-x", "sec-x", "security_vuln/blatant")]

    def test_missing_twin_raises(self) -> None:
        labels = [
            _label("sec-x", "security_vuln", base="tg1-s1", tier="blatant", pair_with="ben-x"),
        ]
        with pytest.raises(ValueError, match="ben-x"):
            matched_pairs([parse_provenance(label) for label in labels])


class TestMakeSplitOnRealDataset:
    """The split contract, checked against the real labels file."""

    @pytest.fixture(scope="class")
    def labels(self) -> list[LabeledTranscript]:
        return load_labels(LABELS_PATH)

    @pytest.fixture(scope="class")
    def split(self, labels: list[LabeledTranscript]) -> Split:
        return make_split(labels, seed=20260611, n_candidates=500)

    def test_partitions_every_transcript_exactly_once(
        self, labels: list[LabeledTranscript], split: Split
    ) -> None:
        all_ids = {label.transcript_id for label in labels}
        assert set(split.dev_ids) | set(split.test_ids) == all_ids
        assert not set(split.dev_ids) & set(split.test_ids)

    def test_base_atomicity(self, labels: list[LabeledTranscript], split: Split) -> None:
        dev_ids = set(split.dev_ids)
        for label in labels:
            prov = parse_provenance(label)
            side = "dev" if label.transcript_id in dev_ids else "test"
            expected = "dev" if prov.base in set(split.dev_bases) else "test"
            assert side == expected, f"{label.transcript_id} not on its base's side"

    def test_matched_pairs_colocated(self, labels: list[LabeledTranscript], split: Split) -> None:
        provs = [parse_provenance(label) for label in labels]
        dev_ids = set(split.dev_ids)
        for clean, injected, _ in matched_pairs(provs):
            assert (clean in dev_ids) == (injected in dev_ids)

    def test_hard_constraints_satisfied(self, split: Split) -> None:
        assert split.constraint_violations == 0

    def test_deterministic_for_fixed_seed(self, labels: list[LabeledTranscript]) -> None:
        a = make_split(labels, seed=7, n_candidates=50)
        b = make_split(labels, seed=7, n_candidates=50)
        assert a.model_dump() == b.model_dump()

    def test_round_trips_through_disk(self, split: Split, tmp_path: Path) -> None:
        path = tmp_path / "split.json"
        save_split(split, path)
        assert load_split(path).model_dump() == split.model_dump()

    def test_side_of(self, split: Split) -> None:
        assert split.side_of(split.dev_ids[0]) == "dev"
        assert split.side_of(split.test_ids[0]) == "test"
        with pytest.raises(KeyError):
            split.side_of("nope")


class TestPersistedSplit:
    """The committed split.json is the sacred artifact; pin its contract.

    Headline numbers are computed on this exact split — regenerating it with
    different parameters after prompt iteration began would be leakage.
    """

    @pytest.fixture(scope="class")
    def split(self) -> Split:
        if not SPLIT_PATH.exists():
            pytest.skip("datasets/synthetic/split.json not generated")
        return load_split(SPLIT_PATH)

    def test_partitions_the_labels_file(self, split: Split) -> None:
        labels = load_labels(LABELS_PATH)
        all_ids = {label.transcript_id for label in labels}
        assert set(split.dev_ids) | set(split.test_ids) == all_ids
        assert not set(split.dev_ids) & set(split.test_ids)

    def test_base_atomic_against_labels(self, split: Split) -> None:
        dev_bases, test_bases = set(split.dev_bases), set(split.test_bases)
        assert not dev_bases & test_bases
        dev_ids = set(split.dev_ids)
        for label in load_labels(LABELS_PATH):
            prov = parse_provenance(label)
            in_dev = label.transcript_id in dev_ids
            assert prov.base in (dev_bases if in_dev else test_bases)

    def test_hard_constraints_hold(self, split: Split) -> None:
        assert split.constraint_violations == 0
