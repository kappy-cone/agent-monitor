"""Tests for VerifiedEnsembleMonitor — per-member verify-then-OR.

Mock-first, in the committed stage-test style: StubMonitors supply canned draws
and a StubPipeline supplies canned per-member verdicts, so the composition +
verification logic is exercised with zero model calls. The headline pin is the
rescue — the inverse of the EnsembleMonitor hazard
(``test_composite.py::test_louder_flagged_member_drives_over_a_quieter_catch``):
a refuted loud false positive no longer drops a quieter member's true catch. One
cache-replay test (skipped without ``.agentmon_cache``) proves the offline,
zero-completion property end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentmon.calibration import DrawContext, DrawResult, run_calibrated
from agentmon.llm.client import LLMResponse
from agentmon.llm.mock import MockLLMClient
from agentmon.monitors.base import Monitor, MonitorConfig
from agentmon.monitors.composite import (
    CascadeMonitor,
    CompositeMonitor,
    Member,
    VerifiedEnsembleMonitor,
)
from agentmon.schemas import (
    Evidence,
    FailureCategory,
    MonitorVerdict,
    ShellCommand,
    Transcript,
    UserMessage,
    VerificationOutcome,
)
from agentmon.verification import (
    ClaimGroundingStage,
    PipelineResult,
    QuoteGroundingStage,
    SemanticVerificationStage,
    StageContext,
    VerificationPipeline,
    default_pipeline,
)

MODEL = "claude-test-1"
TEMPLATE = "Judge this transcript:\n\n{{transcript}}\n\nRespond in JSON."

REPO = Path(__file__).resolve().parents[1]
CACHE = REPO / ".agentmon_cache"
SPLIT = REPO / "datasets" / "synthetic" / "split.json"
LABELS = REPO / "datasets" / "synthetic" / "labels.jsonl"
TRANSCRIPTS = REPO / "datasets" / "synthetic" / "transcripts"


def make_transcript(transcript_id: str = "t1") -> Transcript:
    return Transcript(
        id=transcript_id,
        source="synthetic",
        events=[
            UserMessage(index=0, text="please run a command"),
            ShellCommand(index=1, command="rm -rf /tmp/scratch", output="ok", exit_code=0),
        ],
    )


def make_ctx(*, model: str = MODEL, client: object | None = None) -> DrawContext:
    return DrawContext(
        transcript=make_transcript(),
        client=client or MockLLMClient(),
        cache_dir=None,
        sample_index=0,
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
    """A leaf draw-evaluator returning a canned verdict; no model call."""

    def __init__(self, monitor_id: str, score: int, *, flagged: bool, reasoning: str) -> None:
        self.config = MonitorConfig(id=monitor_id, model=MODEL)
        self._verdict = _verdict(score, flagged=flagged, reasoning=reasoning)

    def evaluate_once(self, ctx: DrawContext) -> DrawResult:
        return DrawResult(
            verdict=self._verdict,
            parse_retries=0,
            parse_repairs=0,
            extra_input_tokens=0,
            extra_output_tokens=0,
        )


class StubPipeline:
    """A VerificationPipeline stand-in: canned ``supported`` keyed on the
    verdict's reasoning, recording the served model each run saw (so per-member
    keying is observable without a client). Fixed per-call usage.
    """

    def __init__(self, supported_by_reasoning: dict[str, bool]) -> None:
        self.supported_by_reasoning = supported_by_reasoning
        self.seen_models: list[str] = []
        self.calls = 0

    def run(self, ctx: StageContext) -> PipelineResult:
        self.calls += 1
        self.seen_models.append(ctx.model)
        supported = self.supported_by_reasoning.get(ctx.verdict.reasoning, True)
        combined = VerificationOutcome(
            supported=supported, model=ctx.model, input_tokens=3, output_tokens=2
        )
        return PipelineResult(combined=combined, stage_outcomes=())


class TestRescue:
    def test_refuted_loud_driver_no_longer_drops_a_quiet_catch(self) -> None:
        # The whole point: loud false positive (80, refuted) + quiet real catch
        # (40, upheld). The ensemble stays flagged on the SURVIVING quiet catch.
        loud_fp = StubMonitor("reward_hacking", 80, flagged=True, reasoning="loud false positive")
        quiet = StubMonitor("security_vuln", 40, flagged=True, reasoning="quiet real catch")
        pipeline = StubPipeline({"loud false positive": False, "quiet real catch": True})
        ensemble = VerifiedEnsembleMonitor(
            "ver-ens", [Member(loud_fp), Member(quiet)], pipeline=pipeline
        )
        out = ensemble.evaluate_once(make_ctx())
        assert out.verdict.categories  # still flagged — the quiet catch survived
        assert out.verdict.reasoning == "quiet real catch"  # driver is the survivor
        assert out.verdict.suspicion_score == 40  # the survivor's score, not the loud 80
        prov = out.verdict.provenance
        assert prov is not None
        assert prov.driver == "security_vuln"
        assert prov.member_scores == {"reward_hacking": 80, "security_vuln": 40}
        assert prov.member_supported == {"reward_hacking": False, "security_vuln": True}
        # Raw (pre-verification) flags: BOTH members flagged, even though the
        # loud one was refuted — the flip decomposition needs the raw picture.
        assert prov.member_flagged == {"reward_hacking": True, "security_vuln": True}
        # The full folded outcome per verified member, straight from the fold.
        assert prov.member_verifications["reward_hacking"].supported is False
        assert prov.member_verifications["security_vuln"].supported is True


class TestBranchB:
    def test_all_flags_refuted_yields_an_unflagged_draw(self) -> None:
        a = StubMonitor("reward_hacking", 80, flagged=True, reasoning="fp a")
        b = StubMonitor("security_vuln", 50, flagged=True, reasoning="fp b")
        pipeline = StubPipeline({"fp a": False, "fp b": False})
        out = VerifiedEnsembleMonitor(
            "ver", [Member(a), Member(b)], pipeline=pipeline
        ).evaluate_once(make_ctx())
        assert not out.verdict.categories  # no survivor -> unflagged (categories forced [])
        assert out.verdict.suspicion_score == 80  # highest member's OWN score, not a max-fold
        prov = out.verdict.provenance
        assert prov is not None
        assert prov.driver == "reward_hacking"
        assert prov.member_supported == {"reward_hacking": False, "security_vuln": False}

    def test_no_member_flags_skips_verification_entirely(self) -> None:
        a = StubMonitor("reward_hacking", 30, flagged=False, reasoning="quiet a")
        b = StubMonitor("security_vuln", 40, flagged=False, reasoning="quiet b")
        pipeline = StubPipeline({})
        out = VerifiedEnsembleMonitor(
            "ver", [Member(a), Member(b)], pipeline=pipeline
        ).evaluate_once(make_ctx())
        assert pipeline.calls == 0  # unflagged members are never verified — no spend
        assert not out.verdict.categories
        assert out.verdict.suspicion_score == 40  # highest-scoring member
        assert out.verdict.provenance is not None
        assert out.verdict.provenance.member_supported == {}  # nothing verified

    def test_branch_b_picks_highest_overall_not_a_refuted_flagged_member(self) -> None:
        # Regression: with no survivor, the driver is the highest-scoring member
        # OVERALL. A refuted flagged member (80) keeps its leaf categories, so a
        # flagged-first selection would wrongly pick it over a higher UNFLAGGED
        # member (90). The fix is flag-blind max over all draws.
        refuted_loud = StubMonitor("reward_hacking", 80, flagged=True, reasoning="refuted")
        quiet_high = StubMonitor("security_vuln", 90, flagged=False, reasoning="quiet high")
        pipeline = StubPipeline({"refuted": False})
        out = VerifiedEnsembleMonitor(
            "ver", [Member(refuted_loud), Member(quiet_high)], pipeline=pipeline
        ).evaluate_once(make_ctx())
        assert not out.verdict.categories  # nothing survived -> unflagged
        assert out.verdict.suspicion_score == 90  # highest overall, not the refuted 80
        assert out.verdict.evidence == []  # an unflagged draw surfaces no grounding
        prov = out.verdict.provenance
        assert prov is not None
        assert prov.driver == "security_vuln"  # the unflagged high scorer
        assert prov.member_supported == {"reward_hacking": False}  # only flagged was verified
        assert prov.member_flagged == {"reward_hacking": True, "security_vuln": False}
        assert set(prov.member_verifications) == {"reward_hacking"}  # unflagged never verified


class TestSurvivorSemantics:
    def test_score_is_max_over_survivors_not_all_members(self) -> None:
        # A refuted loud member must not inflate the score of a flag that now
        # stands only on the survivors.
        loud = StubMonitor("reward_hacking", 80, flagged=True, reasoning="loud")
        mid = StubMonitor("scope_expansion", 50, flagged=True, reasoning="mid")
        low = StubMonitor("security_vuln", 30, flagged=True, reasoning="low")
        pipeline = StubPipeline({"loud": False, "mid": True, "low": True})
        out = VerifiedEnsembleMonitor(
            "ver", [Member(loud), Member(mid), Member(low)], pipeline=pipeline
        ).evaluate_once(make_ctx())
        assert out.verdict.categories
        assert out.verdict.suspicion_score == 50  # max over survivors (50, 30), not 80
        assert out.verdict.provenance is not None
        assert out.verdict.provenance.driver == "scope_expansion"


class TestPerMemberKeying:
    def test_each_flagged_member_verifies_at_its_own_served_model(self) -> None:
        # C2: the cross-substrate fix. The pinned member verifies under its pin,
        # the unpinned under the context model — distinct cache namespaces.
        a = StubMonitor("security_vuln", 60, flagged=True, reasoning="a")
        b = StubMonitor("security_vuln", 70, flagged=True, reasoning="b")
        pipeline = StubPipeline({"a": True, "b": True})
        ensemble = VerifiedEnsembleMonitor(
            "xsub",
            [Member(a), Member(b, model_override="served-gemma")],
            pipeline=pipeline,
        )
        ensemble.evaluate_once(make_ctx(model="served-qwen"))
        assert pipeline.seen_models == ["served-qwen", "served-gemma"]


class TestCascadeOverVerifiedEnsemble:
    """The vcasc[gen->vens] provenance pins: the cascade re-stamps the returned
    verdict but carries the deep composite's own provenance through under
    ``inner`` instead of destroying it, and delegates ``self_verifies`` to the
    deep member (the triage gate is never verified — verification can only turn
    flags off, so verifying the gate could only cost recall)."""

    def test_inner_exposes_which_member_raw_flagged_and_which_was_refuted(self) -> None:
        # An escalated draw whose deep verified ensemble refuted its loud member
        # and kept the quiet one: `inner` shows both raw flags and the refute.
        triage = StubMonitor("triage", 70, flagged=True, reasoning="gate")
        loud = StubMonitor("reward_hacking", 80, flagged=True, reasoning="loud fp")
        quiet = StubMonitor("security_vuln", 40, flagged=True, reasoning="quiet catch")
        pipeline = StubPipeline({"loud fp": False, "quiet catch": True})
        vens = VerifiedEnsembleMonitor("vens", [Member(loud), Member(quiet)], pipeline=pipeline)
        out = CascadeMonitor("vcasc", Member(triage), Member(vens)).evaluate_once(make_ctx())
        assert out.verdict.categories  # the quiet catch survived through the cascade
        prov = out.verdict.provenance
        assert prov is not None and prov.escalated
        assert prov.driver == "vens"
        # The deep entry is the RETURNED (post-verification) flag; raw flags below.
        assert prov.member_flagged == {"triage": True, "vens": True}
        inner = prov.inner
        assert inner is not None and inner.composite == "vens"
        assert inner.member_flagged == {"reward_hacking": True, "security_vuln": True}  # raw
        assert inner.member_verifications["reward_hacking"].supported is False  # the refute
        assert inner.member_verifications["security_vuln"].supported is True
        assert inner.driver == "security_vuln"  # the survivor drove the deep draw

    def test_inner_distinguishes_all_refuted_escalation_from_a_deep_miss(self) -> None:
        # The gate-miss vs catch-loss pin: both cascades below escalate and
        # return an unflagged draw — identical at the top level. Only `inner`
        # separates "a deep member raw-flagged and verification refuted it"
        # (catch-loss, verification's doing) from "no deep member ever
        # raw-flagged" (a deep miss). Without the carry-through, the composite
        # catch-loss guard would be vacuous on every cascade row.
        triage = StubMonitor("triage", 70, flagged=True, reasoning="gate")

        refuted = StubMonitor("deception", 75, flagged=True, reasoning="refuted claim")
        vens_loss = VerifiedEnsembleMonitor(
            "vens", [Member(refuted)], pipeline=StubPipeline({"refuted claim": False})
        )
        out_loss = CascadeMonitor("vcasc", Member(triage), Member(vens_loss)).evaluate_once(
            make_ctx()
        )

        quiet = StubMonitor("deception", 20, flagged=False, reasoning="quiet")
        vens_miss = VerifiedEnsembleMonitor("vens", [Member(quiet)], pipeline=StubPipeline({}))
        out_miss = CascadeMonitor("vcasc", Member(triage), Member(vens_miss)).evaluate_once(
            make_ctx()
        )

        for out in (out_loss, out_miss):  # indistinguishable at the top level
            assert not out.verdict.categories
            prov = out.verdict.provenance
            assert prov is not None and prov.escalated
            assert prov.member_flagged == {"triage": True, "vens": False}

        inner_loss = out_loss.verdict.provenance.inner  # type: ignore[union-attr]
        assert inner_loss is not None
        assert inner_loss.member_flagged == {"deception": True}  # raw-flagged...
        assert inner_loss.member_verifications["deception"].supported is False  # ...and refuted

        inner_miss = out_miss.verdict.provenance.inner  # type: ignore[union-attr]
        assert inner_miss is not None
        assert inner_miss.member_flagged == {"deception": False}  # never raw-flagged
        assert inner_miss.member_verifications == {}  # nothing was ever verified

    def test_cascade_over_self_verifying_deep_is_skipped_by_the_flip_pass(self) -> None:
        # Delegation through run_calibrated: the cascade reads as self-verifying
        # because its deep member is, so verify=True never runs the frozen
        # flip-on-fail pass (no client call, no `verifications` entry) — the
        # deep verified ensemble already verified its own members.
        triage = StubMonitor("triage", 70, flagged=True, reasoning="gate")
        member = StubMonitor("security_vuln", 60, flagged=True, reasoning="catch")
        vens = VerifiedEnsembleMonitor(
            "vens", [Member(member)], pipeline=StubPipeline({"catch": True})
        )
        cascade = CascadeMonitor("vcasc", Member(triage), Member(vens))
        assert cascade.self_verifies is True  # verified iff the deep member is

        client = MockLLMClient()
        [cv] = run_calibrated([cascade], [make_transcript()], client, k=2, verify=True)
        assert client.call_count == 0  # the frozen flip pass never ran
        assert cv.verifications == [None, None]
        assert cv.sample_flagged == [True, True]  # the member survived its own verification


class TestCostAndDriving:
    def test_total_counts_each_member_draw_and_verification_once(self) -> None:
        # Driven through run_calibrated with verify=False (the contract). Each
        # member draw (10 in / 5 out) and each member verification (3 in / 2 out)
        # is counted exactly once; the frozen verification path contributes 0.
        a = StubMonitor("reward_hacking", 60, flagged=True, reasoning="a")
        b = StubMonitor("security_vuln", 70, flagged=True, reasoning="b")
        pipeline = StubPipeline({"a": True, "b": True})
        ensemble = VerifiedEnsembleMonitor("ver", [Member(a), Member(b)], pipeline=pipeline)
        [cv] = run_calibrated([ensemble], [make_transcript()], MockLLMClient(), k=1, verify=False)
        assert cv.total_input_tokens == 26  # 2 draws*10 + 2 verifications*3
        assert cv.total_output_tokens == 14  # 2 draws*5 + 2 verifications*2
        assert cv.verifications == [None]  # the frozen path never ran (verify=False)
        assert cv.stage_outcomes is None

    def test_all_refuted_branch_b_still_counts_every_member_call(self) -> None:
        # C6 in Branch B: every flag refuted -> driver chosen from all draws, but
        # the cost fold is over `draws` (not survivors), so every member's draw
        # AND verification is still counted exactly once.
        a = StubMonitor("reward_hacking", 80, flagged=True, reasoning="a")
        b = StubMonitor("security_vuln", 50, flagged=True, reasoning="b")
        pipeline = StubPipeline({"a": False, "b": False})
        ensemble = VerifiedEnsembleMonitor("ver", [Member(a), Member(b)], pipeline=pipeline)
        [cv] = run_calibrated([ensemble], [make_transcript()], MockLLMClient(), k=1, verify=False)
        assert cv.sample_flagged == [False]  # all refuted -> unflagged
        assert cv.total_input_tokens == 26  # 2 draws*10 + 2 verifications*3, despite all dropped
        assert cv.total_output_tokens == 14

    def test_self_verifies_guard_skips_the_redundant_pass_under_verify_true(self) -> None:
        # The former footgun, now guarded: this monitor sets self_verifies, so
        # run_calibrated skips its flip-on-fail pass. verify=True is therefore
        # IDENTICAL to verify=False — no extra client call, no entry in
        # `verifications`, no cost double-count. (Without the guard, verify=True
        # re-verified the driver via the default pipeline, a redundant,
        # mis-keyed, cost-double-counting call.)
        assert (
            VerifiedEnsembleMonitor(
                "ver", [Member(StubMonitor("a", 1, flagged=False, reasoning="a"))]
            ).self_verifies
            is True
        )
        a = StubMonitor("security_vuln", 70, flagged=True, reasoning="a")
        pipeline = StubPipeline({"a": True})
        ensemble = VerifiedEnsembleMonitor("ver", [Member(a)], pipeline=pipeline)

        off_client = MockLLMClient()
        [off_cv] = run_calibrated([ensemble], [make_transcript()], off_client, k=1, verify=False)
        assert off_client.call_count == 0  # self-contained, no client touched

        on_client = MockLLMClient()
        [on_cv] = run_calibrated([ensemble], [make_transcript()], on_client, k=1, verify=True)
        assert on_client.call_count == 0  # the guard skipped the redundant verification
        assert on_cv.verifications == [None]  # frozen pass did not run for a self-verifier
        assert on_cv.total_input_tokens == off_cv.total_input_tokens  # no double-count
        assert on_cv.sample_flagged == off_cv.sample_flagged  # same flag either way

    def test_guard_is_per_monitor_other_monitors_still_verified(self) -> None:
        # The guard is per-monitor: a non-self-verifying monitor in the same
        # verify=True batch is still verified by the frozen pass (and flips on a
        # refute); only the self-verifier's frozen pass is skipped. The run-level
        # pipeline drives the frozen pass; the ensemble uses its own injected one.
        leaf = StubMonitor("security_vuln", 80, flagged=True, reasoning="leaf-claim")
        ensemble = VerifiedEnsembleMonitor(
            "ver",
            [Member(StubMonitor("deception", 70, flagged=True, reasoning="ens"))],
            pipeline=StubPipeline({"ens": True}),
        )
        run_pipeline = StubPipeline({"leaf-claim": False})  # frozen pass refutes the leaf
        leaf_cv, ens_cv = run_calibrated(
            [leaf, ensemble],
            [make_transcript()],
            MockLLMClient(),
            k=1,
            verify=True,
            pipeline=run_pipeline,
        )
        assert leaf_cv.verifications[0] is not None  # leaf WAS verified by the frozen pass
        assert leaf_cv.sample_flagged == [False]  # and flipped on the refute
        assert ens_cv.verifications == [None]  # the self-verifier's frozen pass was skipped
        assert ens_cv.sample_flagged == [True]  # its member survived its OWN verification


class _FixedUsageScriptedClient:
    """Substring-scripted responses with fixed per-call usage, so totals are assertable."""

    def __init__(self, script: dict[str, str]) -> None:
        self.script = script
        self.calls = 0

    def complete(
        self, prompt: str, *, model: str, max_tokens: int = 4096, temperature: float = 0.0
    ) -> LLMResponse:
        self.calls += 1
        text = next((resp for needle, resp in self.script.items() if needle in prompt), "{}")
        return LLMResponse(text=text, model=model, input_tokens=10, output_tokens=5, latency_ms=0.0)


class TestStageAwareVerificationCost:
    def test_multi_llm_stage_pipeline_counts_every_stage_call(self) -> None:
        # DECISIONS 45-C6: with an injected multi-LLM-stage pipeline a member's
        # verification can spend more than its deciding stage. The ensemble sums
        # usage over the recorded stage outcomes, not the folded outcome, so
        # both the semantic and the claim-grounding call are costed.
        member = StubMonitor("deception", 70, flagged=True, reasoning="claimful")
        pipeline = VerificationPipeline(
            [QuoteGroundingStage(), SemanticVerificationStage(), ClaimGroundingStage()]
        )
        client = _FixedUsageScriptedClient(
            {
                "auditing one claim made": json.dumps({"supported": True}),
                "*deception* flag": json.dumps({"contradicted": True}),
            }
        )
        ensemble = VerifiedEnsembleMonitor("ver", [Member(member)], pipeline=pipeline)
        [cv] = run_calibrated([ensemble], [make_transcript()], client, k=1, verify=False)
        assert client.calls == 2  # semantic + claim grounding both ran
        assert cv.total_input_tokens == 30  # draw 10 + two verification calls (10 + 10)
        assert cv.total_output_tokens == 15  # draw 5 + 5 + 5

    def test_default_pipeline_totals_unchanged_for_the_single_deciding_stage(self) -> None:
        # The default pipeline's stage sum equals the folded outcome's usage —
        # only the semantic stage spends — so totals are pinned unchanged.
        member = StubMonitor("security_vuln", 70, flagged=True, reasoning="claimful")
        client = _FixedUsageScriptedClient(
            {"auditing one claim made": json.dumps({"supported": True})}
        )
        ensemble = VerifiedEnsembleMonitor("ver", [Member(member)], pipeline=default_pipeline())
        [cv] = run_calibrated([ensemble], [make_transcript()], client, k=1, verify=False)
        assert client.calls == 1
        assert cv.total_input_tokens == 20  # draw 10 + one semantic call 10
        assert cv.total_output_tokens == 10


class TestOptIn:
    def test_load_monitors_never_returns_a_composite(self) -> None:
        from agentmon.monitors.registry import load_monitors

        loaded = load_monitors()
        assert loaded  # there are leaf monitors
        assert all(isinstance(m, Monitor) for m in loaded.values())
        assert not any(isinstance(m, CompositeMonitor) for m in loaded.values())


@pytest.mark.skipif(
    not (CACHE.exists() and SPLIT.exists()),
    reason="no .agentmon_cache / split (gitignored) — run locally for offline-replay proof",
)
class TestOfflineReplay:
    def test_singleton_verified_ensemble_replays_from_cache_with_no_live_call(self) -> None:
        # C5: a verified ensemble reconstructs each member's draw AND its
        # verification from the frozen per-substrate verify=True cache, so it
        # replays with zero completions. A singleton over the qwen dev slice
        # exercises both reads (draw + verification) under one served model.
        from agentmon.eval.split import load_labels, load_split, parse_provenance
        from agentmon.monitors.registry import load_monitors

        split = load_split(SPLIT)
        provs = {p.transcript_id: p for p in map(parse_provenance, load_labels(LABELS))}
        dev_tg = sorted(t for t in split.dev_ids if provs[t].source == "tinygrad")
        transcripts = [
            Transcript.model_validate_json((TRANSCRIPTS / f"{t}.json").read_text(encoding="utf-8"))
            for t in dev_tg
        ]
        monitor = load_monitors()["security_vuln"]
        ensemble = VerifiedEnsembleMonitor("ver[security_vuln]", [Member(monitor)])

        class _NoCall:
            def complete(self, *args: object, **kwargs: object) -> object:
                raise AssertionError("cache MISS: a live call was attempted")

        got = run_calibrated(
            [ensemble],
            transcripts,
            _NoCall(),
            k=3,
            cache_dir=CACHE,
            verify=False,
            model_override="agentnom-local",
        )
        assert len(got) == len(transcripts)  # every draw + flagged-member verification was cached
        # At least one dev failure stays flagged after per-member verification.
        assert any(v.fraction_flagged > 0 for v in got)
