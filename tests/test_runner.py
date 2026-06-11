"""Tests for the monitor runner and its on-disk response cache."""

from __future__ import annotations

from pathlib import Path

from agentmon.llm.mock import MockLLMClient
from agentmon.monitors.base import Monitor, MonitorConfig
from agentmon.runner import cache_key, run_monitors
from agentmon.schemas import ShellCommand, Transcript, UserMessage

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


def test_run_monitors_returns_one_verdict_per_pair(tmp_path: Path) -> None:
    monitor = make_monitor()
    transcripts = [make_transcript("t1"), make_transcript("t2", command="rm -rf /")]
    client = MockLLMClient()

    verdicts = run_monitors([monitor], transcripts, client, cache_dir=tmp_path)

    assert client.call_count == 2
    assert [v.transcript_id for v in verdicts] == ["t1", "t2"]
    assert all(v.monitor_id == "judge" for v in verdicts)
    assert all(v.parse_error is None for v in verdicts)


def test_second_run_hits_cache_and_matches(tmp_path: Path) -> None:
    monitor = make_monitor()
    transcripts = [make_transcript("t1"), make_transcript("t2", command="rm -rf /")]

    first = run_monitors([monitor], transcripts, MockLLMClient(), cache_dir=tmp_path)

    fresh_client = MockLLMClient()
    second = run_monitors([monitor], transcripts, fresh_client, cache_dir=tmp_path)

    assert fresh_client.call_count == 0
    assert [v.model_dump() for v in second] == [v.model_dump() for v in first]


def test_no_cache_dir_calls_client_every_run() -> None:
    monitor = make_monitor()
    transcripts = [make_transcript("t1")]
    client = MockLLMClient()

    run_monitors([monitor], transcripts, client, cache_dir=None)
    run_monitors([monitor], transcripts, client, cache_dir=None)

    assert client.call_count == 2


def test_cache_key_changes_with_model() -> None:
    transcript = make_transcript("t1")
    key_a = cache_key(make_monitor(model="claude-test-1"), transcript)
    key_b = cache_key(make_monitor(model="claude-test-2"), transcript)
    assert key_a != key_b


def test_cache_key_changes_with_transcript_content() -> None:
    monitor = make_monitor()
    key_a = cache_key(monitor, make_transcript("t1", command="ls"))
    key_b = cache_key(monitor, make_transcript("t1", command="rm -rf /"))
    assert key_a != key_b


def test_corrupt_cache_file_is_treated_as_miss(tmp_path: Path) -> None:
    monitor = make_monitor()
    transcript = make_transcript("t1")
    cache_file = tmp_path / f"{cache_key(monitor, transcript)}.json"
    cache_file.write_text("{not valid json", encoding="utf-8")

    client = MockLLMClient()
    verdicts = run_monitors([monitor], [transcript], client, cache_dir=tmp_path)

    assert client.call_count == 1
    assert verdicts[0].parse_error is None
    # The corrupt file was overwritten with a valid cached response.
    fresh_client = MockLLMClient()
    run_monitors([monitor], [transcript], fresh_client, cache_dir=tmp_path)
    assert fresh_client.call_count == 0


def test_cache_key_changes_with_max_transcript_tokens() -> None:
    # The key hashes the *built* prompt, so a config change that alters
    # transcript truncation must invalidate the cache.
    transcript = make_transcript("t1", command="x" * 500)
    small = Monitor(
        MonitorConfig(id="judge", model="claude-test-1", max_transcript_tokens=1),
        "Review:\n{{transcript}}",
    )
    large = Monitor(
        MonitorConfig(id="judge", model="claude-test-1", max_transcript_tokens=16000),
        "Review:\n{{transcript}}",
    )
    assert cache_key(small, transcript) != cache_key(large, transcript)
