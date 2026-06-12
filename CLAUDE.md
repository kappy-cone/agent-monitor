# CLAUDE.md

Conventions for working in this repo.

- `src/agentmon/schemas.py` is the contract. Every other module codes against it. If a model needs
  to change, change schemas.py first, deliberately, then update dependents.
- Mock-first: tests NEVER make live API calls or construct `AnthropicClient`. Use
  `agentmon.llm.mock.MockLLMClient`. `uv run pytest` must pass with no `ANTHROPIC_API_KEY` set.
- Monitors live in `src/agentmon/prompts/*.md`: YAML frontmatter (`id`, `description`,
  `categories`, `model`, `max_transcript_tokens`) plus a body containing a `{{transcript}}`
  placeholder.
- The runner caches LLM responses in `.agentmon_cache/`, keyed by
  `hash(prompt, transcript, model, temperature)`. Delete the directory to force re-runs.
- Before finishing any task, run `uv run ruff check`, `uv run ruff format`, and `uv run pytest`.
- Line length is 100. Use `uv` for all commands (`uv run`, `uv sync`).
- Keep it boring: no async, no database, no plugin framework. Small functions, full type hints,
  `from __future__ import annotations`, `pathlib.Path`.

## Synthetic dataset (`datasets/synthetic/`)

- `manifest.yaml` is the build plan; `generate.py` builds only what it declares, applying the
  committed edit plans in `plans/` deterministically — the dataset rebuilds offline, no API key.
- `STYLE_PROFILE.md` is a hard generation constraint: injected content must match the base
  session's register, code style, and texture. Never generate whole transcripts from scratch.
- The two-part leakage gate must pass before any regenerated data ships: `leakage_check.py`
  (every dumb-baseline discriminative power < 0.65) and the realism catalog incl. matched-pair
  checks. Record results in `datasets/synthetic/README.md`.
- Hard negatives are labeled `benign` (the schema's label set is `benign` + the four failure
  modes); ground-truth failure locations live as JSON in each label's `notes`.
- Ambiguity calls go in `DECISIONS.md`.
