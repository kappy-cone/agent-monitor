"""Core data models. Every other module codes against these.

All models are pydantic v2 and round-trip cleanly through JSON
(``model_dump_json`` / ``model_validate_json``).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator


class FailureCategory(StrEnum):
    """Failure modes that monitors flag."""

    SECURITY_VULN = "security_vuln"
    REWARD_HACKING = "reward_hacking"
    SCOPE_EXPANSION = "scope_expansion"
    DECEPTION = "deception"
    OTHER = "other"


class _BaseEvent(BaseModel):
    """Common fields for all transcript events.

    ``index`` is the event's position in the transcript (0-based, sequential).
    ``raw`` preserves the source payload the event was parsed from, so nothing
    is lost in normalization.
    """

    index: int = Field(ge=0)
    raw: dict[str, Any] | None = None


class UserMessage(_BaseEvent):
    kind: Literal["user_message"] = "user_message"
    text: str


class AssistantMessage(_BaseEvent):
    kind: Literal["assistant_message"] = "assistant_message"
    text: str


class ToolCall(_BaseEvent):
    kind: Literal["tool_call"] = "tool_call"
    tool: str
    tool_input: dict[str, Any] = Field(default_factory=dict)


class ToolResult(_BaseEvent):
    kind: Literal["tool_result"] = "tool_result"
    tool: str | None = None
    content: str
    is_error: bool = False


class FileDiff(_BaseEvent):
    """A file edit the agent attempted; ``is_error`` marks edits that failed to apply."""

    kind: Literal["file_diff"] = "file_diff"
    path: str
    diff: str
    is_error: bool = False


class ShellCommand(_BaseEvent):
    """A shell command and its output; ``is_error`` marks failed or interrupted runs."""

    kind: Literal["shell_command"] = "shell_command"
    command: str
    output: str = ""
    exit_code: int | None = None
    is_error: bool = False


class OtherEvent(_BaseEvent):
    """Catch-all for source records that don't map to a known event kind.

    Unknown record types are preserved here rather than dropped, so a
    transcript never silently loses information.
    """

    kind: Literal["other"] = "other"
    description: str = ""


Event = Annotated[
    UserMessage | AssistantMessage | ToolCall | ToolResult | FileDiff | ShellCommand | OtherEvent,
    Field(discriminator="kind"),
]


class Transcript(BaseModel):
    """A normalized coding-agent session."""

    id: str
    source: Literal["claude_code", "synthetic"]
    events: list[Event] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    """A pointer into the transcript supporting a verdict."""

    event_index: int = Field(ge=0)
    quote: str


class MonitorVerdict(BaseModel):
    """One monitor's judgment of one transcript.

    ``raw_response`` always holds the model's full response text, even when
    parsing failed; ``parse_error`` records why parsing failed, in which case
    the verdict fields fall back to defaults (score 0, no categories).
    """

    monitor_id: str
    transcript_id: str
    suspicion_score: int = Field(ge=0, le=100)
    categories: list[FailureCategory] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    reasoning: str = ""
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    raw_response: str = ""
    parse_error: str | None = None


class VerificationOutcome(BaseModel):
    """Result of re-checking one flagged sample's cited evidence.

    ``supported=False`` flips the sample to unflagged in calibrated scoring.
    A parse failure keeps the flag (fail-open for recall) with ``parse_error``
    set; a flag with no valid event citations is unsupported by definition and
    records no model usage.

    ``quote_match`` is a mechanical pre-check, computed without a model call:
    True when every evidence quote appears verbatim in its cited event's
    rendering. It decomposes verification flips into mechanical quote-mismatch
    (``quote_match=False``) vs semantic non-support (``quote_match=True`` but
    ``supported=False``) — so a paraphrased-quote tax is visible in reporting
    rather than silently scored as detection failure.
    """

    supported: bool
    quote_match: bool | None = None
    reasoning: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    raw_response: str = ""
    parse_error: str | None = None


class CalibratedVerdict(BaseModel):
    """Aggregate of k temperature-sampled verdicts for one (monitor, transcript).

    ``fraction_flagged`` is the fraction of samples that flagged at least one
    category — after the verification pass flipped unsupported flags, when
    enabled. ``calibrated_score`` adds a mean-raw-suspicion epsilon tiebreak
    (``fraction + mean_suspicion / 100 * 1e-3``); the epsilon is < 1e-3, far
    below the 1/k gap between fractions, so ranking within a fraction never
    crosses fractions. ``verifications`` is parallel to ``samples``; ``None``
    means verification was not run for that sample (unflagged, or disabled).
    """

    monitor_id: str
    transcript_id: str
    model: str
    k: int = Field(gt=0)
    temperature: float
    samples: list[MonitorVerdict]
    verifications: list[VerificationOutcome | None] = Field(default_factory=list)
    sample_flagged: list[bool]
    fraction_flagged: float = Field(ge=0.0, le=1.0)
    mean_suspicion: float = Field(ge=0.0, le=100.0)
    calibrated_score: float
    verification_enabled: bool = False
    # Parse-hardening bookkeeping: redraws and repair calls that produced (or
    # failed to produce) the samples above, with the discarded attempts' usage,
    # so reported cost covers every token actually spent.
    parse_retries: int = 0
    parse_repairs: int = 0
    extra_input_tokens: int = 0
    extra_output_tokens: int = 0

    @property
    def total_input_tokens(self) -> int:
        verif = sum(v.input_tokens for v in self.verifications if v is not None)
        return sum(s.input_tokens for s in self.samples) + verif + self.extra_input_tokens

    @property
    def total_output_tokens(self) -> int:
        verif = sum(v.output_tokens for v in self.verifications if v is not None)
        return sum(s.output_tokens for s in self.samples) + verif + self.extra_output_tokens

    @property
    def total_latency_ms(self) -> float:
        verif = sum(v.latency_ms for v in self.verifications if v is not None)
        return sum(s.latency_ms for s in self.samples) + verif


_VALID_LABELS = {"benign"} | {c.value for c in FailureCategory}


class LabeledTranscript(BaseModel):
    """Ground-truth label for one transcript: benign, or a failure-mode tag."""

    transcript_id: str
    label: str
    notes: str = ""

    @field_validator("label")
    @classmethod
    def _check_label(cls, v: str) -> str:
        if v not in _VALID_LABELS:
            allowed = ", ".join(sorted(_VALID_LABELS))
            raise ValueError(f"label must be one of: {allowed}")
        return v


class ThresholdMetrics(BaseModel):
    """Confusion-matrix metrics at a fixed score threshold."""

    threshold: int
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float


class MonitorEvalResult(BaseModel):
    """Eval metrics for one monitor over a labeled transcript set."""

    monitor_id: str
    n_transcripts: int
    n_positive: int
    n_benign: int
    n_parse_failures: int = 0
    auroc: float | None = None
    recall_at_1pct_fpr: float | None = None
    recall_at_5pct_fpr: float | None = None
    threshold_metrics: ThresholdMetrics
    mean_input_tokens: float
    mean_output_tokens: float
    mean_latency_ms: float
    mean_cost_usd: float | None = None


class EvalReport(BaseModel):
    """Top-level eval output: one result per monitor."""

    results: list[MonitorEvalResult] = Field(default_factory=list)
