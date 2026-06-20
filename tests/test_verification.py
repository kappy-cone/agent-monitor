"""Tests for the verification pipeline and its default stages.

Phase 1 (default pipeline) behaviour is pinned byte-for-byte against the
committed test matrix by ``test_verification_golden.py``; here we cover the stage
seam directly — the default two stages and the fold rule — all mock-first.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentmon.calibration import run_calibrated, verify_sample
from agentmon.llm.client import LLMResponse
from agentmon.llm.mock import MockLLMClient
from agentmon.monitors.base import Monitor, MonitorConfig
from agentmon.schemas import (
    Evidence,
    FailureCategory,
    MonitorVerdict,
    ShellCommand,
    Transcript,
    UserMessage,
)
from agentmon.verification import (
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


class TestOptInPipelineThroughRunCalibrated:
    def test_default_pipeline_leaves_stage_outcomes_none(self) -> None:
        client = SequenceLLMClient(
            [verdict_json(80, ["security_vuln"]), json.dumps({"supported": True})]
        )
        [result] = run_calibrated([make_monitor()], [make_transcript()], client, k=1, verify=True)
        assert result.stage_outcomes is None  # default path stays byte-identical
        assert result.verifications[0] is not None

    def test_stage_outcomes_parallel_with_none_for_unflagged(self) -> None:
        # An explicit (opt-in) pipeline records per-stage detail, parallel to the
        # samples, with None where a sample was not verified.
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


def test_build_verification_prompt_unchanged_surface() -> None:
    # The public helper still renders the semantic prompt (cache-key stability).
    verdict = make_verdict(quote="rm -rf /tmp/scratch")
    prompt = build_verification_prompt(make_transcript(), verdict)
    assert prompt is not None
    assert '"rm -rf /tmp/scratch"' in prompt
    assert "[1] SHELL $ rm -rf /tmp/scratch -> ok" in prompt
    assert quotes_match(make_transcript(), verdict) is True
