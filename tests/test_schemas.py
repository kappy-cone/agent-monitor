"""Tests for the core schema contracts: union round-trips and field validation."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agentmon.schemas import (
    AssistantMessage,
    CalibratedVerdict,
    CompositeProvenance,
    FailureCategory,
    FileDiff,
    LabeledTranscript,
    MonitorVerdict,
    OtherEvent,
    ShellCommand,
    StageOutcome,
    ToolCall,
    ToolResult,
    Transcript,
    UserMessage,
    VerificationOutcome,
)


class TestEventRoundTrip:
    def test_every_kind_resolves_through_discriminated_union(self) -> None:
        events = [
            UserMessage(index=0, text="hi", raw={"src": "user"}),
            AssistantMessage(index=1, text="hello"),
            ToolCall(index=2, tool="Read", tool_input={"file_path": "/tmp/a"}),
            ToolResult(index=3, tool="Read", content="data", is_error=True),
            FileDiff(index=4, path="/tmp/a", diff="-x\n+y"),
            ShellCommand(index=5, command="ls", output="a b", exit_code=0),
            OtherEvent(index=6, description="system record"),
        ]
        transcript = Transcript(id="rt", source="synthetic", events=events)
        restored = Transcript.model_validate_json(transcript.model_dump_json())
        assert restored == transcript
        # The discriminator must resolve each event to its concrete class.
        assert [type(e) for e in restored.events] == [type(e) for e in events]


class TestMonitorVerdict:
    def _kwargs(self, score: int) -> dict:
        return {
            "monitor_id": "m1",
            "transcript_id": "t1",
            "suspicion_score": score,
            "model": "mock-model",
        }

    def test_accepts_bounds(self) -> None:
        assert MonitorVerdict(**self._kwargs(0)).suspicion_score == 0
        assert MonitorVerdict(**self._kwargs(100)).suspicion_score == 100

    def test_rejects_101(self) -> None:
        with pytest.raises(ValidationError):
            MonitorVerdict(**self._kwargs(101))

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            MonitorVerdict(**self._kwargs(-1))


class TestStageOutcomeAdditivity:
    def test_pre_details_pre_terminal_json_round_trips(self) -> None:
        # A StageOutcome serialized before `details`/`terminal` existed must
        # deserialize with the additive defaults and re-serialize compatibly.
        old_json = json.dumps(
            {
                "stage_id": "semantic",
                "supported": False,
                "quote_match": True,
                "reasoning": "unsupported",
                "model": "mock-model",
                "input_tokens": 10,
                "output_tokens": 5,
                "latency_ms": 1.5,
                "raw_response": "{}",
                "parse_error": None,
            }
        )
        outcome = StageOutcome.model_validate_json(old_json)
        assert outcome.terminal is False
        assert outcome.details == {}
        restored = StageOutcome.model_validate_json(outcome.model_dump_json())
        assert restored == outcome
        # Every pre-existing field survives the round trip unchanged.
        dumped = json.loads(outcome.model_dump_json())
        for key, value in json.loads(old_json).items():
            assert dumped[key] == value


class TestCompositeProvenanceAdditivity:
    def test_pre_composition_json_round_trips_with_defaults(self) -> None:
        # A CompositeProvenance serialized before `member_flagged` /
        # `member_verifications` / `inner` existed must deserialize with the
        # additive defaults and re-serialize compatibly.
        old_json = json.dumps(
            {
                "composite": "ens",
                "driver": "security_vuln",
                "member_scores": {"security_vuln": 60},
                "escalated": False,
                "member_supported": {},
            }
        )
        prov = CompositeProvenance.model_validate_json(old_json)
        assert prov.member_flagged == {}
        assert prov.member_verifications == {}
        assert prov.inner is None
        dumped = json.loads(prov.model_dump_json())
        for key, value in json.loads(old_json).items():
            assert dumped[key] == value

    def test_inner_self_reference_round_trips(self) -> None:
        inner = CompositeProvenance(
            composite="vens",
            driver="deception",
            member_flagged={"deception": True},
            member_verifications={"deception": VerificationOutcome(supported=False)},
        )
        outer = CompositeProvenance(composite="vcasc", driver="vens", escalated=True, inner=inner)
        restored = CompositeProvenance.model_validate_json(outer.model_dump_json())
        assert restored == outer
        assert restored.inner is not None
        assert restored.inner.member_verifications["deception"].supported is False


class TestTotalsNeverSumMemberVerifications:
    def test_member_verification_tokens_ride_in_extras_only(self) -> None:
        # Regression pin (cost double-counting hazard): the per-member
        # verification usage recorded in provenance.member_verifications is
        # ALREADY folded into the draw's extras by the verified ensemble. The
        # CalibratedVerdict totals must read samples + verifications + extras
        # only — never provenance — or every member verification counts twice.
        loud_outcome = VerificationOutcome(
            supported=False, input_tokens=1000, output_tokens=500, latency_ms=9.0
        )
        sample = MonitorVerdict(
            monitor_id="vens",
            transcript_id="t1",
            suspicion_score=70,
            model="mock-model",
            input_tokens=10,
            output_tokens=5,
            provenance=CompositeProvenance(
                composite="vens",
                driver="security_vuln",
                member_flagged={"security_vuln": True},
                member_verifications={"security_vuln": loud_outcome},
            ),
        )
        verdict = CalibratedVerdict(
            monitor_id="vens",
            transcript_id="t1",
            model="mock-model",
            k=1,
            temperature=0.7,
            samples=[sample],
            verifications=[None],
            sample_flagged=[True],
            fraction_flagged=1.0,
            mean_suspicion=70.0,
            calibrated_score=1.0007,
            extra_input_tokens=3,
            extra_output_tokens=2,
        )
        assert verdict.total_input_tokens == 13  # 10 sample + 3 extras; not 1013
        assert verdict.total_output_tokens == 7  # 5 sample + 2 extras; not 507
        assert verdict.total_latency_ms == 0.0  # the outcome's 9.0 ms is not summed


class TestLabeledTranscript:
    def test_accepts_benign(self) -> None:
        labeled = LabeledTranscript(transcript_id="t1", label="benign")
        assert labeled.label == "benign"

    @pytest.mark.parametrize("category", list(FailureCategory))
    def test_accepts_every_failure_category(self, category: FailureCategory) -> None:
        labeled = LabeledTranscript(transcript_id="t1", label=category.value)
        assert labeled.label == category.value

    def test_rejects_bogus(self) -> None:
        with pytest.raises(ValidationError):
            LabeledTranscript(transcript_id="t1", label="bogus")
