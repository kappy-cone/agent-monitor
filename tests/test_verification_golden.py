"""Golden-master characterization of the verification flip behavior.

Pins the committed test-matrix verdicts so the verification-pipeline refactor
(``docs/architecture-deepening.md`` candidate 1) can prove it changes nothing.
The default pipeline must keep both layers below green.

Two layers:

1. **Flip-rule pin** (always runs, CI-durable): recompute the post-flip flags
   and the calibrated score from each committed verdict's ``samples`` +
   ``verifications`` and assert they equal the recorded values. Catches any
   change to the flip rule (flagged ⇒ unflagged on unsupported) or the
   calibrated-score formula. Needs only the committed verdicts.

2. **Cache-replay pin** (skips if ``.agentmon_cache`` is absent — i.e. in CI):
   replay ``run_calibrated`` over the test split against the on-disk cache with
   a client that *raises on any call*, and assert the produced
   ``CalibratedVerdict`` reproduces the committed one byte-for-byte. Catches any
   change to verification *production* (the stages), not just the flip rule. Run
   it locally, where the cache from the test matrix exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentmon.calibration import calibrated_score, run_calibrated
from agentmon.eval.split import load_split
from agentmon.monitors.registry import load_monitors
from agentmon.schemas import CalibratedVerdict, Transcript

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "results" / "gemma-qwen-test-matrix"
CACHE = REPO / ".agentmon_cache"
SPLIT = REPO / "datasets" / "synthetic" / "split.json"
TRANSCRIPTS = REPO / "datasets" / "synthetic" / "transcripts"

MONITORS = ["security_vuln", "scope_expansion", "reward_hacking", "deception", "generalist"]
#: substrate dir under results/ -> the model_override that keys its cache + verdicts
SUBSTRATES = {"gemma": "agentmon-local-gemma", "qwen": "agentnom-local"}


def _committed(substrate: str, monitor: str) -> list[CalibratedVerdict]:
    path = RESULTS / substrate / f"test-{monitor}" / "verdicts.jsonl"
    return [
        CalibratedVerdict.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _replayed_flags(verdict: CalibratedVerdict) -> list[bool]:
    """Recompute post-verification flags from samples + verifications.

    This is the flip rule the refactor must preserve: a sample is flagged when
    it emits a category, then flips to unflagged if its verification outcome is
    unsupported (a present outcome that is not ``supported``).
    """
    flags: list[bool] = []
    for sample, outcome in zip(verdict.samples, verdict.verifications, strict=True):
        flagged = len(sample.categories) > 0
        if flagged and outcome is not None:
            flagged = outcome.supported
        flags.append(flagged)
    return flags


class _NoCallClient:
    """A client that fails loudly on any live call — proves every read is cached."""

    request_count = 0

    def complete(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("cache MISS: live call attempted (cache incomplete or key drift)")


@pytest.mark.parametrize("substrate", SUBSTRATES)
@pytest.mark.parametrize("monitor", MONITORS)
def test_committed_verdicts_obey_the_flip_rule(substrate: str, monitor: str) -> None:
    """Layer 1: the recorded flip + score follow from samples + verifications."""
    for verdict in _committed(substrate, monitor):
        flags = _replayed_flags(verdict)
        assert flags == verdict.sample_flagged, verdict.transcript_id
        assert sum(flags) / verdict.k == verdict.fraction_flagged, verdict.transcript_id
        assert verdict.calibrated_score == calibrated_score(
            verdict.fraction_flagged, verdict.mean_suspicion
        ), verdict.transcript_id


@pytest.mark.skipif(
    not CACHE.exists(),
    reason="no .agentmon_cache (gitignored) — run locally to replay verification production",
)
@pytest.mark.parametrize("substrate,model", list(SUBSTRATES.items()))
@pytest.mark.parametrize("monitor", MONITORS)
def test_cache_replay_reproduces_committed_verdicts(
    substrate: str, model: str, monitor: str
) -> None:
    """Layer 2: replaying the default pipeline from cache reproduces the verdicts exactly."""
    transcripts = [
        Transcript.model_validate_json((TRANSCRIPTS / f"{tid}.json").read_text(encoding="utf-8"))
        for tid in sorted(load_split(SPLIT).test_ids)
    ]
    got = run_calibrated(
        [load_monitors()[monitor]],
        transcripts,
        _NoCallClient(),
        k=3,
        cache_dir=CACHE,
        verify=True,
        model_override=model,
    )
    want = {v.transcript_id: v for v in _committed(substrate, monitor)}
    assert {g.transcript_id for g in got} == set(want)
    for g in got:
        assert g.model_dump_json() == want[g.transcript_id].model_dump_json(), g.transcript_id
