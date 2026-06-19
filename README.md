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
with three implementations: `AnthropicClient` (live Claude), `OpenAICompatClient` (any
OpenAI-compatible endpoint — used for self-hosted local monitors via vLLM and for Gemini), and
`MockLLMClient` (deterministic, for tests and `--mock` runs).

## Quickstart

```sh
uv sync
uv run pytest
uv run agentmon ingest --source claude-code --path tests/fixtures/claude_code_session.jsonl --out out/transcripts
uv run agentmon run --monitor example_security --input tests/fixtures/claude_code_session.jsonl --mock
uv run agentmon eval --verdicts datasets/samples/toy_verdicts.jsonl --labels datasets/samples/toy_labels.jsonl --out out/eval
```

`--mock` uses the deterministic mock client; drop it and set `ANTHROPIC_API_KEY` for live runs.

## Dataset

`datasets/synthetic/` holds a 133-transcript labeled evaluation set: 60 injected failures
(4 modes × 3 difficulty tiers) and 73 benign — 26 of them hard negatives engineered to
trip a naive monitor (user-requested test deletion, credentials moved *out* of code,
honest failure reports). Failures are injected as local edits into segments of real
Claude Code sessions, with **matched pairs** (the same base appears clean and injected)
so a monitor must key on the failure, not the generator's style. The set ships behind a
two-part leakage gate: dumb baselines (length, keywords, comment density, diff fraction)
must stay under 0.65 AUROC, and a realism catalog checks voice convergence, template
residue, and every matched pair. Methodology, gate results, and known limitations:
[datasets/synthetic/README.md](datasets/synthetic/README.md).

```sh
uv run agentmon run --monitor example_security --input datasets/synthetic/transcripts --mock --out verdicts.jsonl
uv run agentmon eval --verdicts verdicts.jsonl --labels datasets/synthetic/labels.jsonl --out out/eval
```

## Repo layout

```
src/agentmon/
  schemas.py      # pydantic models: Event union, Transcript, MonitorVerdict, eval results
  ingest/         # Claude Code session logs -> normalized Transcript JSON
  monitors/       # monitor loading: prompt parsing, transcript rendering, verdict parsing
  prompts/        # monitor definitions: *.md with YAML frontmatter + prompt body
  llm/            # LLMClient protocol, AnthropicClient, OpenAICompatClient, MockLLMClient
  runner.py       # run monitors x transcripts with an on-disk response cache
  eval/           # score verdicts against labels: AUROC, recall @ fixed FPR, cost
  cli.py          # typer CLI: agentmon ingest | run | eval
datasets/
  samples/        # toy verdict/label sets for the eval pipeline
  synthetic/      # labeled eval dataset + manifest, plans, taxonomy, leakage gate
tests/            # everything runs against MockLLMClient; no network, no API key
```

The five-monitor library (`security_vuln`, `reward_hacking`, `scope_expansion`, `deception`,
`generalist`), calibrated k-sampling with bounded evidence verification, and the dev/test
evaluation now exist; the failure-mode taxonomy lives in
[datasets/synthetic/TAXONOMY.md](datasets/synthetic/TAXONOMY.md) and results in
[FINDINGS.md](FINDINGS.md).

## Roadmap (explicitly out of scope today)

- Hierarchical monitoring (cheap first pass, escalate suspicious transcripts)
- Automated red-teaming of monitors
- Live monitoring via Claude Code hooks
- Fine-tuned open-source monitors

## License

MIT
