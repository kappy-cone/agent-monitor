"""Tests for the Claude Code ingest pipeline and the synthetic transcript builder."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from agentmon.ingest.claude_code import ingest_path, parse_session_file
from agentmon.ingest.synthetic import TranscriptBuilder
from agentmon.schemas import (
    FileDiff,
    OtherEvent,
    ShellCommand,
    ToolCall,
    Transcript,
    UserMessage,
)

FIXTURE = Path(__file__).parent / "fixtures" / "claude_code_session.jsonl"


class TestFixtureSession:
    """Parse the scrubbed real session log and check exact event arithmetic."""

    def test_event_counts(self) -> None:
        transcript = parse_session_file(FIXTURE)
        counts = Counter(event.kind for event in transcript.events)
        # The fixture has 84 records: assistant 38, user 18, attachment 8,
        # ai-title 8, last-prompt 6, queue-operation 4, system 1, mode 1.
        # - user: 2 string-content prompts -> 2 user_message; the other 16
        #   records carry tool_result blocks (10 Bash, 6 Read).
        # - assistant blocks: 5 text -> assistant_message; 17 thinking -> other;
        #   16 tool_use = 10 Bash -> shell_command + 6 Read -> tool_call.
        # - the 10 Bash tool_results fold into their shell_command events;
        #   the 6 Read tool_results emit tool_result events.
        # - non-conversation records (8+8+6+4+1+1 = 28) -> other,
        #   so other = 17 thinking + 28 records = 45.
        # Total: 2 + 5 + 10 + 6 + 6 + 45 = 74.
        assert counts == {
            "user_message": 2,
            "assistant_message": 5,
            "shell_command": 10,
            "tool_call": 6,
            "tool_result": 6,
            "other": 45,
        }
        assert len(transcript.events) == 74

    def test_indices_sequential(self) -> None:
        transcript = parse_session_file(FIXTURE)
        assert [event.index for event in transcript.events] == list(range(74))

    def test_metadata(self) -> None:
        transcript = parse_session_file(FIXTURE)
        assert transcript.id == "claude_code_session"
        assert transcript.source == "claude_code"
        meta = transcript.metadata
        assert meta["session_id"] == "3264e65e-82d1-481d-8f44-7ffa768f884d"
        assert meta["cwd"] == "/Users/user/code/tinygrad"
        assert meta["git_branch"] == "master"
        assert meta["claude_code_version"] == "2.1.170"
        assert meta["task"].startswith("Session goal: my first tinygrad PR")
        assert len(meta["task"]) <= 500

    def test_shell_commands_have_output(self) -> None:
        transcript = parse_session_file(FIXTURE)
        shells = [e for e in transcript.events if isinstance(e, ShellCommand)]
        assert len(shells) == 10
        assert all(shell.exit_code is None for shell in shells)
        assert all(not shell.is_error for shell in shells)
        # 9 of the 10 Bash results carry structured stdout/stderr; the one
        # with empty structured output falls back to the block content.
        assert all(shell.output for shell in shells)
        assert sum(1 for s in shells if s.output == "(Bash completed with no output)") == 1

    def test_tool_calls_are_reads(self) -> None:
        transcript = parse_session_file(FIXTURE)
        calls = [e for e in transcript.events if isinstance(e, ToolCall)]
        assert {call.tool for call in calls} == {"Read"}

    def test_thinking_preserved_as_other(self) -> None:
        transcript = parse_session_file(FIXTURE)
        thinking = [
            e
            for e in transcript.events
            if isinstance(e, OtherEvent) and e.description.startswith("assistant thinking: ")
        ]
        assert len(thinking) == 17


def _write_jsonl(path: Path, records: list[dict | str]) -> None:
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _assistant_record(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


class TestHandcraftedSessions:
    def test_write_tool_use_becomes_file_diff(self, tmp_path: Path) -> None:
        record = _assistant_record(
            {
                "type": "tool_use",
                "id": "t1",
                "name": "Write",
                "input": {"file_path": "/tmp/new.py", "content": "line1\nline2"},
            }
        )
        path = tmp_path / "write.jsonl"
        _write_jsonl(path, [record])
        transcript = parse_session_file(path)
        assert len(transcript.events) == 1
        diff = transcript.events[0]
        assert isinstance(diff, FileDiff)
        assert diff.path == "/tmp/new.py"
        assert diff.diff == "+line1\n+line2"

    def test_edit_tool_use_becomes_file_diff(self, tmp_path: Path) -> None:
        record = _assistant_record(
            {
                "type": "tool_use",
                "id": "t1",
                "name": "Edit",
                "input": {
                    "file_path": "/tmp/old.py",
                    "old_string": "a = 1",
                    "new_string": "a = 2\nb = 3",
                },
            }
        )
        path = tmp_path / "edit.jsonl"
        _write_jsonl(path, [record])
        transcript = parse_session_file(path)
        diff = transcript.events[0]
        assert isinstance(diff, FileDiff)
        assert diff.diff == "-a = 1\n+a = 2\n+b = 3"

    def test_unknown_record_type_becomes_other(self, tmp_path: Path) -> None:
        path = tmp_path / "unknown.jsonl"
        _write_jsonl(path, [{"type": "wombat", "data": 42}])
        transcript = parse_session_file(path)
        event = transcript.events[0]
        assert isinstance(event, OtherEvent)
        assert event.description == "wombat record"
        assert event.raw == {"type": "wombat", "data": 42}

    def test_malformed_json_line_becomes_other(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.jsonl"
        _write_jsonl(path, [{"type": "user", "message": {"content": "hi"}}, "{not json"])
        transcript = parse_session_file(path)
        assert isinstance(transcript.events[0], UserMessage)
        event = transcript.events[1]
        assert isinstance(event, OtherEvent)
        assert event.description == "unparseable line"
        assert event.raw == {"line": "{not json"}

    def test_tool_use_without_result_does_not_crash(self, tmp_path: Path) -> None:
        record = _assistant_record(
            {"type": "tool_use", "id": "orphan", "name": "Bash", "input": {"command": "ls"}}
        )
        path = tmp_path / "orphan.jsonl"
        _write_jsonl(path, [record])
        transcript = parse_session_file(path)
        shell = transcript.events[0]
        assert isinstance(shell, ShellCommand)
        assert shell.command == "ls"
        assert shell.output == ""
        assert shell.exit_code is None

    def test_ingest_path_directory(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path / "b_second.jsonl", [{"type": "user", "message": {"content": "b"}}])
        _write_jsonl(tmp_path / "a_first.jsonl", [{"type": "user", "message": {"content": "a"}}])
        (tmp_path / "ignored.txt").write_text("not a session", encoding="utf-8")
        transcripts = ingest_path(tmp_path)
        assert [t.id for t in transcripts] == ["a_first", "b_second"]

    def test_ingest_path_single_file(self, tmp_path: Path) -> None:
        path = tmp_path / "solo.jsonl"
        _write_jsonl(path, [{"type": "user", "message": {"content": "hello"}}])
        transcripts = ingest_path(path)
        assert len(transcripts) == 1
        assert transcripts[0].id == "solo"


class TestOffHappyPath:
    """Malformed records and failure signals must survive normalization."""

    def test_notebook_edit_uses_notebook_path(self, tmp_path: Path) -> None:
        record = _assistant_record(
            {
                "type": "tool_use",
                "id": "t1",
                "name": "NotebookEdit",
                "input": {"notebook_path": "/nb/analysis.ipynb", "new_source": "print('hi')"},
            }
        )
        path = tmp_path / "nb.jsonl"
        _write_jsonl(path, [record])
        diff = parse_session_file(path).events[0]
        assert isinstance(diff, FileDiff)
        assert diff.path == "/nb/analysis.ipynb"

    def test_failed_edit_marks_file_diff_as_error(self, tmp_path: Path) -> None:
        records = [
            _assistant_record(
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Edit",
                    "input": {"file_path": "/app/main.py", "old_string": "a", "new_string": "b"},
                }
            ),
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "String to replace not found in file.",
                            "is_error": True,
                        }
                    ]
                },
            },
        ]
        path = tmp_path / "failed_edit.jsonl"
        _write_jsonl(path, records)
        events = parse_session_file(path).events
        assert len(events) == 1
        diff = events[0]
        assert isinstance(diff, FileDiff)
        assert diff.is_error is True

    def test_interrupted_bash_keeps_signal(self, tmp_path: Path) -> None:
        # Esc mid-command: structured stdout/stderr are empty, the block
        # content carries the interruption notice, and interrupted is set.
        records = [
            _assistant_record(
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "sleep 100"}}
            ),
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "[Request interrupted by user]",
                        }
                    ]
                },
                "toolUseResult": {"stdout": "", "stderr": "", "interrupted": True},
            },
        ]
        path = tmp_path / "interrupted.jsonl"
        _write_jsonl(path, records)
        shell = parse_session_file(path).events[0]
        assert isinstance(shell, ShellCommand)
        assert shell.output == "[Request interrupted by user]"
        assert shell.is_error is True

    def test_malformed_user_record_becomes_other(self, tmp_path: Path) -> None:
        path = tmp_path / "malformed.jsonl"
        _write_jsonl(path, [{"type": "user", "message": None}, {"type": "assistant"}])
        events = parse_session_file(path).events
        assert [e.kind for e in events] == ["other", "other"]
        assert isinstance(events[0], OtherEvent)
        assert events[0].description == "malformed user record"

    def test_non_dict_content_block_becomes_other(self, tmp_path: Path) -> None:
        path = tmp_path / "weird_block.jsonl"
        _write_jsonl(
            path,
            [
                {
                    "type": "user",
                    "message": {"content": ["bare string", {"type": "text", "text": "hi"}]},
                }
            ],
        )
        events = parse_session_file(path).events
        assert [e.kind for e in events] == ["other", "user_message"]
        assert events[0].raw == {"block": "bare string"}

    def test_meta_user_record_skipped_for_task(self, tmp_path: Path) -> None:
        records = [
            {"type": "user", "isMeta": True, "message": {"content": "Caveat: local commands..."}},
            {"type": "user", "message": {"content": "fix the login bug"}},
        ]
        path = tmp_path / "meta.jsonl"
        _write_jsonl(path, records)
        transcript = parse_session_file(path)
        assert [e.kind for e in transcript.events] == ["other", "user_message"]
        assert transcript.metadata["task"] == "fix the login bug"

    def test_non_dict_tool_input_does_not_crash(self, tmp_path: Path) -> None:
        records = [
            _assistant_record({"type": "tool_use", "id": "t1", "name": "Bash", "input": "rm -rf"}),
            _assistant_record({"type": "tool_use", "id": "t2", "name": "Grep", "input": None}),
            _assistant_record({"type": "text", "text": 42}),
        ]
        path = tmp_path / "bad_input.jsonl"
        _write_jsonl(path, records)
        events = parse_session_file(path).events
        assert [e.kind for e in events] == ["shell_command", "tool_call", "assistant_message"]


class TestTranscriptBuilder:
    def test_round_trip(self) -> None:
        transcript = (
            TranscriptBuilder("t1", task="fix the bug")
            .user("please fix it")
            .assistant("on it")
            .tool_call("Read", file_path="/tmp/a.py")
            .tool_result("contents", tool="Read")
            .shell("pytest", output="1 passed", exit_code=0)
            .file_diff("/tmp/a.py", "-old\n+new")
            .other("hook fired")
            .build()
        )
        assert transcript.source == "synthetic"
        assert transcript.metadata == {"task": "fix the bug"}
        assert [e.index for e in transcript.events] == list(range(7))
        restored = Transcript.model_validate_json(transcript.model_dump_json())
        assert restored == transcript

    def test_no_task_means_empty_metadata(self) -> None:
        transcript = TranscriptBuilder("t2").user("hi").build()
        assert transcript.metadata == {}
        assert len(transcript.events) == 1
