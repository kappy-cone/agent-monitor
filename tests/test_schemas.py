"""Tests for the core schema contracts: union round-trips and field validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentmon.schemas import (
    AssistantMessage,
    FailureCategory,
    FileDiff,
    LabeledTranscript,
    MonitorVerdict,
    OtherEvent,
    ShellCommand,
    ToolCall,
    ToolResult,
    Transcript,
    UserMessage,
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
