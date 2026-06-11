"""Monitor abstraction: prompt files, transcript rendering, and verdict parsing.

A :class:`Monitor` is a prompt template plus a :class:`MonitorConfig`, loaded
from a markdown file with YAML frontmatter. It renders a transcript into a
numbered plain-text view, asks an LLM for a verdict, and defensively parses
the response into a :class:`~agentmon.schemas.MonitorVerdict`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from agentmon.llm.client import LLMClient, LLMResponse
from agentmon.schemas import (
    AssistantMessage,
    Event,
    Evidence,
    FailureCategory,
    FileDiff,
    MonitorVerdict,
    ShellCommand,
    ToolCall,
    ToolResult,
    Transcript,
    UserMessage,
)

_EVENT_CHAR_CAP = 2000
_EVENT_TRUNCATION_MARKER = "...[truncated]"


class MonitorConfig(BaseModel):
    """Monitor settings parsed from a prompt file's YAML frontmatter."""

    id: str
    description: str = ""
    categories: list[FailureCategory] = Field(default_factory=list)
    model: str
    max_transcript_tokens: int = Field(default=16000, gt=0)


def _indent_continuation(entry: str) -> str:
    """Indent every line after the first so multi-line payloads stay readable."""
    first, _, rest = entry.partition("\n")
    if not rest:
        return first
    indented = "\n".join(f"    {line}" for line in rest.split("\n"))
    return f"{first}\n{indented}"


def _render_event(event: Event) -> str:
    """Render one event as a numbered entry monitors can cite by index."""
    if isinstance(event, UserMessage):
        body = f"USER: {event.text}"
    elif isinstance(event, AssistantMessage):
        body = f"ASSISTANT: {event.text}"
    elif isinstance(event, ToolCall):
        body = f"TOOL_CALL {event.tool}: {json.dumps(event.tool_input, sort_keys=True)}"
    elif isinstance(event, ToolResult):
        # Error markers lead the entry so per-event truncation can never eat them.
        flag = " [error]" if event.is_error else ""
        body = f"TOOL_RESULT ({event.tool or 'unknown'}){flag}: {event.content}"
    elif isinstance(event, ShellCommand):
        flag = " [error]" if event.is_error else ""
        body = f"SHELL{flag} $ {event.command} -> {event.output}"
    elif isinstance(event, FileDiff):
        flag = " [error]" if event.is_error else ""
        body = f"FILE_DIFF{flag} {event.path}: {event.diff}"
    else:  # OtherEvent is the only remaining kind in the Event union.
        body = f"OTHER: {event.description}"
    return _indent_continuation(f"[{event.index}] {body}")


def render_transcript(transcript: Transcript, max_chars: int = 64000) -> str:
    """Render a transcript as numbered plain-text entries, one per event.

    Each entry is capped at 2000 chars; the whole rendering is capped at
    ``max_chars``, with an explicit marker for any events dropped. Pure and
    deterministic: the same transcript always renders identically.
    """
    parts: list[str] = []
    used = 0
    truncated = False
    for event in transcript.events:
        entry = _render_event(event)
        if len(entry) > _EVENT_CHAR_CAP:
            entry = entry[:_EVENT_CHAR_CAP] + _EVENT_TRUNCATION_MARKER
        cost = len(entry) + (1 if parts else 0)  # +1 for the joining newline
        if used + cost > max_chars:
            truncated = True
            break
        parts.append(entry)
        used += cost
    if truncated:
        # Drop tail entries until the truncation marker itself fits the budget,
        # so the rendering never exceeds max_chars.
        while True:
            remaining = len(transcript.events) - len(parts)
            marker = f"[transcript truncated: {remaining} more events]"
            rendered = "\n".join([*parts, marker])
            if len(rendered) <= max_chars or not parts:
                return rendered
            parts.pop()
    return "\n".join(parts)


def _parse_json_object(text: str) -> dict[str, Any]:
    """Extract the verdict JSON object from ``text``.

    Decodes a candidate object at every ``{`` in the response, tolerating
    prose (and stray braces) before or after the payload, markdown fences,
    and preamble objects. The last object carrying ``suspicion_score`` wins;
    otherwise the first object found.
    """
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    pos = text.find("{")
    while pos != -1:
        try:
            obj, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            pos = text.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            candidates.append(obj)
        pos = text.find("{", end)
    scored = [c for c in candidates if "suspicion_score" in c]
    if scored:
        return scored[-1]
    if candidates:
        return candidates[0]
    raise ValueError("no JSON object found in response")


def _clamp_score(value: Any) -> int:
    """Round a numeric (or numeric-string) score and clamp it to [0, 100]."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise ValueError(f"suspicion_score must be a number, got {value!r}") from None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"suspicion_score must be a number, got {value!r}")
    return max(0, min(100, round(value)))


def _parse_categories(value: Any) -> list[FailureCategory]:
    """Keep valid FailureCategory values; silently drop everything else."""
    if not isinstance(value, list):
        return []
    categories: list[FailureCategory] = []
    for item in value:
        try:
            categories.append(FailureCategory(item))
        except (ValueError, TypeError):
            continue
    return categories


def _parse_evidence(value: Any) -> list[Evidence]:
    """Keep evidence entries that validate; silently drop the rest."""
    if not isinstance(value, list):
        return []
    evidence: list[Evidence] = []
    for item in value:
        try:
            evidence.append(Evidence.model_validate(item))
        except ValidationError:
            continue
    return evidence


class Monitor:
    """A single LLM judge: a prompt template plus its config."""

    def __init__(self, config: MonitorConfig, template: str) -> None:
        self.config = config
        self.template = template

    @classmethod
    def from_file(cls, path: Path) -> Monitor:
        """Load a monitor from a markdown file with YAML frontmatter.

        The file must start with a ``---`` line, contain frontmatter mapping
        to :class:`MonitorConfig` fields, then a closing ``---`` line followed
        by the markdown prompt body.
        """
        text = path.read_text(encoding="utf-8")
        lines = text.split("\n")
        if not lines or lines[0].strip() != "---":
            raise ValueError(f"{path}: expected YAML frontmatter opening '---' on line 1")
        end = None
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end = i
                break
        if end is None:
            raise ValueError(f"{path}: missing closing '---' for YAML frontmatter")
        try:
            data = yaml.safe_load("\n".join(lines[1:end]))
        except yaml.YAMLError as exc:
            raise ValueError(f"{path}: invalid YAML frontmatter: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{path}: frontmatter must be a YAML mapping")
        try:
            config = MonitorConfig.model_validate(data)
        except ValidationError as exc:
            raise ValueError(f"{path}: invalid monitor config: {exc}") from exc
        return cls(config=config, template="\n".join(lines[end + 1 :]))

    def build_prompt(self, transcript: Transcript) -> str:
        """Substitute the rendered transcript into the prompt template."""
        rendered = render_transcript(transcript, max_chars=self.config.max_transcript_tokens * 4)
        # str.replace, not str.format: prompt bodies contain literal JSON braces.
        return self.template.replace("{{transcript}}", rendered)

    def verdict_from_response(self, transcript_id: str, response: LLMResponse) -> MonitorVerdict:
        """Defensively parse a model response into a verdict.

        Any parse failure yields a score-0 verdict with ``parse_error`` set;
        the raw response text and usage metadata are always preserved.
        """
        meta: dict[str, Any] = {
            "monitor_id": self.config.id,
            "transcript_id": transcript_id,
            "model": response.model,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "latency_ms": response.latency_ms,
            "raw_response": response.text,
        }
        try:
            data = _parse_json_object(response.text)
            score = _clamp_score(data.get("suspicion_score", 0))
            categories = _parse_categories(data.get("categories"))
            evidence = _parse_evidence(data.get("evidence"))
            reasoning = data.get("reasoning", "")
            if not isinstance(reasoning, str):
                reasoning = str(reasoning)
        except Exception as exc:
            return MonitorVerdict(suspicion_score=0, parse_error=str(exc), **meta)
        return MonitorVerdict(
            suspicion_score=score,
            categories=categories,
            evidence=evidence,
            reasoning=reasoning,
            **meta,
        )

    def run(self, transcript: Transcript, client: LLMClient) -> MonitorVerdict:
        """Build the prompt, query the client, and parse the verdict."""
        prompt = self.build_prompt(transcript)
        response = client.complete(prompt, model=self.config.model, temperature=0.0)
        return self.verdict_from_response(transcript.id, response)
