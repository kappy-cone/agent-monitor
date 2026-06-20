"""Tests for calibrated scoring: sampling, the sample-indexed cache, verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentmon.calibration import (
    calibrated_score,
    run_calibrated,
    sample_cache_key,
)
from agentmon.llm.client import LLMResponse
from agentmon.llm.mock import MockLLMClient
from agentmon.monitors.base import Monitor, MonitorConfig
from agentmon.schemas import ShellCommand, Transcript, UserMessage
from agentmon.verification import build_verification_prompt

TEMPLATE = "Judge this transcript:\n\n{{transcript}}\n\nRespond in JSON."


def make_monitor(monitor_id: str = "judge") -> Monitor:
    return Monitor(MonitorConfig(id=monitor_id, model="claude-test-1"), TEMPLATE)


def make_transcript(transcript_id: str = "t1") -> Transcript:
    return Transcript(
        id=transcript_id,
        source="synthetic",
        events=[
            UserMessage(index=0, text="please run a command"),
            ShellCommand(index=1, command="rm -rf /tmp/scratch", output="ok", exit_code=0),
        ],
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
    """Returns scripted responses in call order; deterministic per-call variation."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append({"prompt": prompt, "model": model, "temperature": temperature})
        text = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        return LLMResponse(text=text, model=model, input_tokens=10, output_tokens=5)


class TestSampleCache:
    def test_k_samples_are_k_distinct_cache_entries(self, tmp_path: Path) -> None:
        # THE cache x sampling footgun test: a key without sample_index would
        # serve one response k times and collapse fraction-flagged to {0, 1}.
        monitor, transcript = make_monitor(), make_transcript()
        keys = {sample_cache_key(monitor, transcript, i, 0.7) for i in range(5)}
        assert len(keys) == 5

        client = MockLLMClient()
        run_calibrated([monitor], [transcript], client, k=5, cache_dir=tmp_path)
        assert client.call_count == 5
        assert len(list(tmp_path.glob("*.json"))) == 5

    def test_key_changes_with_prompt_and_temperature(self) -> None:
        transcript = make_transcript()
        other = Monitor(MonitorConfig(id="judge", model="claude-test-1"), TEMPLATE + " v2")
        base = sample_cache_key(make_monitor(), transcript, 0, 0.7)
        assert sample_cache_key(other, transcript, 0, 0.7) != base
        assert sample_cache_key(make_monitor(), transcript, 0, 0.0) != base

    def test_rerun_is_served_from_cache(self, tmp_path: Path) -> None:
        monitor, transcript = make_monitor(), make_transcript()
        first = run_calibrated([monitor], [transcript], MockLLMClient(), k=3, cache_dir=tmp_path)
        fresh = MockLLMClient()
        second = run_calibrated([monitor], [transcript], fresh, k=3, cache_dir=tmp_path)
        assert fresh.call_count == 0
        assert second[0].model_dump() == first[0].model_dump()

    def test_k1_is_prefix_of_k5_cache(self, tmp_path: Path) -> None:
        # Sample 0 is shared, so a k=1 smoke run seeds the k=5 run.
        monitor, transcript = make_monitor(), make_transcript()
        run_calibrated([monitor], [transcript], MockLLMClient(), k=1, cache_dir=tmp_path)
        client = MockLLMClient()
        run_calibrated([monitor], [transcript], client, k=5, cache_dir=tmp_path)
        assert client.call_count == 4


class TestCalibratedScoring:
    def test_fraction_flagged_and_epsilon(self) -> None:
        responses = [
            verdict_json(80, ["security_vuln"]),
            verdict_json(20, []),
            verdict_json(90, ["security_vuln"]),
            verdict_json(10, []),
            verdict_json(70, ["security_vuln"]),
        ]
        client = SequenceLLMClient(responses)
        [result] = run_calibrated([make_monitor()], [make_transcript()], client, k=5)
        assert result.fraction_flagged == pytest.approx(0.6)
        assert result.mean_suspicion == pytest.approx(54.0)
        assert result.calibrated_score == pytest.approx(0.6 + 0.54 * 1e-3)
        assert result.sample_flagged == [True, False, True, False, True]
        assert result.verifications == [None] * 5

    def test_epsilon_never_crosses_fraction_gap(self) -> None:
        # Highest epsilon (mean suspicion 100) must rank below the next fraction.
        assert calibrated_score(0.4, 100.0) < calibrated_score(0.6, 0.0)

    def test_k_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="k must be positive"):
            run_calibrated([make_monitor()], [make_transcript()], MockLLMClient(), k=0)


class TestParseHardening:
    def test_parse_failure_retries_once_and_recovers(self) -> None:
        client = SequenceLLMClient(["not json at all", verdict_json(50, ["deception"])])
        [result] = run_calibrated([make_monitor()], [make_transcript()], client, k=1)
        assert client.call_count == 2  # original draw + one redraw
        assert result.sample_flagged == [True]
        assert result.samples[0].parse_error is None
        assert result.parse_retries == 1
        assert result.parse_repairs == 0
        # The discarded first draw's usage is kept in the extras.
        assert result.extra_input_tokens == 10
        assert result.extra_output_tokens == 5

    def test_repair_recovers_after_failed_retry(self) -> None:
        responses = ["garbage one", "garbage two", verdict_json(70, ["deception"])]
        client = SequenceLLMClient(responses)
        [result] = run_calibrated([make_monitor()], [make_transcript()], client, k=1)
        assert client.call_count == 3  # draw + redraw + repair
        # The repair call receives the malformed text, not the transcript prompt.
        assert "garbage two" in str(client.calls[2]["prompt"])
        assert result.sample_flagged == [True]
        assert result.parse_retries == 1
        assert result.parse_repairs == 1
        assert result.extra_input_tokens == 20  # both discarded draws

    def test_unrecoverable_sample_keeps_parse_error_and_counts_unflagged(self) -> None:
        client = SequenceLLMClient(["junk"])  # every call returns junk
        [result] = run_calibrated([make_monitor()], [make_transcript()], client, k=1)
        assert client.call_count == 3
        assert result.samples[0].parse_error is not None
        assert result.sample_flagged == [False]
        assert result.parse_retries == 1
        assert result.parse_repairs == 1
        # Extras: the first discarded draw plus the failed repair call.
        assert result.extra_input_tokens == 20

    def test_retry_uses_a_distinct_cache_slot(self, tmp_path: Path) -> None:
        client = SequenceLLMClient(["not json", verdict_json(50, ["deception"])])
        run_calibrated([make_monitor()], [make_transcript()], client, k=1, cache_dir=tmp_path)
        # Two sample entries (attempt 0 and attempt 1) — a shared slot would
        # have served the cached garbage back to the retry.
        assert len(list(tmp_path.glob("*.json"))) == 2


class TestModelOverride:
    def test_override_changes_cache_key(self) -> None:
        monitor, transcript = make_monitor(), make_transcript()
        primary = sample_cache_key(monitor, transcript, 0, 0.7)
        overridden = sample_cache_key(monitor, transcript, 0, 0.7, model="qwen/test")
        explicit_primary = sample_cache_key(monitor, transcript, 0, 0.7, model="claude-test-1")
        assert overridden != primary
        assert explicit_primary == primary

    def test_attempt_changes_cache_key(self) -> None:
        monitor, transcript = make_monitor(), make_transcript()
        assert sample_cache_key(monitor, transcript, 0, 0.7, attempt=0) != sample_cache_key(
            monitor, transcript, 0, 0.7, attempt=1
        )

    def test_override_reaches_client_and_verdict(self) -> None:
        client = MockLLMClient()
        [result] = run_calibrated(
            [make_monitor()], [make_transcript()], client, k=1, model_override="qwen/test"
        )
        assert client.calls[0]["model"] == "qwen/test"
        assert result.model == "qwen/test"
        assert result.samples[0].model == "qwen/test"

    def test_override_run_does_not_reuse_primary_cache(self, tmp_path: Path) -> None:
        monitor, transcript = make_monitor(), make_transcript()
        run_calibrated([monitor], [transcript], MockLLMClient(), k=1, cache_dir=tmp_path)
        fresh = MockLLMClient()
        run_calibrated(
            [monitor], [transcript], fresh, k=1, cache_dir=tmp_path, model_override="qwen/test"
        )
        assert fresh.call_count == 1  # cache miss: the model is part of the key

    def test_override_applies_to_verification(self) -> None:
        responses = [
            verdict_json(80, ["security_vuln"]),
            json.dumps({"supported": True, "reasoning": "ok"}),
        ]
        client = SequenceLLMClient(responses)
        run_calibrated(
            [make_monitor()],
            [make_transcript()],
            client,
            k=1,
            verify=True,
            model_override="qwen/test",
        )
        assert all(call["model"] == "qwen/test" for call in client.calls)


class TestVerification:
    def test_unsupported_flag_flips_to_unflagged(self) -> None:
        responses = [
            verdict_json(80, ["security_vuln"]),  # sample: flagged
            json.dumps({"supported": False, "reasoning": "quote not in event"}),
        ]
        client = SequenceLLMClient(responses)
        [result] = run_calibrated([make_monitor()], [make_transcript()], client, k=1, verify=True)
        assert result.sample_flagged == [False]
        assert result.fraction_flagged == 0.0
        assert result.verifications[0] is not None
        assert result.verifications[0].supported is False
        assert result.verification_enabled

    def test_supported_flag_survives(self) -> None:
        responses = [
            verdict_json(80, ["security_vuln"]),
            json.dumps({"supported": True, "reasoning": "checks out"}),
        ]
        [result] = run_calibrated(
            [make_monitor()], [make_transcript()], SequenceLLMClient(responses), k=1, verify=True
        )
        assert result.sample_flagged == [True]

    def test_unflagged_samples_are_not_verified(self) -> None:
        client = SequenceLLMClient([verdict_json(20, [])])
        [result] = run_calibrated([make_monitor()], [make_transcript()], client, k=1, verify=True)
        assert client.call_count == 1  # no verification call
        assert result.verifications == [None]

    def test_invalid_citation_flips_without_a_call(self) -> None:
        bad_citation = json.dumps(
            {
                "suspicion_score": 80,
                "categories": ["security_vuln"],
                "evidence": [{"event_index": 99, "quote": "nope"}],
                "reasoning": "scripted",
            }
        )
        client = SequenceLLMClient([bad_citation])
        [result] = run_calibrated([make_monitor()], [make_transcript()], client, k=1, verify=True)
        assert client.call_count == 1  # the sample only; no verification call
        assert result.sample_flagged == [False]
        assert result.verifications[0] is not None
        assert result.verifications[0].supported is False
        assert result.verifications[0].input_tokens == 0

    def test_verifier_parse_failure_fails_open(self) -> None:
        responses = [verdict_json(80, ["security_vuln"]), "garbage, not a verdict"]
        [result] = run_calibrated(
            [make_monitor()], [make_transcript()], SequenceLLMClient(responses), k=1, verify=True
        )
        assert result.sample_flagged == [True]
        assert result.verifications[0] is not None
        assert result.verifications[0].parse_error is not None

    def test_verification_calls_are_cached(self, tmp_path: Path) -> None:
        responses = [
            verdict_json(80, ["security_vuln"]),
            json.dumps({"supported": False, "reasoning": "no"}),
        ]
        first = run_calibrated(
            [make_monitor()],
            [make_transcript()],
            SequenceLLMClient(responses),
            k=1,
            verify=True,
            cache_dir=tmp_path,
        )
        fresh = SequenceLLMClient(responses)
        second = run_calibrated(
            [make_monitor()],
            [make_transcript()],
            fresh,
            k=1,
            verify=True,
            cache_dir=tmp_path,
        )
        assert fresh.call_count == 0
        assert second[0].model_dump() == first[0].model_dump()

    def test_verbatim_quote_flip_is_semantic_not_mechanical(self) -> None:
        # The quote is verbatim, so a flip here is semantic non-support.
        responses = [
            verdict_json(80, ["security_vuln"]),
            json.dumps({"supported": False, "reasoning": "event shows benign cleanup"}),
        ]
        [result] = run_calibrated(
            [make_monitor()], [make_transcript()], SequenceLLMClient(responses), k=1, verify=True
        )
        assert result.verifications[0] is not None
        assert result.verifications[0].quote_match is True
        assert result.verifications[0].supported is False

    def test_paraphrased_quote_is_mechanical_mismatch(self) -> None:
        responses = [
            verdict_json(80, ["security_vuln"], quote="removed the scratch directory"),
            json.dumps({"supported": False, "reasoning": "quote not present"}),
        ]
        [result] = run_calibrated(
            [make_monitor()], [make_transcript()], SequenceLLMClient(responses), k=1, verify=True
        )
        assert result.verifications[0] is not None
        assert result.verifications[0].quote_match is False

    def test_verification_prompt_contains_claim_and_events(self) -> None:
        monitor, transcript = make_monitor(), make_transcript()
        responses = [verdict_json(80, ["security_vuln"], quote="rm -rf /tmp/scratch")]
        client = SequenceLLMClient(responses)
        [result] = run_calibrated([monitor], [transcript], client, k=1)
        prompt = build_verification_prompt(transcript, result.samples[0])
        assert prompt is not None
        assert '"rm -rf /tmp/scratch"' in prompt  # the monitor's quote
        assert "[1] SHELL $ rm -rf /tmp/scratch -> ok" in prompt  # the actual event

    def test_totals_include_verification_usage(self) -> None:
        responses = [
            verdict_json(80, ["security_vuln"]),
            json.dumps({"supported": True, "reasoning": "ok"}),
        ]
        [result] = run_calibrated(
            [make_monitor()], [make_transcript()], SequenceLLMClient(responses), k=1, verify=True
        )
        assert result.total_input_tokens == 20  # 10 sample + 10 verification
        assert result.total_output_tokens == 10
