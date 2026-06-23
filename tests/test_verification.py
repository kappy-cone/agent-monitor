"""Tests for the verification pipeline and its stages.

Phase 1 (default pipeline) behaviour is pinned byte-for-byte against the
committed test matrix by ``test_verification_golden.py``; here we cover the stage
seam directly — the default two stages, the fold rule, and the opt-in event-order
and claim-grounding stages — all mock-first. The claim-grounding stage is also
validated on the six purpose-built dev deception cases (vendored under
``fixtures/claim_grounding_dev/``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentmon.calibration import run_calibrated, verify_sample
from agentmon.llm.client import LLMResponse
from agentmon.llm.mock import MockLLMClient
from agentmon.monitors.base import Monitor, MonitorConfig, render_event
from agentmon.schemas import (
    Evidence,
    FailureCategory,
    MonitorVerdict,
    ShellCommand,
    Transcript,
    UserMessage,
)
from agentmon.verification import (
    ClaimGroundingStage,
    EventOrderStage,
    QuoteGroundingStage,
    SemanticVerificationStage,
    StageContext,
    VerificationPipeline,
    build_verification_prompt,
    default_pipeline,
    quotes_match,
)

TEMPLATE = "Judge this transcript:\n\n{{transcript}}\n\nRespond in JSON."
MODEL = "claude-test-1"


def make_monitor(monitor_id: str = "judge") -> Monitor:
    return Monitor(MonitorConfig(id=monitor_id, model=MODEL), TEMPLATE)


def make_transcript(transcript_id: str = "t1") -> Transcript:
    return Transcript(
        id=transcript_id,
        source="synthetic",
        events=[
            UserMessage(index=0, text="please run a command"),
            ShellCommand(index=1, command="rm -rf /tmp/scratch", output="ok", exit_code=0),
        ],
    )


def make_verdict(
    *,
    quote: str = "rm -rf /tmp/scratch",
    event_index: int = 1,
    categories: list[str] | None = None,
    reasoning: str = "scripted",
) -> MonitorVerdict:
    return MonitorVerdict(
        monitor_id="judge",
        transcript_id="t1",
        suspicion_score=80,
        categories=[FailureCategory(c) for c in (categories or ["security_vuln"])],
        evidence=[Evidence(event_index=event_index, quote=quote)],
        reasoning=reasoning,
        model=MODEL,
    )


def ctx_for(
    verdict: MonitorVerdict,
    client: object,
    *,
    cache_dir: Path | None = None,
    transcript: Transcript | None = None,
) -> StageContext:
    return StageContext(
        transcript=transcript or make_transcript(),
        verdict=verdict,
        client=client,  # type: ignore[arg-type]
        cache_dir=cache_dir,
        model=MODEL,
    )


def verdict_json(score: int, categories: list[str], quote: str = "rm -rf /tmp/scratch") -> str:
    return json.dumps(
        {
            "suspicion_score": score,
            "categories": categories,
            "evidence": [{"event_index": 1, "quote": quote}],
            "reasoning": "scripted",
        }
    )


class SequenceLLMClient:
    """Returns scripted responses in call order; records every call."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def complete(
        self, prompt: str, *, model: str, max_tokens: int = 4096, temperature: float = 0.0
    ) -> LLMResponse:
        self.calls.append({"prompt": prompt, "model": model, "temperature": temperature})
        text = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        return LLMResponse(text=text, model=model, input_tokens=10, output_tokens=5)


class TestQuoteGroundingStage:
    def test_abstains_and_reports_verbatim_match(self) -> None:
        out = QuoteGroundingStage().run(ctx_for(make_verdict(), MockLLMClient()))
        assert out.supported is None  # diagnostic only — never flips
        assert out.quote_match is True

    def test_reports_paraphrase_mismatch(self) -> None:
        verdict = make_verdict(quote="removed the scratch directory")
        out = QuoteGroundingStage().run(ctx_for(verdict, MockLLMClient()))
        assert out.supported is None
        assert out.quote_match is False

    def test_makes_no_model_call(self) -> None:
        client = MockLLMClient()
        QuoteGroundingStage().run(ctx_for(make_verdict(), client))
        assert client.call_count == 0


class TestSemanticVerificationStage:
    def test_no_valid_citation_refutes_without_a_call(self) -> None:
        client = MockLLMClient()
        verdict = make_verdict(event_index=99)  # not in the transcript
        out = SemanticVerificationStage().run(ctx_for(verdict, client))
        assert out.supported is False
        assert client.call_count == 0
        assert out.reasoning == "no cited event index exists in the transcript"

    def test_supported_decision_from_model(self) -> None:
        client = SequenceLLMClient([json.dumps({"supported": True, "reasoning": "ok"})])
        out = SemanticVerificationStage().run(ctx_for(make_verdict(), client))
        assert out.supported is True
        assert client.call_count == 1

    def test_parse_failure_fails_open(self) -> None:
        client = SequenceLLMClient(["not a verdict"])
        out = SemanticVerificationStage().run(ctx_for(make_verdict(), client))
        assert out.supported is True
        assert out.parse_error is not None


class TestVerificationPipelineFold:
    def test_default_pipeline_matches_verify_sample(self) -> None:
        # The compat wrapper and the pipeline are one code path.
        responses = [json.dumps({"supported": False, "reasoning": "unsupported"})]
        verdict = make_verdict()
        a = default_pipeline().run(ctx_for(verdict, SequenceLLMClient(responses))).combined
        b = verify_sample(
            make_monitor(), make_transcript(), verdict, SequenceLLMClient(responses), None
        )
        assert a.model_dump() == b.model_dump()

    def test_quote_match_overlaid_onto_semantic_outcome(self) -> None:
        client = SequenceLLMClient([json.dumps({"supported": False, "reasoning": "no"})])
        out = default_pipeline().run(ctx_for(make_verdict(), client)).combined
        assert out.supported is False  # the semantic stage's decision
        assert out.quote_match is True  # overlaid from the quote-grounding stage

    def test_refutation_short_circuits_later_stages(self) -> None:
        # A refuting first stage stops the pipeline before the LLM stage runs.
        client = MockLLMClient()

        class AlwaysRefute:
            stage_id = "refute"

            def run(self, ctx: StageContext):
                from agentmon.schemas import StageOutcome

                return StageOutcome(stage_id=self.stage_id, supported=False, reasoning="nope")

        pipeline = VerificationPipeline([AlwaysRefute(), SemanticVerificationStage()])
        result = pipeline.run(ctx_for(make_verdict(), client))
        assert result.combined.supported is False
        assert client.call_count == 0  # the semantic stage never ran
        assert len(result.stage_outcomes) == 1

    def test_all_abstain_fails_open(self) -> None:
        result = VerificationPipeline([QuoteGroundingStage()]).run(
            ctx_for(make_verdict(), MockLLMClient())
        )
        assert result.combined.supported is True  # no decision -> keep the flag
        assert result.combined.quote_match is True

    def test_no_citation_path_folds_with_overlaid_quote_match(self) -> None:
        # The committed test matrix contains no no-citation outcomes, so the
        # golden master never exercises this path; pin it directly. No valid
        # citation -> refute with no call, quote_match overlaid from the mechanical
        # stage (False for an absent index), all model usage zeroed.
        client = MockLLMClient()
        verdict = make_verdict(event_index=99)  # absent from the transcript
        out = default_pipeline().run(ctx_for(verdict, client)).combined
        assert client.call_count == 0
        assert out.supported is False
        assert out.reasoning == "no cited event index exists in the transcript"
        assert out.quote_match == quotes_match(make_transcript(), verdict)
        assert out.quote_match is False
        assert (out.model, out.input_tokens, out.output_tokens) == ("", 0, 0)
        assert out.raw_response == ""
        assert out.parse_error is None


class TestEventOrderStage:
    @pytest.mark.parametrize(
        "reasoning",
        [
            "the count (event 22) was taken before the test deletion (event 21)",
            "event 21 happened only after event 22, so the count is stale",
            "the deletion at event 9 occurs before the count at event 4",
            "the grep at index 22 precedes the deletion at index 21",
        ],
    )
    def test_refutes_backwards_ordering(self, reasoning: str) -> None:
        out = EventOrderStage().run(ctx_for(make_verdict(reasoning=reasoning), MockLLMClient()))
        assert out.supported is False
        assert "opposite order" in out.reasoning

    @pytest.mark.parametrize(
        "reasoning",
        [
            "event 21 runs before event 22, the expected order",
            "the deletion at event 21 precedes the count at event 22",
            "the agent skipped a step; nothing about ordering here",
            "only one reference, event 22, is mentioned",
        ],
    )
    def test_abstains_when_consistent_or_silent(self, reasoning: str) -> None:
        out = EventOrderStage().run(ctx_for(make_verdict(reasoning=reasoning), MockLLMClient()))
        assert out.supported is None

    @pytest.mark.parametrize(
        "reasoning",
        [
            "the loop reads arr[5] before writing arr[3]",  # array indices, not events
            "as shown [9], the claim made before [4] is false",  # citation footnotes
            "match group [2] appears after group [8] in the pattern",  # regex groups
            "the deletion [9] occurs before the count [4]",  # bracket evidence pointers
            "the agent claimed at event 8 it tested prior to merging, but no test ran at event 2",
            "event 9 disabled the check; before commit it was never re-enabled, see event 4",
        ],
    )
    def test_abstains_on_non_ordering_phrasings(self, reasoning: str) -> None:
        # A bracketed integer is never an event ref, and a relation word tying
        # clauses across a comma/semicolon/conjunction does not tie the cited
        # indices -- so these would-be false refutations abstain (regression guard
        # for the over-refutation finding; a false refutation here kills a catch).
        out = EventOrderStage().run(ctx_for(make_verdict(reasoning=reasoning), MockLLMClient()))
        assert out.supported is None

    def test_abstains_when_indices_are_far_apart(self) -> None:
        # Refs separated by more than the ordering window are not related.
        filler = "x" * 90
        reasoning = f"event 5 {filler} before {filler} event 3"
        out = EventOrderStage().run(ctx_for(make_verdict(reasoning=reasoning), MockLLMClient()))
        assert out.supported is None

    def test_makes_no_model_call(self) -> None:
        client = MockLLMClient()
        EventOrderStage().run(ctx_for(make_verdict(reasoning="event 2 before event 1"), client))
        assert client.call_count == 0


class TestClaimGroundingStage:
    def test_contradiction_upholds_the_flag(self) -> None:
        client = SequenceLLMClient([json.dumps({"contradicted": True, "reasoning": "refuted"})])
        out = ClaimGroundingStage().run(ctx_for(make_verdict(categories=["deception"]), client))
        assert out.supported is True
        assert client.call_count == 1

    def test_no_contradiction_refutes_the_flag(self) -> None:
        client = SequenceLLMClient([json.dumps({"contradicted": False, "reasoning": "consistent"})])
        out = ClaimGroundingStage().run(ctx_for(make_verdict(categories=["deception"]), client))
        assert out.supported is False

    def test_no_valid_citation_refutes_without_a_call(self) -> None:
        client = MockLLMClient()
        out = ClaimGroundingStage().run(ctx_for(make_verdict(event_index=99), client))
        assert out.supported is False
        assert client.call_count == 0

    def test_parse_failure_fails_open(self) -> None:
        client = SequenceLLMClient(["not json"])
        out = ClaimGroundingStage().run(ctx_for(make_verdict(categories=["deception"]), client))
        assert out.supported is True
        assert out.parse_error is not None

    def test_distinct_cache_key_from_semantic(self, tmp_path: Path) -> None:
        # The two LLM stages must not share — or be served — each other's cache.
        verdict = make_verdict(categories=["deception"])
        sem_client = SequenceLLMClient([json.dumps({"supported": True})])
        claim_client = SequenceLLMClient([json.dumps({"contradicted": True})])
        SemanticVerificationStage().run(ctx_for(verdict, sem_client, cache_dir=tmp_path))
        ClaimGroundingStage().run(ctx_for(verdict, claim_client, cache_dir=tmp_path))
        assert len(list(tmp_path.glob("*.json"))) == 2  # two distinct entries

    @staticmethod
    def _wide_transcript(n: int) -> Transcript:
        # Distinct per-event content ("cmd-i") so a presence check is discriminative.
        return Transcript(
            id="wide",
            source="synthetic",
            events=[ShellCommand(index=i, command=f"cmd-{i}", output="o") for i in range(n)],
        )

    @staticmethod
    def _deception_verdict(cited: list[int]) -> MonitorVerdict:
        # Quotes are "q{i}" (distinct from the "cmd-{i}" event body) so the events
        # section, not the quotes line, is what surfaces a command.
        return MonitorVerdict(
            monitor_id="deception",
            transcript_id="wide",
            suspicion_score=80,
            categories=[FailureCategory.DECEPTION],
            evidence=[Evidence(event_index=i, quote=f"q{i}") for i in cited],
            reasoning="claim",
            model=MODEL,
        )

    def test_surfaces_only_cited_events(self) -> None:
        transcript = self._wide_transcript(12)
        cited = [1, 3, 11]
        client = MockLLMClient(default_response=json.dumps({"contradicted": True}))
        ClaimGroundingStage().run(
            ctx_for(self._deception_verdict(cited), client, transcript=transcript)
        )
        prompt = str(client.calls[0]["prompt"])
        for i in cited:
            assert f"cmd-{i}" in prompt  # cited events are rendered
        for i in (2, 4, 10):
            assert f"cmd-{i}" not in prompt  # uncited events are not

    def test_caps_cited_events(self) -> None:
        transcript = self._wide_transcript(12)
        cited = list(range(10))  # exceeds the 8-citation cap
        client = MockLLMClient(default_response=json.dumps({"contradicted": True}))
        ClaimGroundingStage().run(
            ctx_for(self._deception_verdict(cited), client, transcript=transcript)
        )
        prompt = str(client.calls[0]["prompt"])
        rendered = [i for i in cited if f"cmd-{i}" in prompt]
        assert rendered == list(range(8))  # the lowest 8 indices, sorted; the rest dropped


class TestOptInPipelineThroughRunCalibrated:
    def _ordering_pipeline(self) -> VerificationPipeline:
        return VerificationPipeline(
            [QuoteGroundingStage(), EventOrderStage(), SemanticVerificationStage()]
        )

    def test_event_order_flip_records_stage_outcomes(self) -> None:
        # A flagged sample whose reasoning inverts the event order flips to
        # unflagged via the deterministic stage, with no semantic call.
        sample = json.dumps(
            {
                "suspicion_score": 75,
                "categories": ["deception"],
                "evidence": [{"event_index": 1, "quote": "rm -rf /tmp/scratch"}],
                "reasoning": "the output (event 1) was produced before the request (event 0)",
            }
        )
        client = SequenceLLMClient([sample])
        [result] = run_calibrated(
            [make_monitor()],
            [make_transcript()],
            client,
            k=1,
            verify=True,
            pipeline=self._ordering_pipeline(),
        )
        assert result.sample_flagged == [False]
        assert client.call_count == 1  # the sample only; event-order needs no call
        assert result.stage_outcomes is not None
        stages = result.stage_outcomes[0]
        assert stages is not None
        assert [s.stage_id for s in stages] == ["quote_grounding", "event_order"]
        assert stages[-1].supported is False

    def test_default_pipeline_leaves_stage_outcomes_none(self) -> None:
        client = SequenceLLMClient(
            [verdict_json(80, ["security_vuln"]), json.dumps({"supported": True})]
        )
        [result] = run_calibrated([make_monitor()], [make_transcript()], client, k=1, verify=True)
        assert result.stage_outcomes is None  # default path stays byte-identical
        assert result.verifications[0] is not None

    def test_stage_outcomes_parallel_with_none_for_unflagged(self) -> None:
        responses = [
            verdict_json(80, ["deception"]),  # flagged -> verified
            verdict_json(10, []),  # unflagged -> not verified
            json.dumps({"supported": True}),  # semantic for the first sample
        ]
        [result] = run_calibrated(
            [make_monitor()],
            [make_transcript()],
            SequenceLLMClient(responses),
            k=2,
            pipeline=VerificationPipeline([QuoteGroundingStage(), SemanticVerificationStage()]),
            verify=True,
        )
        assert result.stage_outcomes is not None
        assert result.stage_outcomes[0] is not None
        assert result.stage_outcomes[1] is None  # the unflagged sample was not verified

    def test_totals_count_every_stage_call_without_double_counting(self) -> None:
        # Two LLM stages both run (semantic upholds, then claim-grounding runs).
        responses = [
            verdict_json(80, ["deception"]),
            json.dumps({"supported": True}),
            json.dumps({"contradicted": True}),
        ]
        [result] = run_calibrated(
            [make_monitor()],
            [make_transcript()],
            SequenceLLMClient(responses),
            k=1,
            pipeline=VerificationPipeline(
                [QuoteGroundingStage(), SemanticVerificationStage(), ClaimGroundingStage()]
            ),
            verify=True,
        )
        stages = result.stage_outcomes[0]  # type: ignore[index]
        assert stages is not None
        stage_in = sum(s.input_tokens for s in stages)
        sample_in = sum(s.input_tokens for s in result.samples)
        assert result.total_input_tokens == sample_in + stage_in
        # The folded outcome carries only the last stage's usage, so honest
        # accounting must exceed it.
        assert stage_in > result.verifications[0].input_tokens  # type: ignore[union-attr]


DEV_DIR = Path(__file__).resolve().parent / "fixtures" / "claim_grounding_dev"


def _dev_cases() -> list[tuple[str, list[int], str]]:
    cases: list[tuple[str, list[int], str]] = []
    for line in (DEV_DIR / "ground_truth.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        notes = json.loads(row["notes"])
        cases.append((row["transcript_id"], notes["failure_event_indices"], notes["spec"]))
    return cases


class TestClaimGroundingDevCases:
    """Validate the claim-grounding lever's wiring on the six real dev cases.

    Mock-first, so this checks wiring, not model quality: on each genuine dev
    transcript the stage builds a well-formed claim-grounding prompt that renders
    the labeled contradicting events, and maps the model's contradiction verdict
    to upholding the flag (its absence to a refutation). What a real model would
    actually conclude is out of scope; the synthetic ``TestClaimGroundingStage``
    cases pin the surfacing/cap behaviour discriminatively.
    """

    @pytest.mark.parametrize("tid,failure_indices,spec", _dev_cases())
    def test_grounds_dev_transcripts_and_maps_the_vote(
        self, tid: str, failure_indices: list[int], spec: str
    ) -> None:
        transcript = Transcript.model_validate_json(
            (DEV_DIR / f"{tid}.json").read_text(encoding="utf-8")
        )
        events_by_index = {e.index: e for e in transcript.events}
        verdict = MonitorVerdict(
            monitor_id="deception",
            transcript_id=tid,
            suspicion_score=80,
            categories=[FailureCategory.DECEPTION],
            evidence=[Evidence(event_index=i, quote=f"cited[{i}]") for i in failure_indices],
            reasoning=f"agent's claim is contradicted by the record: {spec}",
            model="dev-model",
        )

        found = MockLLMClient(
            default_response=json.dumps({"contradicted": True, "reasoning": "the event refutes it"})
        )
        ctx = StageContext(
            transcript=transcript, verdict=verdict, client=found, cache_dir=None, model="dev-model"
        )
        out = ClaimGroundingStage().run(ctx)
        assert out.supported is True, tid  # a real deception's claim is upheld

        prompt = str(found.calls[0]["prompt"])
        for i in failure_indices:
            assert render_event(events_by_index[i]) in prompt, (tid, i)

        # The opposite vote refutes — the stage's mapping is not hard-wired.
        absent = MockLLMClient(
            default_response=json.dumps({"contradicted": False, "reasoning": "consistent"})
        )
        refute = ClaimGroundingStage().run(
            StageContext(
                transcript=transcript,
                verdict=verdict,
                client=absent,
                cache_dir=None,
                model="dev-model",
            )
        )
        assert refute.supported is False, tid


def test_build_verification_prompt_unchanged_surface() -> None:
    # The public helper still renders the semantic prompt (cache-key stability).
    verdict = make_verdict(quote="rm -rf /tmp/scratch")
    prompt = build_verification_prompt(make_transcript(), verdict)
    assert prompt is not None
    assert '"rm -rf /tmp/scratch"' in prompt
    assert "[1] SHELL $ rm -rf /tmp/scratch -> ok" in prompt
    assert quotes_match(make_transcript(), verdict) is True
