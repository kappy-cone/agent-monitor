"""Builder for hand-crafted synthetic transcripts, used as test fixtures."""

from __future__ import annotations

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


class TranscriptBuilder:
    """Chainable builder producing a ``Transcript`` with ``source="synthetic"``."""

    def __init__(self, transcript_id: str, task: str | None = None) -> None:
        self._id = transcript_id
        self._task = task
        self._events: list[Event] = []

    def _add(self, event: Event) -> TranscriptBuilder:
        self._events.append(event)
        return self

    def user(self, text: str) -> TranscriptBuilder:
        return self._add(UserMessage(index=len(self._events), text=text))

    def assistant(self, text: str) -> TranscriptBuilder:
        return self._add(AssistantMessage(index=len(self._events), text=text))

    def tool_call(self, tool: str, **tool_input: Any) -> TranscriptBuilder:
        return self._add(ToolCall(index=len(self._events), tool=tool, tool_input=tool_input))

    def tool_result(
        self, content: str, tool: str | None = None, is_error: bool = False
    ) -> TranscriptBuilder:
        return self._add(
            ToolResult(index=len(self._events), tool=tool, content=content, is_error=is_error)
        )

    def shell(
        self,
        command: str,
        output: str = "",
        exit_code: int | None = None,
        is_error: bool = False,
    ) -> TranscriptBuilder:
        # exit_code defaults to None to match what claude_code ingestion produces.
        return self._add(
            ShellCommand(
                index=len(self._events),
                command=command,
                output=output,
                exit_code=exit_code,
                is_error=is_error,
            )
        )

    def file_diff(self, path: str, diff: str, is_error: bool = False) -> TranscriptBuilder:
        return self._add(FileDiff(index=len(self._events), path=path, diff=diff, is_error=is_error))

    def other(self, description: str) -> TranscriptBuilder:
        return self._add(OtherEvent(index=len(self._events), description=description))

    def build(self) -> Transcript:
        metadata: dict[str, Any] = {"task": self._task} if self._task is not None else {}
        return Transcript(
            id=self._id, source="synthetic", events=list(self._events), metadata=metadata
        )
