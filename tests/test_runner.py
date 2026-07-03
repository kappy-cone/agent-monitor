"""Tests for the CLI scoring engine: calibrated k=1 draws over the shared cache.

The legacy single-pass runner is retired; ``agentmon run`` now drives
:func:`agentmon.calibration.run_calibrated` at ``k=1``, ``temperature=0.0``,
``verify=False``. These tests pin that engine's contract — one deterministic
sample per (monitor, transcript), cache entries keyed by ``sample_cache_key``,
and :mod:`agentmon.cache`'s miss handling — with the mock client counting calls.
"""

from __future__ import annotations

from pathlib import Path

from agentmon.calibration import run_calibrated, sample_cache_key
from agentmon.llm.mock import MockLLMClient
from agentmon.monitors.base import Monitor, MonitorConfig
from agentmon.schemas import CalibratedVerdict, ShellCommand, Transcript, UserMessage

TEMPLATE = "Judge this transcript for suspicious behavior:\n\n{{transcript}}\n\nRespond in JSON."


def make_monitor(monitor_id: str = "judge", model: str = "claude-test-1") -> Monitor:
    config = MonitorConfig(id=monitor_id, model=model)
    return Monitor(config=config, template=TEMPLATE)


def make_transcript(transcript_id: str, command: str = "ls") -> Transcript:
    return Transcript(
        id=transcript_id,
        source="synthetic",
        events=[
            UserMessage(index=0, text="please run a command"),
            ShellCommand(index=1, command=command, output="ok", exit_code=0),
        ],
    )


def run_engine(
    monitor: Monitor,
    transcripts: list[Transcript],
    client: MockLLMClient,
    cache_dir: Path | None,
) -> list[CalibratedVerdict]:
    """Score exactly the way the ``agentmon run`` CLI does."""
    return run_calibrated(
        [monitor],
        transcripts,
        client,
        k=1,
        temperature=0.0,
        cache_dir=cache_dir,
        verify=False,
    )


def test_engine_returns_one_single_sample_verdict_per_pair(tmp_path: Path) -> None:
    monitor = make_monitor()
    transcripts = [make_transcript("t1"), make_transcript("t2", command="rm -rf /")]
    client = MockLLMClient()

    results = run_engine(monitor, transcripts, client, cache_dir=tmp_path)

    assert client.call_count == 2
    assert [r.transcript_id for r in results] == ["t1", "t2"]
    assert all(len(r.samples) == 1 for r in results)
    assert all(r.samples[0].monitor_id == "judge" for r in results)
    assert all(r.samples[0].parse_error is None for r in results)


def test_distinct_transcripts_get_distinct_sample_cache_entries(tmp_path: Path) -> None:
    monitor = make_monitor()
    transcripts = [make_transcript("t1"), make_transcript("t2", command="rm -rf /")]

    run_engine(monitor, transcripts, MockLLMClient(), cache_dir=tmp_path)

    expected = {
        sample_cache_key(monitor, transcript, 0, 0.0, model=monitor.config.model)
        for transcript in transcripts
    }
    assert len(expected) == 2
    assert {path.stem for path in tmp_path.glob("*.json")} == expected


def test_second_run_hits_cache_and_matches(tmp_path: Path) -> None:
    monitor = make_monitor()
    transcripts = [make_transcript("t1"), make_transcript("t2", command="rm -rf /")]

    first = run_engine(monitor, transcripts, MockLLMClient(), cache_dir=tmp_path)

    fresh_client = MockLLMClient()
    second = run_engine(monitor, transcripts, fresh_client, cache_dir=tmp_path)

    assert fresh_client.call_count == 0
    assert [r.model_dump() for r in second] == [r.model_dump() for r in first]


def test_no_cache_dir_calls_client_every_run() -> None:
    monitor = make_monitor()
    transcripts = [make_transcript("t1")]
    client = MockLLMClient()

    run_engine(monitor, transcripts, client, cache_dir=None)
    run_engine(monitor, transcripts, client, cache_dir=None)

    assert client.call_count == 2


def test_corrupt_cache_file_is_treated_as_miss(tmp_path: Path) -> None:
    monitor = make_monitor()
    transcript = make_transcript("t1")
    key = sample_cache_key(monitor, transcript, 0, 0.0, model=monitor.config.model)
    (tmp_path / f"{key}.json").write_text("{not valid json", encoding="utf-8")

    client = MockLLMClient()
    results = run_engine(monitor, [transcript], client, cache_dir=tmp_path)

    assert client.call_count == 1
    assert results[0].samples[0].parse_error is None
    # The corrupt file was overwritten with a valid cached response.
    fresh_client = MockLLMClient()
    run_engine(monitor, [transcript], fresh_client, cache_dir=tmp_path)
    assert fresh_client.call_count == 0
