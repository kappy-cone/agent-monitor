"""Tests for the agentmon CLI.

Every ``run`` invocation passes an explicit ``--cache-dir`` under ``tmp_path``
so the tests never touch the real ``.agentmon_cache`` directory.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from agentmon.cli import app
from agentmon.schemas import MonitorVerdict, Transcript

FIXTURE = Path(__file__).parent / "fixtures" / "claude_code_session.jsonl"
SAMPLES = Path(__file__).parent.parent / "datasets" / "samples"

runner = CliRunner()


def run_args(input_path: Path, tmp_path: Path, *extra: str) -> list[str]:
    """CLI args for a mocked ``run`` with its cache confined to tmp_path."""
    return [
        "run",
        "--monitor",
        "example_security",
        "--input",
        str(input_path),
        "--mock",
        "--cache-dir",
        str(tmp_path / "cache"),
        *extra,
    ]


def test_ingest_writes_transcript_json_and_summary(tmp_path: Path) -> None:
    out_dir = tmp_path / "transcripts"

    result = runner.invoke(app, ["ingest", "--path", str(FIXTURE), "--out", str(out_dir)])

    assert result.exit_code == 0
    files = list(out_dir.glob("*.json"))
    assert len(files) == 1
    transcript = Transcript.model_validate_json(files[0].read_text(encoding="utf-8"))
    assert transcript.id == "claude_code_session"
    assert transcript.events
    assert f"claude_code_session: {len(transcript.events)} events" in result.output
    assert "Ingested 1 transcript(s)" in result.output


def test_ingest_rejects_unknown_source(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["ingest", "--source", "cursor", "--path", str(FIXTURE), "--out", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert "unsupported source" in result.output


def test_run_mock_on_session_jsonl(tmp_path: Path) -> None:
    result = runner.invoke(app, run_args(FIXTURE, tmp_path))

    assert result.exit_code == 0
    assert "suspicion_score" in result.output
    assert "example_security" in result.output


def test_run_on_ingested_json_transcript(tmp_path: Path) -> None:
    out_dir = tmp_path / "transcripts"
    ingested = runner.invoke(app, ["ingest", "--path", str(FIXTURE), "--out", str(out_dir)])
    assert ingested.exit_code == 0

    result = runner.invoke(app, run_args(out_dir / "claude_code_session.json", tmp_path))

    assert result.exit_code == 0
    assert "suspicion_score" in result.output


def test_run_out_writes_verdict_jsonl(tmp_path: Path) -> None:
    out_file = tmp_path / "verdicts.jsonl"

    result = runner.invoke(app, run_args(FIXTURE, tmp_path, "--out", str(out_file)))

    assert result.exit_code == 0
    lines = out_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    verdict = MonitorVerdict.model_validate_json(lines[0])
    assert verdict.monitor_id == "example_security"
    assert verdict.transcript_id == "claude_code_session"


def test_run_unknown_monitor_lists_available(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["run", "--monitor", "nope", "--input", str(FIXTURE), "--mock", "--no-cache"]
    )

    assert result.exit_code != 0
    assert "unknown monitor" in result.output
    assert "example_security" in result.output


def test_eval_writes_report_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "report"

    result = runner.invoke(
        app,
        [
            "eval",
            "--verdicts",
            str(SAMPLES / "toy_verdicts.jsonl"),
            "--labels",
            str(SAMPLES / "toy_labels.jsonl"),
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 0
    assert (out_dir / "report.md").exists()
    assert (out_dir / "results.json").exists()
    assert "example_security: auroc=" in result.output


def test_run_on_directory_of_transcripts(tmp_path: Path) -> None:
    ingest_out = tmp_path / "transcripts"
    result = runner.invoke(app, ["ingest", "--path", str(FIXTURE), "--out", str(ingest_out)])
    assert result.exit_code == 0
    result = runner.invoke(app, run_args(ingest_out, tmp_path))
    assert result.exit_code == 0
    assert "suspicion_score" in result.output


def test_mock_runs_use_a_separate_cache_namespace(tmp_path: Path) -> None:
    result = runner.invoke(app, run_args(FIXTURE, tmp_path))
    assert result.exit_code == 0
    # Mock responses land under cache/mock, never in the shared live cache.
    cache_dir = tmp_path / "cache"
    assert list((cache_dir / "mock").glob("*.json"))
    assert not list(cache_dir.glob("*.json"))


def test_run_rejects_non_session_jsonl(tmp_path: Path) -> None:
    # A verdicts JSONL is not a session log; refuse instead of running the
    # monitor on an all-noise transcript.
    result = runner.invoke(app, run_args(SAMPLES / "toy_verdicts.jsonl", tmp_path))
    assert result.exit_code != 0
    # Rich wraps error text in a box, so normalize before matching.
    plain = " ".join(result.output.replace("│", " ").split())
    assert "does not look like a Claude Code session log" in plain
