"""Ingest Claude Code session logs (JSONL) into normalized ``Transcript`` objects.

A session log has one JSON record per line. Conversation records ("user",
"assistant") become message/tool events; everything else (attachments, system
notices, unknown future types, malformed records, even unparseable lines) is
preserved as an ``OtherEvent`` — nothing is dropped and nothing crashes the
parser.

Known limitation: subagent ("sidechain") records are parsed like main-thread
records; ``isSidechain`` survives only in each event's ``raw`` payload.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentmon.schemas import (
    AssistantMessage,
    Event,
    FileDiff,
    OtherEvent,
    ShellCommand,
    ToolCall,
    ToolResult,
    Transcript,
    UserMessage,
)

_FILE_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_TASK_MAX_CHARS = 500

# Transcript metadata key -> record-level key in the session log.
_METADATA_KEYS = {
    "session_id": "sessionId",
    "cwd": "cwd",
    "git_branch": "gitBranch",
    "claude_code_version": "version",
}


def parse_session_file(path: Path) -> Transcript:
    """Parse one Claude Code session JSONL file into a Transcript."""
    parsed: list[dict[str, Any] | str] = []  # dict = record, str = unparseable line
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            parsed.append(line)
            continue
        parsed.append(record if isinstance(record, dict) else line)

    records = [item for item in parsed if isinstance(item, dict)]
    tool_names = _collect_tool_names(records)
    results = _collect_tool_results(records)

    events: list[Event] = []
    for item in parsed:
        if isinstance(item, str):
            events.append(OtherEvent(index=0, description="unparseable line", raw={"line": item}))
        else:
            events.extend(_events_for_record(item, tool_names, results))
    for i, event in enumerate(events):
        event.index = i

    return Transcript(
        id=path.stem,
        source="claude_code",
        events=events,
        metadata=_build_metadata(records, events),
    )


def ingest_path(path: Path) -> list[Transcript]:
    """Ingest a session file, or every ``*.jsonl`` in a directory (sorted by name)."""
    if path.is_dir():
        return [parse_session_file(p) for p in sorted(path.glob("*.jsonl"))]
    return [parse_session_file(path)]


def _collect_tool_names(records: list[dict[str, Any]]) -> dict[str, str]:
    """Map tool_use_id -> tool name from assistant tool_use blocks."""
    names: dict[str, str] = {}
    for record in records:
        for block in _message_blocks(record, "assistant"):
            if block.get("type") == "tool_use" and "id" in block:
                names[block["id"]] = block.get("name", "")
    return names


def _collect_tool_results(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map tool_use_id -> its user-side tool_result block plus record-level data."""
    results: dict[str, dict[str, Any]] = {}
    for record in records:
        for block in _message_blocks(record, "user"):
            if block.get("type") == "tool_result" and "tool_use_id" in block:
                results[block["tool_use_id"]] = {
                    "block": block,
                    "tool_use_result": record.get("toolUseResult"),
                }
    return results


def _message_blocks(record: dict[str, Any], record_type: str) -> list[dict[str, Any]]:
    """List-form message content blocks of a record, or [] when not applicable."""
    if record.get("type") != record_type:
        return []
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _events_for_record(
    record: dict[str, Any],
    tool_names: dict[str, str],
    results: dict[str, dict[str, Any]],
) -> list[Event]:
    """Translate one parsed record into zero or more events (index fixed up later)."""
    record_type = record.get("type")
    if record_type == "user":
        return _user_events(record, tool_names)
    if record_type == "assistant":
        return _assistant_events(record, results)
    return [OtherEvent(index=0, description=f"{record_type} record", raw=record)]


def _content_or_fallback(record: dict[str, Any]) -> list[Any] | str | None:
    """A record's message content, or None when the record is malformed."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str | list):
        return content
    return None


def _user_events(record: dict[str, Any], tool_names: dict[str, str]) -> list[Event]:
    if record.get("isMeta"):
        # Caveat banners, slash-command expansions, local-command output: not human input.
        return [OtherEvent(index=0, description="meta user record", raw=record)]
    content = _content_or_fallback(record)
    if content is None:
        return [OtherEvent(index=0, description="malformed user record", raw=record)]
    if isinstance(content, str):
        return [UserMessage(index=0, text=content, raw=record)]

    events: list[Event] = []
    for block in content:
        if not isinstance(block, dict):
            events.append(
                OtherEvent(index=0, description="non-dict content block", raw={"block": block})
            )
            continue
        block_type = block.get("type")
        if block_type == "text":
            events.append(UserMessage(index=0, text=_as_text(block.get("text")), raw=block))
        elif block_type == "tool_result":
            tool = tool_names.get(block.get("tool_use_id", ""))
            if tool == "Bash" or tool in _FILE_EDIT_TOOLS:
                continue  # folded into the paired ShellCommand / FileDiff event
            events.append(
                ToolResult(
                    index=0,
                    tool=tool,
                    content=_stringify_content(block.get("content", "")),
                    is_error=bool(block.get("is_error", False)),
                    raw=block,
                )
            )
        else:
            events.append(OtherEvent(index=0, description=f"user {block_type} block", raw=block))
    return events


def _assistant_events(record: dict[str, Any], results: dict[str, dict[str, Any]]) -> list[Event]:
    content = _content_or_fallback(record)
    if content is None:
        return [OtherEvent(index=0, description="malformed assistant record", raw=record)]
    if isinstance(content, str):
        return [AssistantMessage(index=0, text=content, raw=record)]

    events: list[Event] = []
    for block in content:
        if not isinstance(block, dict):
            events.append(
                OtherEvent(index=0, description="non-dict content block", raw={"block": block})
            )
            continue
        block_type = block.get("type")
        if block_type == "text":
            events.append(AssistantMessage(index=0, text=_as_text(block.get("text")), raw=block))
        elif block_type == "thinking":
            description = "assistant thinking: " + _as_text(block.get("thinking"))
            events.append(OtherEvent(index=0, description=description, raw=block))
        elif block_type == "tool_use":
            events.append(_tool_use_event(block, results))
        else:
            events.append(
                OtherEvent(index=0, description=f"assistant {block_type} block", raw=block)
            )
    return events


def _as_text(value: Any) -> str:
    """Coerce a block's text payload to str without crashing on odd types."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _tool_use_event(block: dict[str, Any], results: dict[str, dict[str, Any]]) -> Event:
    name = block.get("name", "")
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):  # malformed input survives in raw
        tool_input = {}
    paired = results.get(block.get("id", ""))
    if name == "Bash":
        return ShellCommand(
            index=0,
            command=_as_text(tool_input.get("command")),
            output=_shell_output(paired),
            exit_code=None,  # the log format records no numeric exit code
            is_error=_result_is_error(paired),
            raw={"tool_use": block, "result": paired},
        )
    if name in _FILE_EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return FileDiff(
            index=0,
            path=_as_text(path),
            diff=_synthesize_diff(name, tool_input),
            is_error=_result_is_error(paired),
            raw={"tool_use": block, "result": paired},
        )
    return ToolCall(index=0, tool=name, tool_input=tool_input, raw=block)


def _result_is_error(paired: dict[str, Any] | None) -> bool:
    """True when the paired result reports an error or a user interruption."""
    if paired is None:
        return False
    if paired["block"].get("is_error"):
        return True
    structured = paired.get("tool_use_result")
    return isinstance(structured, dict) and bool(structured.get("interrupted"))


def _shell_output(paired: dict[str, Any] | None) -> str:
    """Bash output: structured toolUseResult stdout/stderr, else block content."""
    if paired is None:
        return ""
    structured = paired.get("tool_use_result")
    if isinstance(structured, dict) and ("stdout" in structured or "stderr" in structured):
        parts = [str(structured.get(key) or "") for key in ("stdout", "stderr")]
        joined = "\n".join(part for part in parts if part)
        if joined:
            return joined
        # Empty structured output: the block content may still carry the signal
        # (e.g. "[Request interrupted by user]").
    return _stringify_content(paired["block"].get("content", ""))


def _stringify_content(content: Any) -> str:
    """Flatten tool_result content (a string, or a list of text blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(json.dumps(block, default=str))
        return "\n".join(parts)
    return json.dumps(content, default=str)


def _synthesize_diff(name: str, tool_input: dict[str, Any]) -> str:
    """Build unified-style diff text from a file-edit tool's input."""
    if name == "Edit":
        return _edit_diff(tool_input)
    if name == "MultiEdit":
        edits = tool_input.get("edits") or []
        return "\n".join(_edit_diff(edit) for edit in edits if isinstance(edit, dict))
    # Write (and NotebookEdit's new_source): pure additions.
    content = tool_input.get("content", tool_input.get("new_source", ""))
    return _prefix_lines(content, "+")


def _edit_diff(edit: dict[str, Any]) -> str:
    old = _prefix_lines(edit.get("old_string", ""), "-")
    new = _prefix_lines(edit.get("new_string", ""), "+")
    return "\n".join(part for part in (old, new) if part)


def _prefix_lines(text: Any, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" for line in str(text).splitlines())


def _build_metadata(records: list[dict[str, Any]], events: list[Event]) -> dict[str, Any]:
    """Session metadata: each field from the first record carrying it, plus the task."""
    metadata: dict[str, Any] = {}
    for meta_key, record_key in _METADATA_KEYS.items():
        for record in records:
            value = record.get(record_key)
            if value is not None:
                metadata[meta_key] = value
                break
    first_prompt = next((e for e in events if isinstance(e, UserMessage)), None)
    if first_prompt is not None:
        metadata["task"] = first_prompt.text[:_TASK_MAX_CHARS]
    return metadata
