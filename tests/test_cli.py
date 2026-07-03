"""Tests for the agentmon CLI.

Every ``run`` invocation passes an explicit ``--cache-dir`` under ``tmp_path``
so the tests never touch the real ``.agentmon_cache`` directory.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentmon import calibration
from agentmon.calibration import sample_cache_key
from agentmon.cli import app
from agentmon.ingest.claude_code import parse_session_file
from agentmon.monitors.registry import get_monitor
from agentmon.schemas import CalibratedVerdict, MonitorVerdict, Transcript

FIXTURE = Path(__file__).parent / "fixtures" / "claude_code_session.jsonl"
SAMPLES = Path(__file__).parent.parent / "datasets" / "samples"

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


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


def test_run_pins_the_calibrated_k1_engine_binding(tmp_path: Path) -> None:
    """The CLI writes exactly the k=1 / temperature-0 / attempt-0 sample entry.

    Any drift in cli.py's ``run_calibrated`` binding to k > 1 or a nonzero
    temperature would add or rekey cache entries and break the set equality
    below, so this pins the engine contract through the real command rather
    than a re-issued library call. (verify=True is invisible to the cache with
    the stock mock client — its benign verdict never triggers verification —
    so the verify binding is pinned separately by the spy test below.)
    """
    result = runner.invoke(app, run_args(FIXTURE, tmp_path))
    assert result.exit_code == 0

    monitor = get_monitor("example_security")
    expected = sample_cache_key(
        monitor, parse_session_file(FIXTURE), 0, 0.0, model=monitor.config.model
    )
    cache_files = {path.stem for path in (tmp_path / "cache" / "mock").glob("*.json")}
    assert cache_files == {expected}


def test_run_binds_the_engine_at_k1_temp0_no_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run command issues exactly one run_calibrated call with the pinned kwargs."""
    seen: list[dict[str, object]] = []
    real_run_calibrated = calibration.run_calibrated

    def spy(*args: Any, **kwargs: Any) -> list[CalibratedVerdict]:
        seen.append(dict(kwargs))
        return real_run_calibrated(*args, **kwargs)

    monkeypatch.setattr("agentmon.cli.run_calibrated", spy)
    result = runner.invoke(app, run_args(FIXTURE, tmp_path))

    assert result.exit_code == 0
    assert len(seen) == 1
    assert seen[0]["k"] == 1
    assert seen[0]["temperature"] == 0.0
    assert seen[0]["verify"] is False
    assert seen[0]["cache_dir"] == tmp_path / "cache" / "mock"


def test_run_rejects_non_session_jsonl(tmp_path: Path) -> None:
    # A verdicts JSONL is not a session log; refuse instead of running the
    # monitor on an all-noise transcript.
    result = runner.invoke(app, run_args(SAMPLES / "toy_verdicts.jsonl", tmp_path))
    assert result.exit_code != 0
    # Rich wraps error text in a box, so normalize before matching.
    plain = " ".join(result.output.replace("│", " ").split())
    assert "does not look like a Claude Code session log" in plain


def test_report_rejects_missing_run_dir(tmp_path: Path) -> None:
    # Force a wide render width so Rich does not char-wrap the (long tmp_path)
    # error message and split "summary.json" across a box line under CI's
    # 80-column, forced-color output; strip ANSI before matching for the same
    # reason (CI colorizes; local runs may not).
    result = runner.invoke(app, ["report", str(tmp_path / "no-such-run")], env={"COLUMNS": "300"})
    assert result.exit_code != 0
    plain = " ".join(_ANSI.sub("", result.output).replace("│", " ").split())
    assert "summary.json" in plain
