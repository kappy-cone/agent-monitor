---
name: regen-dataset
description: Rebuild the synthetic evaluation set offline and run it through the two-part leakage gate before it ships. Use when regenerating or editing datasets/synthetic/ — new edit plans, changed manifest, rebalanced rows, or any request to "rebuild/regenerate the dataset", "run the leakage gate", or "re-gate the data".
---

# Regenerate the synthetic dataset

The evaluation set must rebuild **deterministically and offline**, and must pass the **two-part
leakage gate** before any regenerated data is used. This skill runs that discipline end to end.
Domain terms (base, injection, edit plan, matched pair, leakage gate, dumb baseline, realism
catalog) are defined in [CONTEXT.md](../../../CONTEXT.md). Conventions are in
[CLAUDE.md](../../../CLAUDE.md).

Run everything from the repo root with `uv` (per CLAUDE.md). If a pinned-Python/lockfile clash
blocks a run, fall back to `env -u UV_PYTHON uv run --no-sync python …`.

## Hard rules (do not skip)

- **Never generate whole transcripts from scratch.** Injections are deterministic edits to real
  bases via committed edit plans. The base supplies all style and texture.
- **Style profile is a hard constraint.** Every injected event must match its base's register,
  diff format, error-output format, and texture (`datasets/synthetic/STYLE_PROFILE.md`). A
  synthetic tell is a bug.
- **Both halves of a matched pair go through the identical pipeline.** If the envelope differs,
  the envelope leaks the label.
- **Provenance never lives in the transcript** — only in the label's notes. Hard negatives are
  labeled `benign`.
- **The data does not ship until the full gate passes** and results are recorded.

## Workflow

### 1. Rebuild (offline, no API key)

```sh
# optional: emit numbered renders of every clean base (for authoring/inspecting plans)
uv run python datasets/synthetic/generate.py --render-bases

# build all transcripts + labels from the manifest and committed edit plans
uv run python datasets/synthetic/generate.py

# rebuild only specific rows while iterating
uv run python datasets/synthetic/generate.py --only sec-bla-01,sec-med-01
```

`generate.py` reads `manifest.yaml`, slices and scrubs the real bases, normalizes uniformly
(strip raw payloads, uniform source label, bare diffs, capped fields), then applies each committed
plan in `plans/<id>.json`. It self-checks: a meta-label scan (no "inject/synthetic/cwe-/…"
strings), a post-scrub deny-list (no real identifiers), and render-budget visibility (every
failure-carrying event must be visible within the monitor's budget). **Sources must exist locally**
or the build fails hard — this is expected, not a bug to work around.

> Authoring new plans with a live model needs credentials: `generate.py --author` drafts missing
> plans, then **commit them** so the next rebuild is offline again.

### 2. Gate 4A — dumb baselines (automated)

```sh
uv run python datasets/synthetic/leakage_check.py --json-out out/leakage.json
```

Every dumb baseline's discriminative power must be **< 0.65**. The script exits non-zero and prints
`FAIL` on the offending baseline if not. If it fails, **rebalance rather than thin the failures**
(e.g. the first build's high `file_diff_fraction` was fixed by adding benign-filler rows, not by
removing injections). Re-run until clean.

### 3. Gate 4B — realism catalog (manual audit)

Audit the regenerated set for synthetic tells. Cross-check against `STYLE_PROFILE.md`, `TAXONOMY.md`,
and each row's `spec` in `manifest.yaml`:

- [ ] **Matched-pair fidelity** — every injected diff matches its manifest claim and is byte-faithful.
- [ ] **Voice/texture convergence** — injected messages stay in the base's register (length caps,
      no repeated digression-openers, no invented status tags, no convergence to a generator voice).
- [ ] **Cross-instance clustering** — no reused magic numbers, filenames, fake credentials, ports,
      or error strings where variation is expected; no format tells (unified-diff markers,
      `Error:` prefixes, stray non-null exit codes); fabrications are internally consistent.
- [ ] **No-laundering (security rows)** — every injection traces to a public CWE class and the host
      base's own code, with zero vocabulary from any excluded private session.

### 4. Record + log

- [ ] Update `datasets/synthetic/README.md`: the 4A baseline table (AUROC, power, verdict) with the
      worst-power summary line, the 4B findings and resolutions, and any composition change.
- [ ] Log every ambiguity call in `DECISIONS.md` under the synthetic-dataset section (what was
      ambiguous, how it was resolved, where it's documented).

## Ship checklist

```
[ ] All transcripts build with no error
[ ] Gate 4A: every dumb baseline power < 0.65
[ ] Gate 4B: all four audits complete, findings + resolutions recorded
[ ] Post-scrub deny-list and meta-label scans clean (generate.py passed)
[ ] Every failure-carrying event visible within the monitor render budget
[ ] README.md gate results updated; DECISIONS.md calls logged
```

Note: `generate.py` is a pure deterministic pipeline — it does **not** use `.agentmon_cache/`. Only
delete that cache when re-running monitors, never when rebuilding the dataset.
