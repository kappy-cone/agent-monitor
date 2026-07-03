"""Rendering: the single source of truth for what a monitor saw.

Owns the render format, the per-event cap, the token-aware budget, and the
overflow policy. Everything downstream — prompt building, quote grounding,
verification rendering — consumes a :class:`MonitorView`; the entry format
(prefixes, numbering, continuation indent, caps, markers) is PRIVATE to this
module. Pure and deterministic: no I/O, no model calls — the same transcript
always renders identically, and every sample cache key hashes the bytes
produced here.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from agentmon.schemas import (
    AssistantMessage,
    Event,
    Evidence,
    FileDiff,
    ShellCommand,
    ToolCall,
    ToolResult,
    Transcript,
    UserMessage,
)

_EVENT_CHAR_CAP = 2000
_EVENT_TRUNCATION_MARKER = "...[truncated]"

_MIN_RELOCATE_CHARS = 16  # min normalized quote length to relocate a miscited quote
_MIN_SEGMENT_CHARS = 8  # min normalized length per ellipsis segment
_MIN_ELLIPSIS_ANCHOR_CHARS = 16  # at least one ellipsis segment must reach this floor

#: Head share of the budget under the bracket policy. Dev-derived (design 05 R1):
#: ground-truth evidence sits late (median position 0.85 of the transcript), so
#: the head keeps orientation context and the tail gets the remainder.
_BRACKET_HEAD_FRACTION = 0.30

OverflowPolicy = Literal["head", "bracket"]
"""Which events fill the render budget when a transcript does not fit.

``head`` is the legacy rule (keep the front, drop the tail); ``bracket`` keeps
the front and the back and elides the middle behind an index-ranged marker.
"""


def _indent_continuation(entry: str) -> str:
    """Indent every line after the first so multi-line payloads stay readable."""
    first, _, rest = entry.partition("\n")
    if not rest:
        return first
    indented = "\n".join(f"    {line}" for line in rest.split("\n"))
    return f"{first}\n{indented}"


def render_event(event: Event) -> str:
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


def _capped_entry(event: Event) -> tuple[str, bool, int]:
    """One event's view entry: (post-cap text, cap fired, pre-cap length)."""
    entry = render_event(event)
    full_chars = len(entry)
    if full_chars > _EVENT_CHAR_CAP:
        return entry[:_EVENT_CHAR_CAP] + _EVENT_TRUNCATION_MARKER, True, full_chars
    return entry, False, full_chars


@dataclass(frozen=True)
class ViewEvent:
    """One event exactly as it appears in the monitor's prompt."""

    index: int
    text: str  # post per-event cap, post continuation indent — the real bytes
    truncated: bool  # the per-event cap fired
    full_chars: int  # entry length before the cap


@dataclass(frozen=True)
class MonitorView:
    """What one monitor saw for one transcript. Pure and deterministic.

    An event is either fully present in ``events`` (in transcript order,
    citable by index) or covered by an ``elided`` range — absent but accounted
    for, never silently dropped. ``text`` is the exact string substituted into
    the ``{{transcript}}`` placeholder.
    """

    transcript_id: str
    events: tuple[ViewEvent, ...]  # in transcript order
    elided: tuple[tuple[int, int], ...]  # inclusive (first, last) index ranges dropped
    text: str  # the exact string substituted into {{transcript}}

    def event_text(self, index: int) -> str | None:
        """The view bytes for one cited event; None if elided or absent."""
        for event in self.events:
            if event.index == index:
                return event.text
        return None


@dataclass(frozen=True)
class RenderBudget:
    """A transcript budget in token space, converted to chars by a calibrated ratio."""

    max_tokens: int
    chars_per_token: float = 4.0  # legacy value; calibrated budgets override
    margin: float = 1.0  # multiplicative safety factor, applied to chars
    reserved_tokens: int = 0  # template + fixed prompt overhead

    @property
    def max_chars(self) -> int:
        return int((self.max_tokens - self.reserved_tokens) * self.chars_per_token * self.margin)

    @classmethod
    def legacy(cls, max_tokens: int) -> RenderBudget:
        """Byte-identical to the historical ``max_transcript_tokens * 4`` heuristic."""
        return cls(max_tokens=max_tokens)  # 4.0 * 1.0, reserved 0  ->  exactly tokens*4

    @classmethod
    def calibrated(cls, max_tokens: int, model: str, template_chars: int = 0) -> RenderBudget:
        """A budget derived from a substrate-calibrated chars-per-token ratio.

        Reserves room for the fixed prompt template and applies a 0.95 safety
        margin so the estimate errs toward fitting the served context.
        """
        cpt = _CALIBRATED_CPT.get(model, _CALIBRATED_CPT[""])
        reserved = math.ceil(template_chars / cpt)
        return cls(
            max_tokens=max_tokens, chars_per_token=cpt, margin=0.95, reserved_tokens=reserved
        )


#: Pinned by scripts/calibrate_render_budget.py (results/dev-render-calibration/):
#: p05 of observed len(build_prompt(t)) / server-reported input_tokens over the
#: 990 committed sample records per substrate, rounded DOWN to 0.1, floored at
#: 3.0 — the rule was recorded in the script before the numbers were read.
#: Measured: qwen p05 3.0429 -> 3.0; gemma p05 2.8400 -> the 3.0 floor binds
#: (its report-only retrodiction coverage is 0.78, recorded, never gated). The
#: "" fallback is the min of the per-substrate pins (the conservative side: a
#: smaller ratio overestimates tokens).
_CALIBRATED_CPT: dict[str, float] = {
    "agentnom-local": 3.0,  # Qwen-3.6-35B (the served-name typo is canonical)
    "agentmon-local-gemma": 3.0,  # Gemma-4-31B (p05 2.84; the 3.0 floor binds)
    "": 3.0,  # conservative default for unknown models
}


def estimate_tokens(text: str, chars_per_token: float = _CALIBRATED_CPT[""]) -> int:
    """Estimated token count for ``text`` under a chars-per-token ratio."""
    return math.ceil(len(text) / chars_per_token)


def preflight_fits(
    prompt: str,
    served_context: int,
    *,
    output_reserve: int = 2048,
    chars_per_token: float = _CALIBRATED_CPT[""],
) -> bool:
    """True when the prompt's estimated tokens fit the served context with output room."""
    return estimate_tokens(prompt, chars_per_token) + output_reserve <= served_context


def build_view(
    transcript: Transcript,
    budget: RenderBudget,
    policy: OverflowPolicy = "head",
) -> MonitorView:
    """Build the view of a transcript one monitor actually sees.

    Under ``head`` the output text byte-equals the legacy ``render_transcript``
    for every budget — ``render_transcript`` delegates here, so the rendering
    census guards this path. Under ``bracket`` the head keeps the front of the
    transcript, the tail is filled backwards from the end, and the middle is
    elided behind ONE contiguous index-ranged marker.
    """
    if policy == "head":
        return _head_view(transcript, budget.max_chars)
    return _bracket_view(transcript, budget.max_chars)


def _head_view(transcript: Transcript, max_chars: int) -> MonitorView:
    """Legacy head policy: keep the front, drop whole events from the tail."""
    view_events: list[ViewEvent] = []
    parts: list[str] = []
    used = 0
    truncated = False
    for event in transcript.events:
        entry, was_capped, full_chars = _capped_entry(event)
        cost = len(entry) + (1 if parts else 0)  # +1 for the joining newline
        if used + cost > max_chars:
            truncated = True
            break
        parts.append(entry)
        view_events.append(
            ViewEvent(index=event.index, text=entry, truncated=was_capped, full_chars=full_chars)
        )
        used += cost
    if not truncated:
        return MonitorView(
            transcript_id=transcript.id,
            events=tuple(view_events),
            elided=(),
            text="\n".join(parts),
        )
    # Drop tail entries until the truncation marker itself fits the budget, so
    # the rendering never exceeds max_chars. Degenerate escape (pinned legacy
    # behavior): when no entry fits at all, the bare marker returns even if it
    # overshoots the budget.
    while True:
        remaining = len(transcript.events) - len(parts)
        marker = f"[transcript truncated: {remaining} more events]"
        rendered = "\n".join([*parts, marker])
        if len(rendered) <= max_chars or not parts:
            break
        parts.pop()
        view_events.pop()
    dropped = transcript.events[len(parts) :]
    return MonitorView(
        transcript_id=transcript.id,
        events=tuple(view_events),
        elided=((dropped[0].index, dropped[-1].index),),
        text=rendered,
    )


def _elision_marker(first_index: int, last_index: int, count: int, chars: int) -> str:
    """The bracket policy's gap marker: names the exact index range it hides."""
    return f"[events {first_index}..{last_index} elided: {count} events, {chars} chars]"


def _bracket_view(transcript: Transcript, max_chars: int) -> MonitorView:
    """Bracket policy: keep the front and the back, elide the middle.

    Head fills to ``_BRACKET_HEAD_FRACTION`` of the budget; the tail fills
    backwards with the remainder minus the marker allowance. Exactly one
    contiguous range is elided, and the marker names it, so a monitor knows
    precisely what it cannot see. Degenerate escape (mirrors the head policy):
    when not even the bare marker fits, the marker returns anyway.
    """
    events = transcript.events
    capped = [_capped_entry(event) for event in events]
    texts = [entry for entry, _, _ in capped]
    full_text = "\n".join(texts)
    if len(full_text) <= max_chars:
        view_events = tuple(
            ViewEvent(index=event.index, text=entry, truncated=was_capped, full_chars=full_chars)
            for event, (entry, was_capped, full_chars) in zip(events, capped, strict=True)
        )
        return MonitorView(
            transcript_id=transcript.id, events=view_events, elided=(), text=full_text
        )

    n = len(events)
    prefix = [0]
    for entry in texts:
        prefix.append(prefix[-1] + len(entry))

    def marker_for(head_count: int, tail_start: int) -> str:
        elided_chars = prefix[tail_start] - prefix[head_count]
        return _elision_marker(
            events[head_count].index,
            events[tail_start - 1].index,
            tail_start - head_count,
            elided_chars,
        )

    def assembled_len(head_count: int, tail_start: int) -> int:
        part_count = head_count + 1 + (n - tail_start)
        text_chars = prefix[head_count] + (prefix[n] - prefix[tail_start])
        return text_chars + len(marker_for(head_count, tail_start)) + (part_count - 1)

    # Head fill: whole entries from the front, up to the head share of the budget.
    # The transcript overflows, so head_count < n and at least one event elides.
    head_budget = int(max_chars * _BRACKET_HEAD_FRACTION)
    head_count = 0
    used = 0
    for entry in texts:
        cost = len(entry) + (1 if head_count else 0)
        if used + cost > head_budget:
            break
        used += cost
        head_count += 1
    # Shrink the head until head + full-elision marker fits the whole budget.
    while head_count and assembled_len(head_count, n) > max_chars:
        head_count -= 1
    # Tail fill, backwards from the end. Position head_count stays elided so the
    # marker never lies; the marker only shrinks as the elided range narrows, so
    # every accepted step keeps the assembly within budget.
    tail_start = n
    while tail_start - 1 > head_count and assembled_len(head_count, tail_start - 1) <= max_chars:
        tail_start -= 1

    view_events = tuple(
        ViewEvent(
            index=events[i].index,
            text=texts[i],
            truncated=capped[i][1],
            full_chars=capped[i][2],
        )
        for i in [*range(head_count), *range(tail_start, n)]
    )
    text = "\n".join([*texts[:head_count], marker_for(head_count, tail_start), *texts[tail_start:]])
    return MonitorView(
        transcript_id=transcript.id,
        events=view_events,
        elided=((events[head_count].index, events[tail_start - 1].index),),
        text=text,
    )


def render_transcript(transcript: Transcript, max_chars: int = 64000) -> str:
    """Render a transcript as numbered plain-text entries, one per event.

    Each entry is capped at 2000 chars; the whole rendering is capped at
    ``max_chars``, with an explicit marker for any events dropped. Delegates to
    :func:`build_view` under the legacy head policy — byte-identical for every
    ``max_chars``, so the rendering census guards the view path.
    """
    # chars_per_token=1.0 makes max_tokens count chars exactly: int(n * 1.0) == n.
    budget = RenderBudget(max_tokens=max_chars, chars_per_token=1.0)
    return build_view(transcript, budget, "head").text


# --- grounding: does the monitor's quoted evidence appear where it says it does? ---

GroundingStatus = Literal[
    "exact", "normalized", "ellipsis", "relocated", "elided", "unmatched", "no_such_event"
]
"""How one evidence quote resolved against the monitor's view.

``exact``/``normalized``/``ellipsis`` ground at the cited event, ``relocated``
grounds at another in-view event; ``elided`` (cited event exists but is not in
the view), ``unmatched``, and ``no_such_event`` never ground.
"""

#: The statuses that count as grounded — the quote's content is present in the view.
_GROUNDED_STATUSES = frozenset({"exact", "normalized", "ellipsis", "relocated"})

#: The statuses whose cited event is present in the view (renderable for a verifier).
_IN_VIEW_STATUSES = frozenset({"exact", "normalized", "ellipsis", "relocated", "unmatched"})

_ELLIPSIS_SPLIT = re.compile(r"\.\.\.|…")


def normalize_for_grounding(text: str) -> str:
    """The one normalization rule, applied to BOTH sides of every comparison.

    Whitespace-only: line endings unify to ``\\n``, then every whitespace run
    collapses to a single space and the edges strip. This erases the renderer's
    continuation indent, tab-vs-space differences, and monitor re-wrapping —
    and NOTHING else. Case, punctuation, digits, character order, and
    contiguity are sacred, so fabricated or paraphrased quotes still fail.
    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", unified).strip()


@dataclass(frozen=True)
class QuoteGrounding:
    """How one Evidence entry maps to the monitor's view."""

    status: GroundingStatus
    cited_index: int
    resolved_index: int | None  # == cited_index for exact/normalized/ellipsis;
    # the found event for relocated; None otherwise
    ambiguous: int = 0  # relocation: how many other events also matched

    @property
    def grounded(self) -> bool:
        return self.status in _GROUNDED_STATUSES


@dataclass(frozen=True)
class GroundingReport:
    """Every evidence entry's grounding for one verdict against one view."""

    entries: tuple[QuoteGrounding, ...]

    @property
    def all_grounded(self) -> bool:
        # Never vacuously true: an evidence-free flag is NOT grounded, matching
        # quotes_match's empty-evidence rule (verification.py).
        return bool(self.entries) and all(e.grounded for e in self.entries)

    @property
    def any_grounded(self) -> bool:
        return any(e.grounded for e in self.entries)

    @property
    def render_indices(self) -> tuple[int, ...]:
        """Deduplicated, sorted: every cited index present in the view
        (regardless of grounding status) plus every relocation target. This is
        what a verifier prompt renders; empty exactly when no citation resolves
        to any in-view event."""
        indices = {e.cited_index for e in self.entries if e.status in _IN_VIEW_STATUSES}
        indices.update(
            e.resolved_index
            for e in self.entries
            if e.status == "relocated" and e.resolved_index is not None
        )
        return tuple(sorted(indices))


def _ellipsis_segments(quote: str) -> list[str] | None:
    """The quote's normalized ellipsis segments, or None when it has no
    ellipsis or violates the length floors (each segment >= 8 normalized
    chars, at least one >= 16). Leading/trailing ellipses contribute empty
    segments, which constrain nothing and are dropped."""
    parts = _ELLIPSIS_SPLIT.split(quote)
    if len(parts) < 2:
        return None  # no ellipsis token: plain containment already decided this quote
    segments = [seg for seg in (normalize_for_grounding(part) for part in parts) if seg]
    if not segments or any(len(seg) < _MIN_SEGMENT_CHARS for seg in segments):
        return None
    if not any(len(seg) >= _MIN_ELLIPSIS_ANCHOR_CHARS for seg in segments):
        return None
    return segments


def _segments_in_order(segments: Sequence[str], surface: str) -> bool:
    """True when every segment appears in order, without overlap, in surface."""
    pos = 0
    for segment in segments:
        found = surface.find(segment, pos)
        if found < 0:
            return False
        pos = found + len(segment)
    return True


def _match_surface(quote: str, normalized_quote: str, surface: str) -> GroundingStatus | None:
    """Match one quote against one event's normalized view surface.

    ``normalized`` on containment of the whole normalized quote, ``ellipsis``
    when every '...'-separated segment appears in order within this SAME
    surface (no cross-event stitching, ever), None otherwise.
    """
    if normalized_quote and normalized_quote in surface:
        return "normalized"
    segments = _ellipsis_segments(quote)
    if segments is not None and _segments_in_order(segments, surface):
        return "ellipsis"
    return None


def _is_elided(view: MonitorView, index: int) -> bool:
    """True when the cited index falls inside one of the view's elided ranges."""
    return any(first <= index <= last for first, last in view.elided)


def _ground_entry(view: MonitorView, entry: Evidence) -> QuoteGrounding:
    """Resolve one evidence entry against the view — what the monitor saw."""
    cited = entry.event_index
    view_text = view.event_text(cited)
    if view_text is None:
        # Terminal either way: no relocation for citations the monitor could
        # not have read (elided) or that never existed (no_such_event).
        status: GroundingStatus = "elided" if _is_elided(view, cited) else "no_such_event"
        return QuoteGrounding(status=status, cited_index=cited, resolved_index=None)
    if entry.quote in view_text:
        # Byte-for-byte the legacy quotes_match test, kept for diagnostics parity.
        return QuoteGrounding(status="exact", cited_index=cited, resolved_index=cited)
    normalized_quote = normalize_for_grounding(entry.quote)
    matched = _match_surface(entry.quote, normalized_quote, normalize_for_grounding(view_text))
    if matched is not None:
        return QuoteGrounding(status=matched, cited_index=cited, resolved_index=cited)
    if len(normalized_quote) >= _MIN_RELOCATE_CHARS:
        # Miscitation repair: search the OTHER in-view events, nearest to the
        # cited index first, ties to the lower index — deterministic. Repairs
        # the pointer, never the vote.
        candidates = sorted(
            (event for event in view.events if event.index != cited),
            key=lambda event: (abs(event.index - cited), event.index),
        )
        hits = [
            event.index
            for event in candidates
            if _match_surface(entry.quote, normalized_quote, normalize_for_grounding(event.text))
        ]
        if hits:
            return QuoteGrounding(
                status="relocated",
                cited_index=cited,
                resolved_index=hits[0],
                ambiguous=len(hits) - 1,
            )
    return QuoteGrounding(status="unmatched", cited_index=cited, resolved_index=None)


def ground_evidence(view: MonitorView, evidence: Sequence[Evidence]) -> GroundingReport:
    """Ground every evidence quote against the view the monitor actually saw.

    Pure, deterministic, no model calls. View-true: a quote from beyond a
    capped event's head, or from an elided event, never grounds — the monitor
    never saw those bytes, so they cannot support its claim.
    """
    return GroundingReport(entries=tuple(_ground_entry(view, entry) for entry in evidence))
