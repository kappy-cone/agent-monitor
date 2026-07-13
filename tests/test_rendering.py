"""Byte-level guards for the rendering extraction (architecture candidate 4, Phase 1).

The render format feeds every sample cache key: any byte drift in
``render_transcript`` output invalidates the entire ``.agentmon_cache`` and the
golden-master replay. These tests pin the format two ways:

1. A render-hash pin over a committed dev-split transcript, recorded from the
   pre-move code in ``monitors/base.py``.
2. A byte-equivalence census: every committed transcript must render
   identically under the moved code and a frozen local copy of the old
   algorithm, at the production budget and at awkward budgets that exercise
   the truncation-marker paths.

Mock-free and offline: rendering is pure, no client is ever constructed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agentmon.monitors import base as monitors_base
from agentmon.rendering import RenderBudget, render_event, render_transcript
from agentmon.schemas import (
    AssistantMessage,
    Event,
    FileDiff,
    ShellCommand,
    ToolCall,
    ToolResult,
    Transcript,
    UserMessage,
)

REPO = Path(__file__).resolve().parents[1]
TRANSCRIPTS = REPO / "datasets" / "synthetic" / "transcripts"
TRANSCRIPT_PATHS = sorted(TRANSCRIPTS.glob("*.json"))

#: The production budget (16000 tokens * 4 chars) plus non-multiple-of-4 budgets
#: small enough to force the global-cap and marker-fitting paths on real data.
CENSUS_BUDGETS = (64000, 50, 150, 300)

#: sha256 of ``render_transcript(dec-sub-05)`` at the default 64000-char budget,
#: computed from the pre-move code in monitors/base.py. dec-sub-05 is dev-split
#: and carries a >2000-char event, so the pin covers the per-event cap too.
RENDER_HASH_PIN = "35c57a81004f9295f635d96fb0919683351d82e911135ceca1b512f711f6027d"
PINNED_TRANSCRIPT = TRANSCRIPTS / "dec-sub-05.json"


def _load(path: Path) -> Transcript:
    return Transcript.model_validate_json(path.read_text(encoding="utf-8"))


# --- frozen copy of the pre-move algorithm (monitors/base.py before the extraction) ---

_LEGACY_EVENT_CHAR_CAP = 2000
_LEGACY_EVENT_TRUNCATION_MARKER = "...[truncated]"


def _legacy_indent_continuation(entry: str) -> str:
    first, _, rest = entry.partition("\n")
    if not rest:
        return first
    indented = "\n".join(f"    {line}" for line in rest.split("\n"))
    return f"{first}\n{indented}"


def _legacy_render_event(event: Event) -> str:
    if isinstance(event, UserMessage):
        body = f"USER: {event.text}"
    elif isinstance(event, AssistantMessage):
        body = f"ASSISTANT: {event.text}"
    elif isinstance(event, ToolCall):
        body = f"TOOL_CALL {event.tool}: {json.dumps(event.tool_input, sort_keys=True)}"
    elif isinstance(event, ToolResult):
        flag = " [error]" if event.is_error else ""
        body = f"TOOL_RESULT ({event.tool or 'unknown'}){flag}: {event.content}"
    elif isinstance(event, ShellCommand):
        flag = " [error]" if event.is_error else ""
        body = f"SHELL{flag} $ {event.command} -> {event.output}"
    elif isinstance(event, FileDiff):
        flag = " [error]" if event.is_error else ""
        body = f"FILE_DIFF{flag} {event.path}: {event.diff}"
    else:
        body = f"OTHER: {event.description}"
    return _legacy_indent_continuation(f"[{event.index}] {body}")


def _legacy_render_transcript(transcript: Transcript, max_chars: int = 64000) -> str:
    parts: list[str] = []
    used = 0
    truncated = False
    for event in transcript.events:
        entry = _legacy_render_event(event)
        if len(entry) > _LEGACY_EVENT_CHAR_CAP:
            entry = entry[:_LEGACY_EVENT_CHAR_CAP] + _LEGACY_EVENT_TRUNCATION_MARKER
        cost = len(entry) + (1 if parts else 0)
        if used + cost > max_chars:
            truncated = True
            break
        parts.append(entry)
        used += cost
    if truncated:
        while True:
            remaining = len(transcript.events) - len(parts)
            marker = f"[transcript truncated: {remaining} more events]"
            rendered = "\n".join([*parts, marker])
            if len(rendered) <= max_chars or not parts:
                return rendered
            parts.pop()
    return "\n".join(parts)


# --- the render-hash pin ---


def test_render_bytes_pinned() -> None:
    """The moved renderer reproduces the pre-move bytes on a committed transcript."""
    rendered = render_transcript(_load(PINNED_TRANSCRIPT))
    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest() == RENDER_HASH_PIN


# --- the byte-equivalence census ---


def test_census_covers_all_committed_transcripts() -> None:
    assert len(TRANSCRIPT_PATHS) == 157  # 155 + 2 indirect_injection (design 08)


@pytest.mark.parametrize("path", TRANSCRIPT_PATHS, ids=lambda p: p.stem)
def test_census_matches_legacy_renderer(path: Path) -> None:
    """Every committed transcript renders byte-identically to the old algorithm."""
    transcript = _load(path)
    for event in transcript.events:
        assert render_event(event) == _legacy_render_event(event), (path.stem, event.index)
    for budget in CENSUS_BUDGETS:
        assert render_transcript(transcript, max_chars=budget) == _legacy_render_transcript(
            transcript, budget
        ), (path.stem, budget)


# --- truncation-marker edge paths ---


def test_oversized_single_event_matches_legacy_at_every_budget() -> None:
    """One 5000-char event: the per-event cap, then the marker paths at tiny budgets."""
    transcript = Transcript(
        id="t-oversize",
        source="synthetic",
        events=[UserMessage(index=0, text="x" * 5000)],
    )
    for budget in (*CENSUS_BUDGETS, 10):
        assert render_transcript(transcript, max_chars=budget) == _legacy_render_transcript(
            transcript, budget
        ), budget
    rendered = render_transcript(transcript)
    assert rendered.endswith("...[truncated]")


def test_bare_marker_when_no_event_fits() -> None:
    """All events popped: the bare marker returns even when it overshoots the budget."""
    transcript = Transcript(
        id="t-bare",
        source="synthetic",
        events=[UserMessage(index=i, text="y" * 100) for i in range(3)],
    )
    rendered = render_transcript(transcript, max_chars=10)
    assert rendered == "[transcript truncated: 3 more events]"
    assert rendered == _legacy_render_transcript(transcript, 10)


def test_marker_fit_pops_tail_entries_like_legacy() -> None:
    """A budget where entries fit but the marker forces the pop loop."""
    events = [UserMessage(index=i, text=f"message {i} " + "y" * 100) for i in range(10)]
    transcript = Transcript(id="t-pop", source="synthetic", events=events)
    # Each entry is 120 chars; at 245 two entries fit but entries+marker do not,
    # so the tail entry pops until the marker fits.
    rendered = render_transcript(transcript, max_chars=245)
    assert rendered == _legacy_render_transcript(transcript, 245)
    lines = rendered.split("\n")
    assert lines[-1] == "[transcript truncated: 9 more events]"
    assert len(rendered) <= 245


# --- RenderBudget ---


def test_render_budget_legacy_is_the_historical_heuristic() -> None:
    """legacy(max_tokens).max_chars is exactly tokens*4 — the cache-key-preserving path."""
    assert RenderBudget.legacy(16000).max_chars == 64000
    assert RenderBudget.legacy(8192).max_chars == 32768
    assert RenderBudget.legacy(1).max_chars == 4


def test_render_budget_is_frozen() -> None:
    budget = RenderBudget.legacy(16000)
    with pytest.raises(AttributeError):
        budget.max_tokens = 1  # type: ignore[misc]


# --- re-exports ---


def test_monitors_base_reexports_the_renderer() -> None:
    """Existing importers (verification.py, scripts) keep working unchanged."""
    assert monitors_base.render_event is render_event
    assert monitors_base.render_transcript is render_transcript
