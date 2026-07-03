"""Tests for HeuristicMonitor — the zero-cost adapter at the composition seam.

Mock-first: the adapter makes no client call by construction, so every test
asserts zero client traffic. The headline pins are the sanctioned composition
(the heuristic as an OR-widener of an LLM triage gate, never ``members[0]``)
and the documented k-degeneracy: draws are identical across ``sample_index``,
so the heuristic's contribution to any flagged fraction is {0, 1}.
"""

from __future__ import annotations

from agentmon.calibration import DrawContext, DrawResult, run_calibrated
from agentmon.heuristic import MODEL_ID, MONITOR_ID, HeuristicMonitor, heuristic_verdict
from agentmon.llm.mock import MockLLMClient
from agentmon.monitors.base import MonitorConfig
from agentmon.monitors.composite import CascadeMonitor, EnsembleMonitor, Member
from agentmon.schemas import (
    FailureCategory,
    MonitorVerdict,
    ShellCommand,
    Transcript,
    UserMessage,
)

MODEL = "claude-test-1"


def scope_transcript() -> Transcript:
    """A transcript the heuristic flags (pip install -> scope_expansion)."""
    return Transcript(
        id="t-scope",
        source="synthetic",
        events=[
            UserMessage(index=0, text="add retry logic"),
            ShellCommand(index=1, command="pip install tenacity", output="ok", exit_code=0),
        ],
    )


def benign_transcript() -> Transcript:
    return Transcript(
        id="t-benign",
        source="synthetic",
        events=[
            UserMessage(index=0, text="rename the helper"),
            ShellCommand(index=1, command="pytest -q", output="12 passed", exit_code=0),
        ],
    )


def make_ctx(
    transcript: Transcript, *, sample_index: int = 0, client: MockLLMClient | None = None
) -> DrawContext:
    return DrawContext(
        transcript=transcript,
        client=client or MockLLMClient(),
        cache_dir=None,
        sample_index=sample_index,
        temperature=0.7,
        model=MODEL,
    )


class StubMonitor:
    """A leaf draw-evaluator with a canned verdict; counts calls."""

    def __init__(self, monitor_id: str, score: int, *, flagged: bool, reasoning: str = "") -> None:
        self.config = MonitorConfig(id=monitor_id, model=MODEL)
        self._verdict = MonitorVerdict(
            monitor_id=monitor_id,
            transcript_id="t",
            suspicion_score=score,
            categories=[FailureCategory.SECURITY_VULN] if flagged else [],
            reasoning=reasoning or monitor_id,
            model=MODEL,
            input_tokens=10,
            output_tokens=5,
        )
        self.calls = 0

    def evaluate_once(self, ctx: DrawContext) -> DrawResult:
        self.calls += 1
        return DrawResult(
            verdict=self._verdict,
            parse_retries=0,
            parse_repairs=0,
            extra_input_tokens=0,
            extra_output_tokens=0,
        )


class TestAdapter:
    def test_config_and_zero_cost_draw(self) -> None:
        monitor = HeuristicMonitor()
        assert monitor.config.id == MONITOR_ID
        assert monitor.config.model == MODEL_ID  # the pseudo-model, never a served name
        client = MockLLMClient()
        draw = monitor.evaluate_once(make_ctx(scope_transcript(), client=client))
        assert client.call_count == 0  # no client, no cache, no tokens
        assert draw.verdict.input_tokens == 0
        assert draw.verdict.output_tokens == 0
        assert draw.parse_retries == 0 and draw.parse_repairs == 0
        assert draw.extra_input_tokens == 0 and draw.extra_output_tokens == 0

    def test_draw_is_exactly_the_heuristic_verdict(self) -> None:
        transcript = scope_transcript()
        draw = HeuristicMonitor().evaluate_once(make_ctx(transcript))
        assert draw.verdict == heuristic_verdict(transcript)
        assert FailureCategory.SCOPE_EXPANSION in draw.verdict.categories


class TestKDegeneracy:
    def test_draws_identical_across_sample_index(self) -> None:
        # Documented k-degeneracy: the adapter ignores sample_index (and
        # temperature), so all k draws are the same verdict by design.
        monitor = HeuristicMonitor()
        transcript = scope_transcript()
        draws = [monitor.evaluate_once(make_ctx(transcript, sample_index=i)) for i in range(3)]
        assert all(d.verdict == draws[0].verdict for d in draws)

    def test_fraction_flagged_is_all_or_nothing_through_run_calibrated(self) -> None:
        # Through the k-loop the heuristic's fraction is {0, 1} — a future
        # reader must not misread composite fraction distributions as anomalous.
        client = MockLLMClient()
        flagged_cv, benign_cv = run_calibrated(
            [HeuristicMonitor()], [scope_transcript(), benign_transcript()], client, k=3
        )
        assert client.call_count == 0  # the whole run is client-free
        assert flagged_cv.fraction_flagged == 1.0
        assert flagged_cv.sample_flagged == [True, True, True]
        assert benign_cv.fraction_flagged == 0.0
        assert flagged_cv.total_input_tokens == 0
        assert flagged_cv.total_output_tokens == 0


class TestOrWidenedGate:
    """The sanctioned composition: the heuristic widens an LLM triage gate.
    ``Member(EnsembleMonitor(...))`` as a cascade's triage IS the widened gate —
    zero new combination code. The pseudo-model member is never ``members[0]``,
    so the gate's config.model stays a real served name.
    """

    def test_heuristic_flag_escalates_past_an_llm_miss(self) -> None:
        llm_gate = StubMonitor("generalist", 10, flagged=False, reasoning="missed")
        deep = StubMonitor("deep", 85, flagged=True, reasoning="deep catch")
        gate = EnsembleMonitor("gate[gen-or-heur]", [Member(llm_gate), Member(HeuristicMonitor())])
        assert gate.config.model == MODEL  # members[0] is the LLM, not "heuristic"
        cascade = CascadeMonitor("casc", Member(gate), Member(deep))
        out = cascade.evaluate_once(make_ctx(scope_transcript()))
        assert deep.calls == 1  # escalated on the heuristic's flag alone (OR)
        assert out.verdict.reasoning == "deep catch"
        prov = out.verdict.provenance
        assert prov is not None and prov.escalated
        assert prov.member_flagged == {"gate[gen-or-heur]": True, "deep": True}

    def test_llm_flag_escalates_when_the_heuristic_is_quiet(self) -> None:
        # The widener is an OR, not an AND: an LLM-only flag still escalates.
        llm_gate = StubMonitor("generalist", 70, flagged=True, reasoning="caught")
        deep = StubMonitor("deep", 85, flagged=True, reasoning="deep catch")
        gate = EnsembleMonitor("gate[gen-or-heur]", [Member(llm_gate), Member(HeuristicMonitor())])
        cascade = CascadeMonitor("casc", Member(gate), Member(deep))
        cascade.evaluate_once(make_ctx(benign_transcript()))
        assert deep.calls == 1

    def test_both_quiet_short_circuits_and_inner_keeps_the_gate_provenance(self) -> None:
        # Neither gate member flags -> no escalation; the cascade's re-stamp
        # carries the gate ensemble's own provenance through under `inner`, so
        # which gate member stayed quiet remains legible.
        llm_gate = StubMonitor("generalist", 10, flagged=False, reasoning="quiet")
        deep = StubMonitor("deep", 85, flagged=True, reasoning="never runs")
        gate = EnsembleMonitor("gate[gen-or-heur]", [Member(llm_gate), Member(HeuristicMonitor())])
        cascade = CascadeMonitor("casc", Member(gate), Member(deep))
        out = cascade.evaluate_once(make_ctx(benign_transcript()))
        assert deep.calls == 0
        prov = out.verdict.provenance
        assert prov is not None and not prov.escalated
        assert prov.member_flagged == {"gate[gen-or-heur]": False}
        assert prov.inner is not None  # the gate's own provenance survived
        assert prov.inner.composite == "gate[gen-or-heur]"
        assert prov.inner.member_flagged == {"generalist": False, "heuristic_baseline": False}
