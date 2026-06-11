# agentmon

LLM-as-judge monitoring for coding-agent transcripts.

agentmon is a trusted-monitoring harness in the spirit of AI-control work (Greenblatt et al. 2023):
a trusted monitor model reads the full transcript of a coding-agent session and emits a structured
suspicion verdict — a 0–100 suspicion score, flagged failure categories (`security_vuln`,
`reward_hacking`, `scope_expansion`, `deception`, plus an `other` catch-all), and event-indexed
evidence quotes pointing back into the transcript. Monitors are plain markdown prompts; the harness handles ingestion, running
monitors over transcript sets, and scoring the results.

Evaluation is against labeled transcripts (benign vs. a failure-mode tag). The headline metric is
catch rate at a fixed false-positive budget — recall @ 1% and 5% FPR: with a threshold calibrated
to flag only 1% of benign transcripts, what fraction of true failures do we catch? The report also
includes AUROC, precision/recall/F1 at a fixed threshold, and cost/latency per transcript, since a
monitor that's accurate but too expensive to run on every session isn't deployable. agentmon
ingests real Claude Code session logs, and the entire test suite runs against a deterministic mock
LLM — no API key needed for `uv run pytest`.

## Architecture

```
session logs (~/.claude/projects)
        |
        v
     ingest  ──>  Transcript JSON (one file per session)
                       |
                       v
                    runner  (monitors x transcripts, on-disk response cache)
                       |
                       v
                MonitorVerdicts ──> eval ──> results.json + report.md
```

Monitors are defined as `prompts/*.md` files with YAML frontmatter (id, model, categories, …) and
a `{{transcript}}` placeholder in the body. All LLM access goes through an `LLMClient` protocol
with two implementations: `AnthropicClient` (live) and `MockLLMClient` (deterministic, for tests
and `--mock` runs).

## Quickstart

```sh
uv sync
uv run pytest
uv run agentmon ingest --source claude-code --path tests/fixtures/claude_code_session.jsonl --out out/transcripts
uv run agentmon run --monitor example_security --input tests/fixtures/claude_code_session.jsonl --mock
uv run agentmon eval --verdicts datasets/samples/toy_verdicts.jsonl --labels datasets/samples/toy_labels.jsonl --out out/eval
```

`--mock` uses the deterministic mock client; drop it and set `ANTHROPIC_API_KEY` for live runs.

## Repo layout

```
src/agentmon/
  schemas.py      # pydantic models: Event union, Transcript, MonitorVerdict, eval results
  ingest/         # Claude Code session logs -> normalized Transcript JSON
  monitors/       # monitor loading: prompt parsing, transcript rendering, verdict parsing
  prompts/        # monitor definitions: *.md with YAML frontmatter + prompt body
  llm/            # LLMClient protocol, AnthropicClient, MockLLMClient
  runner.py       # run monitors x transcripts with an on-disk response cache
  eval/           # score verdicts against labels: AUROC, recall @ fixed FPR, cost
  cli.py          # typer CLI: agentmon ingest | run | eval
datasets/         # sample verdict/label sets for the eval pipeline
tests/            # everything runs against MockLLMClient; no network, no API key
```

## Roadmap (explicitly out of scope today)

- Real monitor prompt library and failure-mode taxonomy docs
- Synthetic dataset generation (injecting failure modes into benign transcripts)
- Hierarchical monitoring (cheap first pass, escalate suspicious transcripts)
- Automated red-teaming of monitors
- Live monitoring via Claude Code hooks
- Fine-tuned open-source monitors

## License

MIT
