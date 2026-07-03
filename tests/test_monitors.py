"""Tests for the monitor abstraction: loading, rendering, prompting, parsing."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from agentmon.llm.client import LLMResponse
from agentmon.llm.mock import DEFAULT_VERDICT_JSON, MockLLMClient
from agentmon.monitors.base import Monitor, MonitorConfig, render_transcript
from agentmon.monitors.registry import DEFAULT_PROMPTS_DIR, get_monitor, load_monitors
from agentmon.schemas import (
    AssistantMessage,
    FailureCategory,
    FileDiff,
    OtherEvent,
    ShellCommand,
    ToolCall,
    ToolResult,
    Transcript,
    UserMessage,
)

EXAMPLE_PROMPT = DEFAULT_PROMPTS_DIR / "example_security.md"


def full_transcript() -> Transcript:
    """A transcript exercising every event kind."""
    return Transcript(
        id="t-full",
        source="synthetic",
        events=[
            UserMessage(index=0, text="add a login endpoint"),
            AssistantMessage(index=1, text="Sure, writing it now."),
            ToolCall(index=2, tool="Read", tool_input={"file_path": "app.py"}),
            ToolResult(index=3, tool="Read", content="def app(): ..."),
            ShellCommand(index=4, command="pytest -q", output="3 passed", exit_code=0),
            FileDiff(index=5, path="app.py", diff="+ secret = 'hunter2'"),
            OtherEvent(index=6, description="session compacted"),
        ],
    )


def make_monitor(template: str = "Review this:\n{{transcript}}\nRespond in JSON.") -> Monitor:
    return Monitor(config=MonitorConfig(id="m1", model="mock-model"), template=template)


def response_with(payload: dict[str, object]) -> LLMResponse:
    return LLMResponse(text=json.dumps(payload), model="mock-model")


# --- from_file / registry ---


def test_from_file_example_security() -> None:
    monitor = Monitor.from_file(EXAMPLE_PROMPT)
    assert monitor.config.id == "example_security"
    assert monitor.config.description
    assert monitor.config.categories == [FailureCategory.SECURITY_VULN]
    assert monitor.config.model == "claude-opus-4-8"
    assert monitor.config.max_transcript_tokens == 16000
    assert "{{transcript}}" in monitor.template


def test_from_file_malformed_frontmatter_raises_with_path(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("---\nid: [unclosed\n---\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"bad\.md"):
        Monitor.from_file(bad)


def test_from_file_missing_frontmatter_raises_with_path(tmp_path: Path) -> None:
    bad = tmp_path / "nofront.md"
    bad.write_text("just a body, no frontmatter\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"nofront\.md"):
        Monitor.from_file(bad)


def test_load_monitors_finds_example_security() -> None:
    monitors = load_monitors()
    assert "example_security" in monitors
    assert monitors["example_security"].config.model == "claude-opus-4-8"


def test_load_monitors_duplicate_ids_raise(tmp_path: Path) -> None:
    content = EXAMPLE_PROMPT.read_text(encoding="utf-8")
    (tmp_path / "a.md").write_text(content, encoding="utf-8")
    (tmp_path / "b.md").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_monitors(tmp_path)


def test_get_monitor_returns_monitor() -> None:
    monitor = get_monitor("example_security")
    assert monitor.config.id == "example_security"


def test_get_monitor_missing_id_lists_available() -> None:
    with pytest.raises(KeyError, match="example_security"):
        get_monitor("does_not_exist")


# --- render_transcript ---


def test_render_transcript_numbers_every_event_kind() -> None:
    lines = render_transcript(full_transcript()).split("\n")
    assert lines[0] == "[0] USER: add a login endpoint"
    assert lines[1] == "[1] ASSISTANT: Sure, writing it now."
    assert lines[2] == '[2] TOOL_CALL Read: {"file_path": "app.py"}'
    assert lines[3] == "[3] TOOL_RESULT (Read): def app(): ..."
    assert lines[4] == "[4] SHELL $ pytest -q -> 3 passed"
    assert lines[5] == "[5] FILE_DIFF app.py: + secret = 'hunter2'"
    assert lines[6] == "[6] OTHER: session compacted"


def test_render_transcript_marks_tool_result_errors() -> None:
    transcript = Transcript(
        id="t-err",
        source="synthetic",
        events=[ToolResult(index=0, tool="Bash", content="boom", is_error=True)],
    )
    assert render_transcript(transcript) == "[0] TOOL_RESULT (Bash) [error]: boom"


def test_render_transcript_indents_multiline_payloads() -> None:
    transcript = Transcript(
        id="t-multi",
        source="synthetic",
        events=[UserMessage(index=0, text="line one\nline two")],
    )
    assert render_transcript(transcript) == "[0] USER: line one\n    line two"


def test_render_transcript_caps_each_event_at_2000_chars() -> None:
    transcript = Transcript(
        id="t-big",
        source="synthetic",
        events=[UserMessage(index=0, text="x" * 5000)],
    )
    rendered = render_transcript(transcript)
    assert rendered.endswith("...[truncated]")
    assert len(rendered) == 2000 + len("...[truncated]")


def test_render_transcript_global_cap_reports_dropped_events() -> None:
    # Each entry is exactly 120 chars; with max_chars=300 only two fit.
    events = [UserMessage(index=i, text=f"message {i} " + "y" * 100) for i in range(10)]
    transcript = Transcript(id="t-cap", source="synthetic", events=events)
    rendered = render_transcript(transcript, max_chars=300)
    lines = rendered.split("\n")
    assert lines[0].startswith("[0] USER:")
    assert lines[1].startswith("[1] USER:")
    assert lines[2] == "[transcript truncated: 8 more events]"
    assert len(lines) == 3


def test_render_transcript_is_deterministic() -> None:
    transcript = full_transcript()
    assert render_transcript(transcript) == render_transcript(transcript)


# --- build_prompt ---


def test_build_prompt_substitutes_rendered_transcript() -> None:
    prompt = make_monitor().build_prompt(full_transcript())
    assert "{{transcript}}" not in prompt
    assert "[0] USER: add a login endpoint" in prompt
    assert "[6] OTHER: session compacted" in prompt


def test_build_prompt_preserves_literal_json_braces() -> None:
    monitor = make_monitor(template='{"suspicion_score": 0}\n{{transcript}}')
    prompt = monitor.build_prompt(full_transcript())
    assert prompt.startswith('{"suspicion_score": 0}')


# --- run / verdict parsing ---


def test_run_with_mock_default_response() -> None:
    monitor = make_monitor()
    client = MockLLMClient()
    verdict = monitor.run(full_transcript(), client)
    assert verdict.monitor_id == "m1"
    assert verdict.transcript_id == "t-full"
    assert verdict.raw_response == DEFAULT_VERDICT_JSON
    assert verdict.suspicion_score == 10
    assert verdict.categories == []
    assert verdict.evidence[0].event_index == 0
    assert verdict.parse_error is None
    assert verdict.model == "mock-model"
    assert client.call_count == 1
    assert client.calls[0]["model"] == "mock-model"
    assert client.calls[0]["temperature"] == 0.0


def test_fence_wrapped_json_response_parses() -> None:
    payload = json.dumps(
        {
            "suspicion_score": 80,
            "categories": ["security_vuln"],
            "evidence": [{"event_index": 5, "quote": "+ secret = 'hunter2'"}],
            "reasoning": "Hardcoded credential added.",
        }
    )
    response = LLMResponse(text=f"```json\n{payload}\n```", model="mock-model")
    verdict = make_monitor().verdict_from_response("t1", response)
    assert verdict.parse_error is None
    assert verdict.suspicion_score == 80
    assert verdict.categories == [FailureCategory.SECURITY_VULN]
    assert verdict.evidence[0].event_index == 5
    assert verdict.reasoning == "Hardcoded credential added."


def test_garbage_response_sets_parse_error_and_keeps_raw() -> None:
    response = LLMResponse(text="I cannot help with that.", model="mock-model")
    verdict = make_monitor().verdict_from_response("t1", response)
    assert verdict.parse_error
    assert verdict.suspicion_score == 0
    assert verdict.categories == []
    assert verdict.evidence == []
    assert verdict.raw_response == "I cannot help with that."
    assert verdict.monitor_id == "m1"
    assert verdict.transcript_id == "t1"


def test_score_above_100_is_clamped() -> None:
    response = response_with(
        {"suspicion_score": 250, "categories": [], "evidence": [], "reasoning": ""}
    )
    verdict = make_monitor().verdict_from_response("t1", response)
    assert verdict.suspicion_score == 100
    assert verdict.parse_error is None


def test_float_score_is_rounded() -> None:
    response = response_with(
        {"suspicion_score": 49.6, "categories": [], "evidence": [], "reasoning": ""}
    )
    verdict = make_monitor().verdict_from_response("t1", response)
    assert verdict.suspicion_score == 50
    assert verdict.parse_error is None


def test_invalid_categories_and_evidence_are_dropped() -> None:
    response = response_with(
        {
            "suspicion_score": 40,
            "categories": ["security_vuln", "not_a_category"],
            "evidence": [
                {"event_index": 1, "quote": "ok"},
                {"quote": "missing event index"},
            ],
            "reasoning": "partial garbage",
        }
    )
    verdict = make_monitor().verdict_from_response("t1", response)
    assert verdict.parse_error is None
    assert verdict.categories == [FailureCategory.SECURITY_VULN]
    assert len(verdict.evidence) == 1
    assert verdict.evidence[0].event_index == 1


# --- regression: review findings on parsing and rendering ---


def test_parse_tolerates_prose_with_braces_around_json() -> None:
    text = (
        "Event 3 contains `if (x) { return; }` which looks fine.\n"
        '{"suspicion_score": 85, "categories": ["security_vuln"], '
        '"evidence": [], "reasoning": "hardcoded secret"}\n'
        "Overall: suspicious (see the {evidence} above)."
    )
    verdict = make_monitor().verdict_from_response("t1", LLMResponse(text=text, model="m"))
    assert verdict.parse_error is None
    assert verdict.suspicion_score == 85


def test_parse_prefers_object_with_suspicion_score() -> None:
    text = (
        '{"summary": "two findings"}\n'
        '{"suspicion_score": 60, "categories": [], "evidence": [], "reasoning": "r"}'
    )
    verdict = make_monitor().verdict_from_response("t1", LLMResponse(text=text, model="m"))
    assert verdict.parse_error is None
    assert verdict.suspicion_score == 60


def test_parse_coerces_string_score() -> None:
    verdict = make_monitor().verdict_from_response(
        "t1", response_with({"suspicion_score": "85", "reasoning": "r"})
    )
    assert verdict.parse_error is None
    assert verdict.suspicion_score == 85


def test_error_marker_survives_per_event_truncation() -> None:
    transcript = Transcript(
        id="t",
        source="synthetic",
        events=[
            ShellCommand(index=0, command="pytest", output="F" * 3000, is_error=True),
        ],
    )
    rendered = render_transcript(transcript)
    assert rendered.startswith("[0] SHELL [error] $ pytest")
    assert rendered.endswith("...[truncated]")


def test_render_never_exceeds_max_chars() -> None:
    transcript = Transcript(
        id="t",
        source="synthetic",
        events=[UserMessage(index=i, text="x" * 90) for i in range(10)],
    )
    for budget in (50, 100, 150, 400):
        assert len(render_transcript(transcript, max_chars=budget)) <= budget
    assert "[transcript truncated:" in render_transcript(transcript, max_chars=150)


def test_config_rejects_nonpositive_transcript_budget() -> None:
    with pytest.raises(ValueError, match="max_transcript_tokens"):
        MonitorConfig(id="m1", model="mock-model", max_transcript_tokens=0)


# --- monitor view: additive config fields, the view path, the truncation warning ---


def test_frozen_prompt_files_parse_with_legacy_view_defaults() -> None:
    """The committed prompt files carry neither new field: defaults keep them
    legacy (chars_per_token 4.0, head policy) and load raises no warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        monitors = load_monitors()
    assert len(monitors) >= 5
    for monitor in monitors.values():
        assert monitor.config.chars_per_token == 4.0
        assert monitor.config.overflow_policy == "head"


def test_config_new_fields_parse_from_frontmatter(tmp_path: Path) -> None:
    content = (
        "---\n"
        "id: dev_round\n"
        "model: mock-model\n"
        "max_transcript_tokens: 8000\n"
        "chars_per_token: 3.2\n"
        "overflow_policy: bracket\n"
        "---\n"
        "Review this:\n{{transcript}}\n"
    )
    path = tmp_path / "dev_round.md"
    path.write_text(content, encoding="utf-8")
    monitor = Monitor.from_file(path)
    assert monitor.config.chars_per_token == 3.2
    assert monitor.config.overflow_policy == "bracket"


def test_config_warns_when_tight_margin_meets_head_policy() -> None:
    """chars_per_token < 4.0 with the head policy is self-inflicted truncation."""
    with pytest.warns(UserWarning, match="bracket"):
        MonitorConfig(id="m1", model="mock-model", chars_per_token=3.2)


def test_config_no_warning_for_bracket_or_legacy_margin() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        MonitorConfig(id="m1", model="mock-model")
        MonitorConfig(id="m1", model="mock-model", chars_per_token=3.2, overflow_policy="bracket")
        MonitorConfig(id="m1", model="mock-model", chars_per_token=4.5)


def test_config_rejects_unknown_overflow_policy() -> None:
    with pytest.raises(ValueError, match="overflow_policy"):
        MonitorConfig(id="m1", model="mock-model", overflow_policy="middle")  # type: ignore[arg-type]


def test_monitor_view_default_is_the_legacy_render() -> None:
    monitor = make_monitor()
    transcript = full_transcript()
    view = monitor.view(transcript)
    assert view.text == render_transcript(transcript)
    assert view.elided == ()
    assert view.transcript_id == "t-full"


def test_build_prompt_goes_through_the_view() -> None:
    monitor = make_monitor()
    transcript = full_transcript()
    expected = monitor.template.replace("{{transcript}}", monitor.view(transcript).text)
    assert monitor.build_prompt(transcript) == expected


def test_build_prompt_with_bracket_policy_elides_the_middle() -> None:
    config = MonitorConfig(
        id="m-bracket", model="mock-model", max_transcript_tokens=100, overflow_policy="bracket"
    )
    monitor = Monitor(config=config, template="Review this:\n{{transcript}}\nRespond in JSON.")
    events = [UserMessage(index=i, text=f"message {i} " + "y" * 100) for i in range(10)]
    transcript = Transcript(id="t-elide", source="synthetic", events=events)
    prompt = monitor.build_prompt(transcript)
    assert " elided: " in prompt
    assert "[0] USER: message 0" in prompt
    assert "[9] USER: message 9" in prompt


def test_monitor_view_respects_chars_per_token() -> None:
    config = MonitorConfig(
        id="m-tight",
        model="mock-model",
        max_transcript_tokens=100,
        chars_per_token=3.0,
        overflow_policy="bracket",
    )
    monitor = Monitor(config=config, template="{{transcript}}")
    events = [UserMessage(index=i, text=f"message {i} " + "y" * 100) for i in range(10)]
    transcript = Transcript(id="t-tight", source="synthetic", events=events)
    view = monitor.view(transcript)
    assert len(view.text) <= 300  # 100 tokens * 3.0 chars/token
    assert view.elided
