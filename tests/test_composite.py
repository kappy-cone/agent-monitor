"""Tests for the monitor-composition seam (cascade + ensemble).

Mock-first, in the committed stage-test style: composition logic is exercised
through ``StubMonitor`` draw-evaluators (no model calls, no prompts), and the
"driven identically to a leaf" goal is pinned by running composites through
``run_calibrated``. The adversarial block guards the corners that quietly break
a composite — no member flags, score ties, empty members, and a model pin that
must route to its own cache namespace rather than silently borrowing the
context's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentmon.calibration import DrawContext, DrawResult, run_calibrated, sample_cache_key
from agentmon.llm.mock import MockLLMClient
from agentmon.monitors.base import Monitor, MonitorConfig
from agentmon.monitors.composite import (
    CascadeMonitor,
    CompositeMonitor,
    EnsembleMonitor,
    Member,
)
from agentmon.schemas import (
    Evidence,
    FailureCategory,
    MonitorVerdict,
    ShellCommand,
    Transcript,
    UserMessage,
)

MODEL = "claude-test-1"
TEMPLATE = "Judge this transcript:\n\n{{transcript}}\n\nRespond in JSON."


def make_transcript(transcript_id: str = "t1") -> Transcript:
    return Transcript(
        id=transcript_id,
        source="synthetic",
        events=[
            UserMessage(index=0, text="please run a command"),
            ShellCommand(index=1, command="rm -rf /tmp/scratch", output="ok", exit_code=0),
        ],
    )


def make_ctx(*, model: str = MODEL, sample_index: int = 0) -> DrawContext:
    return DrawContext(
        transcript=make_transcript(),
        client=MockLLMClient(),
        cache_dir=None,
        sample_index=sample_index,
        temperature=0.7,
        model=model,
    )


def _verdict(score: int, *, flagged: bool, reasoning: str) -> MonitorVerdict:
    return MonitorVerdict(
        monitor_id="stub",
        transcript_id="t1",
        suspicion_score=score,
        categories=[FailureCategory.SECURITY_VULN] if flagged else [],
        evidence=[Evidence(event_index=1, quote="rm -rf /tmp/scratch")] if flagged else [],
        reasoning=reasoning,
        model=MODEL,
        input_tokens=10,
        output_tokens=5,
    )


class StubMonitor:
    """A leaf draw-evaluator with a canned verdict; records calls and the model
    each draw saw, so member-model-pin routing is observable without a client.
    """

    def __init__(self, monitor_id: str, score: int, *, flagged: bool, reasoning: str = "") -> None:
        self.config = MonitorConfig(id=monitor_id, model=MODEL)
        self._verdict = _verdict(score, flagged=flagged, reasoning=reasoning or monitor_id)
        self.calls = 0
        self.seen_models: list[str] = []

    def evaluate_once(self, ctx: DrawContext) -> DrawResult:
        self.calls += 1
        self.seen_models.append(ctx.model)
        return DrawResult(
            verdict=self._verdict,
            parse_retries=0,
            parse_repairs=0,
            extra_input_tokens=0,
            extra_output_tokens=0,
        )


class TestEnsembleMonitor:
    def test_or_flag_max_score_and_driver_reasoning(self) -> None:
        # One member flags at 60, another scores 90 but does not flag: the
        # composite flags (OR), carries the max score (90), and returns the
        # flagged member's draw verbatim (its reasoning), not the higher scorer's.
        flagged = StubMonitor("security_vuln", 60, flagged=True, reasoning="real catch")
        louder = StubMonitor("deception", 90, flagged=False, reasoning="noise")
        ensemble = EnsembleMonitor("ens", [Member(flagged), Member(louder)])
        out = ensemble.evaluate_once(make_ctx())
        assert out.verdict.categories  # OR: flagged
        assert out.verdict.suspicion_score == 90  # max across members
        assert out.verdict.reasoning == "real catch"  # the driver's draw, verbatim
        prov = out.verdict.provenance
        assert prov is not None
        assert prov.driver == "security_vuln"
        assert prov.member_scores == {"security_vuln": 60, "deception": 90}
        assert prov.escalated is False

    def test_no_member_flags_picks_highest_scoring_member(self) -> None:
        # No flag anywhere -> composite is unflagged, but still returns a real
        # draw: the highest-scoring member, with the max score.
        low = StubMonitor("security_vuln", 15, flagged=False, reasoning="low")
        high = StubMonitor("deception", 40, flagged=False, reasoning="high")
        out = EnsembleMonitor("ens", [Member(low), Member(high)]).evaluate_once(make_ctx())
        assert not out.verdict.categories  # OR over zero flags -> unflagged
        assert out.verdict.suspicion_score == 40
        assert out.verdict.provenance is not None
        assert out.verdict.provenance.driver == "deception"

    def test_score_tie_resolves_to_the_earlier_member(self) -> None:
        # Two flagged members tied on score: the driver is deterministic — the
        # earlier member in declaration order, never order-dependent flake.
        first = StubMonitor("security_vuln", 70, flagged=True, reasoning="first")
        second = StubMonitor("reward_hacking", 70, flagged=True, reasoning="second")
        out = EnsembleMonitor("ens", [Member(first), Member(second)]).evaluate_once(make_ctx())
        assert out.verdict.provenance is not None
        assert out.verdict.provenance.driver == "security_vuln"
        assert out.verdict.reasoning == "first"

    def test_single_member_reproduces_the_leaf_draw(self) -> None:
        # A one-member ensemble is the reconciliation anchor: same flag, same
        # score, same reasoning as the member; only provenance is added.
        member = StubMonitor("security_vuln", 55, flagged=True, reasoning="leaf")
        out = EnsembleMonitor("ens", [Member(member)]).evaluate_once(make_ctx())
        assert out.verdict.suspicion_score == 55
        assert out.verdict.reasoning == "leaf"
        assert bool(out.verdict.categories) is True
        assert out.verdict.provenance is not None
        assert out.verdict.provenance.member_scores == {"security_vuln": 55}

    def test_louder_flagged_member_drives_over_a_quieter_catch(self) -> None:
        # Adversarial pin (the verification-collateral hazard): when two members
        # flag, the driver is the LOUDER one, so the returned draw — the only
        # one a downstream verification pass re-checks — carries the loud
        # member's reasoning. If that louder member is a false positive that
        # verification refutes, the quieter member's real catch (which rode in
        # only as the OR flag) is dropped with it. The ensemble cannot, by
        # itself, protect a quiet catch behind a loud one; per-member
        # verification is a live-box step (each member's draw is cached).
        loud_fp = StubMonitor("reward_hacking", 80, flagged=True, reasoning="loud false positive")
        quiet_catch = StubMonitor("security_vuln", 40, flagged=True, reasoning="quiet real catch")
        out = EnsembleMonitor("ens", [Member(loud_fp), Member(quiet_catch)]).evaluate_once(
            make_ctx()
        )
        assert out.verdict.categories  # OR flag holds...
        assert out.verdict.reasoning == "loud false positive"  # ...but the driver is the loud FP
        assert out.verdict.provenance is not None
        assert out.verdict.provenance.driver == "reward_hacking"
        # Both members' draws survive in provenance, so the box can recover the
        # quiet catch by verifying members independently rather than the driver.
        assert out.verdict.provenance.member_scores == {"reward_hacking": 80, "security_vuln": 40}


class TestCascadeMonitor:
    def test_triage_no_flag_short_circuits_and_skips_deep(self) -> None:
        triage = StubMonitor("triage", 20, flagged=False, reasoning="quiet")
        deep = StubMonitor("deep", 80, flagged=True, reasoning="deep")
        out = CascadeMonitor("casc", Member(triage), Member(deep)).evaluate_once(make_ctx())
        assert deep.calls == 0  # the deep member never ran
        assert not out.verdict.categories  # triage's unflagged draw is returned
        prov = out.verdict.provenance
        assert prov is not None
        assert prov.escalated is False
        assert prov.driver == "triage"
        assert prov.member_scores == {"triage": 20}  # deep absent: it was skipped

    def test_triage_flag_escalates_and_returns_deep(self) -> None:
        triage = StubMonitor("triage", 70, flagged=True, reasoning="triage")
        deep = StubMonitor("deep", 45, flagged=True, reasoning="deep verdict")
        out = CascadeMonitor("casc", Member(triage), Member(deep)).evaluate_once(make_ctx())
        assert deep.calls == 1
        assert out.verdict.reasoning == "deep verdict"  # deep's draw is returned
        assert out.verdict.suspicion_score == 45  # deep's own score, not max
        prov = out.verdict.provenance
        assert prov is not None
        assert prov.escalated is True
        assert prov.driver == "deep"
        assert prov.member_scores == {"triage": 70, "deep": 45}

    def test_recall_bounded_by_triage(self) -> None:
        # Triage misses a genuine failure the deep member would have caught: the
        # cascade cannot recover it, because deep never runs. This is the cost of
        # the GPU-time saving, made explicit.
        triage = StubMonitor("triage", 10, flagged=False, reasoning="missed it")
        deep = StubMonitor("deep", 95, flagged=True, reasoning="would have caught")
        out = CascadeMonitor("casc", Member(triage), Member(deep)).evaluate_once(make_ctx())
        assert deep.calls == 0
        assert not out.verdict.categories  # the catch is lost to triage's miss


class TestMemberModelPin:
    def test_pin_routes_member_to_its_served_name(self) -> None:
        # A pinned member's draw sees the override model; an unpinned member sees
        # the context model. The two reach distinct cache namespaces by design.
        pinned = StubMonitor("security_vuln", 50, flagged=True)
        plain = StubMonitor("security_vuln", 50, flagged=True)
        ensemble = EnsembleMonitor(
            "xsub",
            [Member(pinned, model_override="served-gemma"), Member(plain)],
        )
        ensemble.evaluate_once(make_ctx(model="served-qwen"))
        assert pinned.seen_models == ["served-gemma"]  # the pin won
        assert plain.seen_models == ["served-qwen"]  # the context model

    def test_pin_disambiguates_members_in_provenance(self) -> None:
        # The same monitor pinned to two substrates must not collide in the
        # member-score map — the name carries the served model.
        qwen = StubMonitor("security_vuln", 30, flagged=False)
        gemma = StubMonitor("security_vuln", 80, flagged=True)
        ensemble = EnsembleMonitor(
            "sec-qwen+gemma",
            [
                Member(qwen, model_override="served-qwen"),
                Member(gemma, model_override="served-gemma"),
            ],
        )
        out = ensemble.evaluate_once(make_ctx())
        assert out.verdict.provenance is not None
        assert out.verdict.provenance.member_scores == {
            "security_vuln@served-qwen": 30,
            "security_vuln@served-gemma": 80,
        }
        assert out.verdict.provenance.driver == "security_vuln@served-gemma"

    def test_pin_reaches_a_distinct_cache_namespace(self, tmp_path: Path) -> None:
        # Adversarial: a model pin must change the cache key a real leaf member
        # reads — otherwise a cross-substrate ensemble would silently serve one
        # substrate's draws for both. Pin to an absent namespace and the replay
        # is a cache miss (a live call), not a quiet fall-through to the context.
        monitor = Monitor(MonitorConfig(id="security_vuln", model=MODEL), TEMPLATE)
        transcript = make_transcript()
        # Seed the cache under the context model only.
        run_calibrated([monitor], [transcript], MockLLMClient(), k=1, cache_dir=tmp_path)
        ctx_key = sample_cache_key(monitor, transcript, 0, 0.7, model="served-qwen")
        pin_key = sample_cache_key(monitor, transcript, 0, 0.7, model="served-gemma")
        assert ctx_key != pin_key  # the pin lands in its own namespace

        pinned = EnsembleMonitor("xsub", [Member(monitor, model_override="served-gemma")])
        fresh = MockLLMClient()
        run_calibrated(
            [pinned],
            [transcript],
            fresh,
            k=1,
            cache_dir=tmp_path,
            model_override="served-qwen",
        )
        # The pin overrode the runner model, so the seeded "served-qwen" entry
        # did not satisfy it: the member fetched its own "served-gemma" draw.
        assert fresh.call_count == 1
        assert fresh.calls[0]["model"] == "served-gemma"


class TestCompositeConstruction:
    def test_empty_members_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one member"):
            EnsembleMonitor("ens", [])

    def test_base_evaluate_once_is_abstract(self) -> None:
        composite = CompositeMonitor("c", [Member(StubMonitor("m", 0, flagged=False))])
        with pytest.raises(NotImplementedError):
            composite.evaluate_once(make_ctx())

    def test_config_model_defaults_to_first_member(self) -> None:
        pinned = Member(StubMonitor("a", 0, flagged=False), model_override="served-gemma")
        assert EnsembleMonitor("ens", [pinned]).config.model == "served-gemma"
        plain = Member(StubMonitor("b", 0, flagged=False))
        assert EnsembleMonitor("ens", [plain]).config.model == MODEL


class TestCompositeThroughRunCalibrated:
    """The seam goal: run_calibrated drives a composite exactly like a leaf."""

    def test_ensemble_fraction_is_the_or_across_draws(self) -> None:
        # k=2 draws: the ensemble flags whenever either member flags that draw.
        # StubMonitors return a fixed draw, so both draws flag -> fraction 1.0.
        flagged = StubMonitor("security_vuln", 60, flagged=True, reasoning="catch")
        quiet = StubMonitor("deception", 90, flagged=False, reasoning="noise")
        ensemble = EnsembleMonitor("ens", [Member(flagged), Member(quiet)])
        [cv] = run_calibrated([ensemble], [make_transcript()], MockLLMClient(), k=2, verify=False)
        assert cv.monitor_id == "ens"
        assert cv.sample_flagged == [True, True]
        assert cv.fraction_flagged == 1.0
        assert cv.mean_suspicion == 90.0  # max score per draw
        assert cv.samples[0].provenance is not None

    def test_cascade_escalation_recorded_per_draw(self) -> None:
        triage = StubMonitor("triage", 70, flagged=True, reasoning="t")
        deep = StubMonitor("deep", 80, flagged=True, reasoning="d")
        cascade = CascadeMonitor("casc", Member(triage), Member(deep))
        [cv] = run_calibrated([cascade], [make_transcript()], MockLLMClient(), k=3, verify=False)
        assert deep.calls == 3  # escalated on every draw
        assert all(s.provenance is not None and s.provenance.escalated for s in cv.samples)

    def test_cascade_short_circuit_through_the_loop(self) -> None:
        triage = StubMonitor("triage", 10, flagged=False, reasoning="t")
        deep = StubMonitor("deep", 80, flagged=True, reasoning="d")
        cascade = CascadeMonitor("casc", Member(triage), Member(deep))
        [cv] = run_calibrated([cascade], [make_transcript()], MockLLMClient(), k=3, verify=False)
        assert deep.calls == 0  # triage gated every draw
        assert cv.fraction_flagged == 0.0
        assert all(s.provenance is not None and not s.provenance.escalated for s in cv.samples)

    def test_ensemble_token_total_counts_every_member_once(self) -> None:
        # Honest cost: total input tokens = sum over members of their draw usage
        # (driver on the sample, the rest in extras), per draw.
        a = StubMonitor("security_vuln", 60, flagged=True)
        b = StubMonitor("deception", 90, flagged=False)
        ensemble = EnsembleMonitor("ens", [Member(a), Member(b)])
        [cv] = run_calibrated([ensemble], [make_transcript()], MockLLMClient(), k=1, verify=False)
        assert cv.total_input_tokens == 20  # 10 + 10, both members, once each
        assert cv.total_output_tokens == 10  # 5 + 5
