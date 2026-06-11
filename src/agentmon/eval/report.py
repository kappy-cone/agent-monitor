"""Render an EvalReport to disk as results.json and a human-readable report.md."""

from __future__ import annotations

from pathlib import Path

from agentmon.schemas import EvalReport, MonitorEvalResult

_INTRO = (
    "Recall figures use a false-alarm-budget framing: pick the score threshold that "
    "flags at most the budgeted fraction of benign transcripts (e.g. 1% FPR), then "
    "report the fraction of true failures caught at that threshold. This mirrors "
    "deployment, where the capacity to review false alarms is the binding constraint."
)


def _fmt(value: float | None) -> str:
    """Format a float to 3 decimals, with an em-dash for None."""
    return "—" if value is None else f"{value:.3f}"


def _monitor_section(result: MonitorEvalResult) -> str:
    tm = result.threshold_metrics
    rows = [
        (
            "Transcripts (n / positive / benign)",
            f"{result.n_transcripts} / {result.n_positive} / {result.n_benign}",
        ),
        ("Verdict parse failures (scored 0)", str(result.n_parse_failures)),
        ("AUROC", _fmt(result.auroc)),
        ("Recall @ 1% FPR", _fmt(result.recall_at_1pct_fpr)),
        ("Recall @ 5% FPR", _fmt(result.recall_at_5pct_fpr)),
        (f"Precision @ threshold {tm.threshold}", _fmt(tm.precision)),
        (f"Recall @ threshold {tm.threshold}", _fmt(tm.recall)),
        (f"F1 @ threshold {tm.threshold}", _fmt(tm.f1)),
        ("Mean input tokens", _fmt(result.mean_input_tokens)),
        ("Mean output tokens", _fmt(result.mean_output_tokens)),
        ("Mean latency (ms)", _fmt(result.mean_latency_ms)),
        ("Mean cost (USD)", _fmt(result.mean_cost_usd)),
    ]
    lines = [f"## {result.monitor_id}", "", "| Metric | Value |", "| --- | --- |"]
    lines += [f"| {name} | {value} |" for name, value in rows]
    return "\n".join(lines)


def write_report(report: EvalReport, out_dir: Path) -> tuple[Path, Path]:
    """Write results.json and report.md into out_dir; return (json_path, md_path)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "results.json"
    md_path = out_dir / "report.md"
    json_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    sections = [_monitor_section(result) for result in report.results]
    md = "\n\n".join(["# Monitor evaluation report", _INTRO, *sections]) + "\n"
    md_path.write_text(md, encoding="utf-8")
    return json_path, md_path
