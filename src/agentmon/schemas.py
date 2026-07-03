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

    ``provenance`` is set only on a composite monitor's draw and records how its
    members combined; it defaults ``None`` so a leaf draw serializes exactly as
    before this field existed and pre-composite verdicts still deserialize.
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
    provenance: CompositeProvenance | None = None


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


class CompositeProvenance(BaseModel):
    """Why a composite monitor's draw looks the way it does.

    Stamped on the per-draw verdict a composite returns; leaf draws leave it
    ``None``. ``composite`` is the composite's id, ``driver`` the member whose
    draw was returned verbatim (the highest-scoring flagged member of an
    ensemble, or the gating member of a cascade), ``member_scores`` each
    participating member's suspicion score keyed by member name, and
    ``escalated`` whether a cascade reached its deep member (always ``False``
    for an ensemble or a short-circuited cascade).

    ``member_supported`` is populated only by a verified ensemble: per *flagged*
    member name, whether that member's own verification upheld (``True``) or
    refuted (``False``) its flag. Unflagged (never-verified) members are absent.
    It defaults empty, so the plain ensemble and cascade serialize unchanged.

    ``member_flagged`` is the raw (pre-verification) flag per member that
    actually drew this sample. A short-circuited cascade records
    ``{triage_name: False}`` — the deep member never drew. For a composite
    member (cascade deep = verified ensemble) the entry is post-verification by
    construction; the raw inner flags live in ``inner``.

    ``member_verifications`` is the full per-member folded verification outcome
    (verified composites only; ``quote_match`` makes the mechanical/semantic
    flip decomposition legible per member). The usage tokens here are ALREADY
    folded into the draw's extras — ``total_input_tokens`` /
    ``total_output_tokens`` must never sum this field.

    ``inner`` is the returned member's own provenance when that member is
    itself a composite (a cascade over a verified ensemble). The cascade
    re-stamps the returned verdict; without this field it would destroy the
    inner ``member_flagged`` / ``member_supported`` / ``member_verifications``
    — collapsing "escalated but every deep flag was refuted" (catch-loss) into
    "escalated but no deep member ever raw-flagged" (a deep miss). Leaves and
    flat composites never set it.

    All fields beyond the first two default empty/``None``, so pre-composite
    verdicts deserialize and leaf verdicts (``provenance=None``) serialize
    byte-identically.
    """

    composite: str
    driver: str
    member_scores: dict[str, int] = Field(default_factory=dict)
    escalated: bool = False
    member_supported: dict[str, bool] = Field(default_factory=dict)
    member_flagged: dict[str, bool] = Field(default_factory=dict)
    member_verifications: dict[str, VerificationOutcome] = Field(default_factory=dict)
    inner: CompositeProvenance | None = None


# ``inner`` self-references CompositeProvenance, and MonitorVerdict references it
# forward (the class sits below VerificationOutcome so ``member_verifications``
# can be typed concretely) — resolve both deferred annotations now.
CompositeProvenance.model_rebuild()
MonitorVerdict.model_rebuild()


class StageOutcome(BaseModel):
    """One verification *stage's* contribution to the flip decision for one sample.

    A verification pipeline runs ordered stages and folds their votes into the
    single :class:`VerificationOutcome` recorded in ``verifications``. This model
    is the per-stage detail behind that fold.

    ``supported`` is the stage's vote on the flag: ``False`` refutes it (and ends
    the pipeline), ``True`` upholds it, ``None`` abstains — the stage had no
    opinion (e.g. a diagnostic-only stage, or a deterministic check that found
    nothing). ``terminal`` marks an uphold that ends the run (a "confirm"):
    refutation is already terminal by pipeline rule, so the field only changes
    behaviour when a stage deliberately emits ``supported=True, terminal=True``.
    No default stage ever sets it. ``quote_match`` carries the mechanical
    quote-grounding diagnostic when a stage computes one. The usage fields are
    zero for stages that make no model call.

    ``details`` holds structured stage evidence; keys are documented here, and
    anything richer forces a deliberate schema change. Current keys:
    ``statuses`` and ``resolved`` (the normalized-grounding stage's per-entry
    grounding statuses and resolved event indices).

    Recorded in ``CalibratedVerdict.stage_outcomes`` only when a non-default
    pipeline runs; the default two-stage pipeline leaves that field ``None`` so
    its verdicts stay byte-identical to the committed test matrix.
    """

    stage_id: str
    supported: bool | None = None
    quote_match: bool | None = None
    terminal: bool = False
    reasoning: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    raw_response: str = ""
    parse_error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class CalibratedVerdict(BaseModel):
    """Aggregate of k temperature-sampled verdicts for one (monitor, transcript).

    ``fraction_flagged`` is the fraction of samples that flagged at least one
    category — after the verification pass flipped unsupported flags, when
    enabled. ``calibrated_score`` adds a mean-raw-suspicion epsilon tiebreak
    (``fraction + mean_suspicion / 100 * 1e-3``); the epsilon is < 1e-3, far
    below the 1/k gap between fractions, so ranking within a fraction never
    crosses fractions. ``verifications`` is parallel to ``samples``; ``None``
    means verification was not run for that sample (unflagged, or disabled).

    ``stage_outcomes`` is the per-stage detail behind each sample's
    ``verifications`` entry, parallel to ``samples`` (``None`` per sample where
    verification was not run). It is ``None`` for the whole verdict under the
    default two-stage pipeline — recorded only when an opt-in pipeline runs — so
    the committed verdicts (written before the field existed) still deserialize
    and the default path stays byte-identical.
    """

    monitor_id: str
    transcript_id: str
    model: str
    k: int = Field(gt=0)
    temperature: float
    samples: list[MonitorVerdict]
    verifications: list[VerificationOutcome | None] = Field(default_factory=list)
    stage_outcomes: list[list[StageOutcome] | None] | None = None
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

    def _verification_usage(self, field: str) -> float:
        """Sum one usage field across the verification pass.

        Counts per-stage outcomes when an opt-in pipeline recorded them (so a
        multi-call pipeline is costed honestly, with no double count against the
        folded ``verifications`` entry); otherwise the folded outcomes, which the
        default pipeline populates exactly as before this field existed.
        """
        if self.stage_outcomes is not None:
            return sum(getattr(o, field) for outs in self.stage_outcomes if outs for o in outs)
        return sum(getattr(v, field) for v in self.verifications if v is not None)

    @property
    def total_input_tokens(self) -> int:
        verif = int(self._verification_usage("input_tokens"))
        return sum(s.input_tokens for s in self.samples) + verif + self.extra_input_tokens

    @property
    def total_output_tokens(self) -> int:
        verif = int(self._verification_usage("output_tokens"))
        return sum(s.output_tokens for s in self.samples) + verif + self.extra_output_tokens

    @property
    def total_latency_ms(self) -> float:
        verif = self._verification_usage("latency_ms")
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


class BandWindow(BaseModel):
    """A per-row anomaly band on the draw-level flag rate; bounds are inclusive.

    Derived from dev rates by the pinned blind-to-test rule (DECISIONS 39/42,
    :func:`agentmon.eval.bands.derive_band`). A rate outside the band signals a
    substrate or pipeline anomaly, not monitor quality.
    """

    lo: float
    hi: float


class FlipReport(BaseModel):
    """Report-only verification decomposition for one gated row (DECISIONS 35b/c).

    The folded fields count verification *calls* and flips with the mechanical
    (quote-mismatch) split. ``member_refutations`` is the verified-ensemble
    path: provenance-derived per-member refutes, with no mechanical split
    available (``member_supported`` is bool-only) — the two counts measure
    different things and are never blended.
    """

    verif_calls: int
    flips: int
    flip_rate: float
    mechanical: int
    mech_fraction: float
    member_refutations: int = 0


class GateResult(BaseModel):
    """The typed pass/halt artifact of one QC-gated row (it becomes summary.json).

    ``halt_reasons`` empty means the row passed; the exit-code contract lives in
    :func:`agentmon.eval.gate.enforce`, the reason strings in ``run_gate``.
    ``own_tp_n`` is surfaced so a mis-declared band-row ``modes`` (0 own-mode
    TPs on a failure-bearing slice) is visible, never a silent pass.
    """

    row_id: str
    substrate: str
    n: int
    k: int
    draws: int
    raw_flag_rate: float
    band: BandWindow
    halt_reasons: list[str]
    own_tp_n: int
    own_tp_catches_lost: int
    flip_report: FlipReport
    requests: int
    calls_accounted: int
    cost_usd: float | None
    preexisting_cache_hits: int

    @property
    def passed(self) -> bool:
        return not self.halt_reasons


class CalibratedRowSummary(BaseModel):
    """Per-monitor summary of a calibrated run — the summarize() dict keys, typed.

    ``failures_missed`` entries are ``(transcript_id, stratum)`` pairs;
    ``auroc_own`` is ``None`` for an all-modes row, where own-AUROC degenerates
    to ``auroc_all``.
    """

    n: int
    auroc_all: float | None
    auroc_own: float | None
    recall_at_0fp: float | None
    recall_at_5pct: float | None
    hard_negatives_flagged: list[str]
    benign_flagged: list[str]
    failures_missed: list[tuple[str, str]]
    own_failures_missed: list[tuple[str, str]]
    mean_fraction_by_cell: dict[str, float]
    parse_retries: int
    parse_repairs: int
    verification_flips: int
    mechanical_flips: int
