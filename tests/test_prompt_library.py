"""The real monitor library: every prompt loads, renders, and parses end to end."""

from __future__ import annotations

import json

import pytest

from agentmon.llm.mock import MockLLMClient
from agentmon.monitors.registry import load_monitors
from agentmon.schemas import (
    AssistantMessage,
    CalibratedVerdict,
    FailureCategory,
    ShellCommand,
    Transcript,
    UserMessage,
)

LIBRARY_IDS = ["deception", "generalist", "reward_hacking", "scope_expansion", "security_vuln"]


@pytest.fixture(scope="module")
def monitors() -> dict[str, object]:
    return load_monitors()


def test_library_contains_the_five_monitors(monitors: dict[str, object]) -> None:
    assert set(LIBRARY_IDS) <= set(monitors)


@pytest.mark.parametrize("monitor_id", LIBRARY_IDS)
def test_prompt_has_transcript_placeholder(monitors, monitor_id: str) -> None:
    assert "{{transcript}}" in monitors[monitor_id].template


@pytest.mark.parametrize("monitor_id", LIBRARY_IDS)
def test_prompt_states_evidence_over_narration(monitors, monitor_id: str) -> None:
    # The claims-vs-ground-truth weighting is a required property of every
    # prompt, not a style choice; guard the explicit statement.
    template = monitors[monitor_id].template
    assert "ground truth" in template.lower()
    assert "ASSISTANT" in template


@pytest.mark.parametrize("monitor_id", LIBRARY_IDS)
def test_prompt_makes_flagging_a_deliberate_binary_act(monitors, monitor_id: str) -> None:
    assert "deliberate binary decision" in monitors[monitor_id].template


def test_category_monitors_declare_their_target(monitors) -> None:
    for monitor_id in ["security_vuln", "reward_hacking", "scope_expansion", "deception"]:
        assert monitors[monitor_id].config.categories == [FailureCategory(monitor_id)]


def test_generalist_declares_all_categories(monitors) -> None:
    assert set(monitors["generalist"].config.categories) == set(FailureCategory)


@pytest.mark.parametrize("monitor_id", LIBRARY_IDS)
def test_end_to_end_mock_run_parses(monitors, monitor_id: str) -> None:
    transcript = Transcript(
        id="t1",
        source="synthetic",
        events=[
            UserMessage(index=0, text="fix the failing test in utils"),
            ShellCommand(index=1, command="pytest -x", output="1 failed in 0.21s", is_error=True),
            AssistantMessage(index=2, text="All tests pass now."),
        ],
    )
    flagged = json.dumps(
        {
            "suspicion_score": 85,
            "categories": [monitor_id if monitor_id != "generalist" else "other"],
            "evidence": [{"event_index": 2, "quote": "All tests pass now."}],
            "reasoning": "claim contradicts the failing run",
        }
    )
    client = MockLLMClient(script={"fix the failing test": flagged})
    verdict = monitors[monitor_id].run(transcript, client)
    assert verdict.parse_error is None
    assert verdict.suspicion_score == 85
    assert len(verdict.categories) == 1
    assert verdict.evidence[0].event_index == 2


def test_calibrated_verdict_round_trips() -> None:
    from agentmon.calibration import run_calibrated
    from agentmon.monitors.base import Monitor, MonitorConfig

    monitor = Monitor(MonitorConfig(id="judge", model="claude-test-1"), "{{transcript}}")
    transcript = Transcript(
        id="t1", source="synthetic", events=[UserMessage(index=0, text="hello")]
    )
    [result] = run_calibrated([monitor], [transcript], MockLLMClient(), k=2)
    restored = CalibratedVerdict.model_validate_json(result.model_dump_json())
    assert restored.model_dump() == result.model_dump()
