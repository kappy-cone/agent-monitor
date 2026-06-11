"""write_report output: file layout, JSON round-trip, and markdown contents."""

from __future__ import annotations

from pathlib import Path

from agentmon.eval.metrics import evaluate
from agentmon.eval.report import write_report
from agentmon.schemas import EvalReport, LabeledTranscript, MonitorVerdict

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "datasets" / "samples"


def _toy_report() -> EvalReport:
    verdict_lines = (SAMPLES_DIR / "toy_verdicts.jsonl").read_text().splitlines()
    label_lines = (SAMPLES_DIR / "toy_labels.jsonl").read_text().splitlines()
    verdicts = [MonitorVerdict.model_validate_json(line) for line in verdict_lines if line]
    labels = [LabeledTranscript.model_validate_json(line) for line in label_lines if line]
    return evaluate(verdicts, labels, threshold=50)


def test_write_report_creates_both_files(tmp_path: Path) -> None:
    report = _toy_report()
    json_path, md_path = write_report(report, tmp_path / "out")
    assert json_path == tmp_path / "out" / "results.json"
    assert md_path == tmp_path / "out" / "report.md"
    assert json_path.is_file()
    assert md_path.is_file()


def test_results_json_round_trips(tmp_path: Path) -> None:
    report = _toy_report()
    json_path, _ = write_report(report, tmp_path / "out")
    assert EvalReport.model_validate_json(json_path.read_text()) == report


def test_report_md_contents(tmp_path: Path) -> None:
    report = _toy_report()
    _, md_path = write_report(report, tmp_path / "out")
    md = md_path.read_text()
    assert "example_security" in md
    assert "1% FPR" in md
    # Toy AUROC is 31/32 = 0.96875, formatted to 3 decimals.
    assert "0.969" in md


def test_write_report_creates_nested_dirs_and_handles_empty(tmp_path: Path) -> None:
    json_path, md_path = write_report(EvalReport(), tmp_path / "a" / "b")
    assert json_path.is_file()
    assert md_path.is_file()
    assert EvalReport.model_validate_json(json_path.read_text()) == EvalReport()
