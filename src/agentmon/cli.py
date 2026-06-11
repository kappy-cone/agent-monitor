"""Typer CLI: ingest session logs, run monitors, and evaluate verdicts."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel, ValidationError

from agentmon.eval.metrics import evaluate
from agentmon.eval.report import write_report
from agentmon.ingest.claude_code import ingest_path, parse_session_file
from agentmon.llm.client import AnthropicClient, LLMClient
from agentmon.llm.mock import MockLLMClient
from agentmon.monitors.registry import get_monitor
from agentmon.runner import run_monitors
from agentmon.schemas import LabeledTranscript, MonitorVerdict, Transcript

app = typer.Typer(
    help="LLM-as-judge monitoring for coding-agent transcripts.",
    no_args_is_help=True,
)

_SUPPORTED_SOURCE = "claude-code"
_DEFAULT_CACHE_DIR = Path(".agentmon_cache")


def _load_transcripts(path: Path) -> list[Transcript]:
    """Load transcripts from a session ``.jsonl``, an ingested ``.json``, or a directory."""
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if p.is_file() and p.suffix in (".json", ".jsonl"))
        if not files:
            raise typer.BadParameter(
                f"no .json or .jsonl files found in directory (not searched recursively): {path}",
                param_hint="--input",
            )
        return [transcript for file in files for transcript in _load_transcripts(file)]
    if not path.is_file():
        raise typer.BadParameter(f"file does not exist: {path}", param_hint="--input")
    if path.suffix == ".jsonl":
        transcript = parse_session_file(path)
        if all(event.kind == "other" for event in transcript.events):
            # Most likely not a session log at all (e.g. a verdicts JSONL):
            # refuse rather than burn LLM calls on an all-noise transcript.
            raise typer.BadParameter(
                f"{path} does not look like a Claude Code session log "
                "(no conversation events found)",
                param_hint="--input",
            )
        return [transcript]
    if path.suffix == ".json":
        try:
            return [Transcript.model_validate_json(path.read_text(encoding="utf-8"))]
        except ValidationError as exc:
            raise typer.BadParameter(
                f"{path} is not a valid Transcript: {exc}", param_hint="--input"
            ) from exc
    raise typer.BadParameter(
        f"unsupported input {path.name!r}: expected a .jsonl session log or a .json transcript",
        param_hint="--input",
    )


def _read_jsonl[ModelT: BaseModel](
    path: Path, model: type[ModelT], param_hint: str
) -> list[ModelT]:
    """Parse one ``model`` instance per non-empty line of a JSONL file."""
    if not path.is_file():
        raise typer.BadParameter(f"file does not exist: {path}", param_hint=param_hint)
    items: list[ModelT] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            items.append(model.model_validate_json(line))
        except ValidationError as exc:
            raise typer.BadParameter(
                f"{path}, line {line_no}: invalid {model.__name__}: {exc}",
                param_hint=param_hint,
            ) from exc
    return items


def _fmt(value: float | None) -> str:
    """Format a metric to 3 decimals, with ``n/a`` for undefined values."""
    return "n/a" if value is None else f"{value:.3f}"


@app.command()
def ingest(
    path: Annotated[
        Path,
        typer.Option(help="A session .jsonl file, or a directory of session .jsonl files."),
    ],
    out: Annotated[
        Path,
        typer.Option(help="Output directory; one <transcript_id>.json is written per transcript."),
    ],
    source: Annotated[
        str,
        typer.Option(help=f"Source log format; only {_SUPPORTED_SOURCE!r} is supported."),
    ] = _SUPPORTED_SOURCE,
) -> None:
    """Normalize agent session logs into Transcript JSON files."""
    if source != _SUPPORTED_SOURCE:
        raise typer.BadParameter(
            f"unsupported source {source!r}; supported sources: {_SUPPORTED_SOURCE}",
            param_hint="--source",
        )
    if not path.exists():
        raise typer.BadParameter(f"path does not exist: {path}", param_hint="--path")
    transcripts = ingest_path(path)
    out.mkdir(parents=True, exist_ok=True)
    for transcript in transcripts:
        out_file = out / f"{transcript.id}.json"
        out_file.write_text(transcript.model_dump_json(indent=2) + "\n", encoding="utf-8")
        typer.echo(f"{transcript.id}: {len(transcript.events)} events -> {out_file}")
    typer.echo(f"Ingested {len(transcripts)} transcript(s) into {out}")


@app.command()
def run(
    monitor: Annotated[
        str,
        typer.Option(help="Monitor id, resolved against the bundled prompt library."),
    ],
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            help="A session .jsonl file, an ingested Transcript .json file, "
            "or a directory of either (non-recursive).",
        ),
    ],
    mock: Annotated[
        bool,
        typer.Option(
            "--mock", help="Use the deterministic mock LLM client instead of the Anthropic API."
        ),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option(help="Append each verdict to this JSONL file."),
    ] = None,
    cache_dir: Annotated[
        Path,
        typer.Option(help="Directory for the on-disk LLM response cache."),
    ] = _DEFAULT_CACHE_DIR,
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="Disable the response cache."),
    ] = False,
) -> None:
    """Run one monitor over the given transcript(s) and print each verdict.

    Without --mock this calls the Anthropic API, which requires the
    ANTHROPIC_API_KEY environment variable to be set.
    """
    try:
        resolved = get_monitor(monitor)
    except KeyError as exc:
        typer.echo(f"Error: {exc.args[0]}", err=True)
        raise typer.Exit(1) from exc
    transcripts = _load_transcripts(input_path)

    client: LLMClient
    if mock:
        client = MockLLMClient()
    else:
        try:
            client = AnthropicClient()
        except Exception as exc:
            typer.echo(
                f"Error: could not create the Anthropic client (is ANTHROPIC_API_KEY set?): {exc}",
                err=True,
            )
            raise typer.Exit(1) from exc

    # Mock responses are cached under their own namespace so a later live run
    # can never be served a canned mock verdict from the shared cache.
    effective_cache = None if no_cache else (cache_dir / "mock" if mock else cache_dir)
    verdicts = run_monitors([resolved], transcripts, client, cache_dir=effective_cache)
    for verdict in verdicts:
        typer.echo(verdict.model_dump_json(indent=2))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as handle:
            for verdict in verdicts:
                handle.write(verdict.model_dump_json() + "\n")
        typer.echo(f"Appended {len(verdicts)} verdict(s) to {out}")


@app.command(name="eval")
def eval_(
    verdicts: Annotated[
        Path,
        typer.Option(help="JSONL file with one MonitorVerdict per line."),
    ],
    labels: Annotated[
        Path,
        typer.Option(help="JSONL file with one LabeledTranscript per line."),
    ],
    out: Annotated[
        Path,
        typer.Option(help="Output directory for results.json and report.md."),
    ],
    threshold: Annotated[
        int,
        typer.Option(help="Suspicion-score threshold for the confusion-matrix metrics."),
    ] = 50,
) -> None:
    """Score monitor verdicts against ground-truth labels and write a report."""
    verdict_models = _read_jsonl(verdicts, MonitorVerdict, "--verdicts")
    label_models = _read_jsonl(labels, LabeledTranscript, "--labels")
    report = evaluate(verdict_models, label_models, threshold=threshold)
    json_path, md_path = write_report(report, out)
    typer.echo(f"Wrote {md_path} and {json_path}")
    for result in report.results:
        typer.echo(
            f"{result.monitor_id}: auroc={_fmt(result.auroc)} "
            f"recall@1%fpr={_fmt(result.recall_at_1pct_fpr)} "
            f"recall@5%fpr={_fmt(result.recall_at_5pct_fpr)}"
        )
