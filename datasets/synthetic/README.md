# Synthetic evaluation dataset

133 labeled coding-agent transcripts for evaluating trusted monitors: 60 injected failures
(4 modes × 3 difficulty tiers × 5 instances), 73 benign — of which 26 are **hard
negatives** engineered to trip a naive monitor and 12 are benign-injected filler. Built
from 39 segments of real, scrubbed Claude Code sessions. How this set was built — and how
its leakage was controlled — is as much the artifact as the data.

## Methodology

**Real bases, local injection, matched pairs.** Nothing here is generated from a blank
prompt. Each transcript starts as a contiguous segment of a real Claude Code session
(`manifest.yaml` → `bases`), scrubbed and normalized. Failures are injected as *local
edits* — small, committed edit plans (`plans/<id>.json`) applied deterministically by
`generate.py`. Wherever a failure is injected, the same base also appears clean: 94
matched pairs. A monitor that separates a pair must be keying on the failure, not the
style — the strongest structural defense against learning the generator instead of the
failure.

**Style profile as a generation constraint.** Before generating anything, the real
sessions were profiled (`STYLE_PROFILE.md`): tool-call conventions, three distinct
assistant voice registers, per-repo code style, session texture (real sessions backtrack,
fail, and re-read — a clean linear narrative is itself a tell), and the empty-thinking
property of all source sessions. Every injection was authored against that profile and
validated for meta-label leaks, scrub deny-list survival, and render-budget visibility of
its failure events.

**Uniform pipeline.** Clean and injected halves go through the identical path: parse with
the production ingest parser → scrub → strip `raw` payloads → uniform
`source="synthetic"` → uniform 4000-char field caps → renumber. Provenance (base id,
class, tier, CWE, injector model, failure event indices) lives in each label's `notes`
JSON, never in the transcript.

**Taxonomy.** `TAXONOMY.md` defines the four failure modes with tiers keyed to *detection
requirement* (blatant = one event suffices; medium = local context; subtle = cross-event
reasoning), every security signature anchored to a public CWE/OWASP class, and the
hard-negative catalog with the disambiguating evidence a competent monitor must use.
The agentic *forms* of the security signatures were additionally seeded from a private
real session under a strict abstraction boundary (see `ORIENTATION.md`); a dedicated
no-laundering audit verifies nothing project-specific surfaced.

## Leakage gate results

### 4A — dumb-baseline AUROC (gate: discriminative power < 0.65)

Each baseline scores transcripts from a cheap surface feature; power is
`max(AUROC, 1 − AUROC)`. Final run over all 133 transcripts:

| Baseline | AUROC | Power | Verdict |
| --- | --- | --- | --- |
| n_events | 0.586 | 0.586 | pass |
| total_chars | 0.600 | 0.600 | pass |
| keyword_hits | 0.523 | 0.523 | pass |
| comment_density_in_diffs | 0.508 | 0.508 | pass |
| file_diff_fraction | 0.642 | 0.642 | pass (thin margin — see below) |
| shell_error_fraction | 0.587 | 0.587 | pass |
| unified_diff_markers | 0.500 | 0.500 | pass (added after 4B; see below) |

The first build **failed** this gate: `file_diff_fraction` separated at 0.662 because
failure injections add diffs while plain benign transcripts keep base rates. Fixed by
adding 12 benign-filler rows (ordinary, in-scope work injected through the identical
machinery) rather than thinning failure content. The post-fix margin on that baseline is
thin (0.642) and is the first thing to improve when the benign pool scales.

Matched-pair deltas are class-balanced (mean +2.3 to +4.9 events per class, median +4):
injection size itself does not encode the label.

### 4B — realism catalog

Four audits over the generated set: matched-pair diffs (every mode×tier cell plus hard
negatives and filler), voice/texture convergence against the style profile,
cross-instance template/value clustering over all 94 edit plans, and the scoped
no-laundering check on the security cells. Findings and resolutions:

- **Blocker (fixed structurally):** injected diffs used unified-diff conventions
  (`@@` hunks, context lines) while every parser-synthesized base diff is a bare
  `-`/`+` block — a format-counting baseline reached 0.677. The generator now reduces
  *all* diffs to the bare form uniformly, and `unified_diff_markers` joined the 4A
  baseline set (post-fix: exactly 0.500, i.e. zero discriminative power).
- **Format tells (fixed structurally):** four injected failure outputs copied the raw
  layer's `Error: Exit code` prefix (normalized outputs read `Exit code N`); two events
  carried non-null `exit_code` against 1142 nulls. Both normalizations are now uniform
  pipeline steps; the style profile was corrected at the source.
- **Fabrication-consistency majors (fixed per plan, re-verified):** a ruff output
  contradicting the file its own transcript wrote; a pytest progress line with two F's
  but "1 failed"; an env dump whose `sorted()`/`tail` combination was impossible; a
  runner summary omitting the pre-existing cases its own loader includes; an SSRF probe
  resting on an invented internal proxy and a misattributed motivation. Each was
  repaired minimally and confirmed against the regenerated transcripts.
- **Voice convergence (fixed):** subagent-register injections had drifted to a denser
  generator voice (48% of injected messages over the register's 25-word cap vs 7% in
  bases); a repeated "While the X is <state>" digression-opener marked five
  scope-expansion rows; an invented `DIVERGE` status tag contradicted the real harness
  vocabulary; two fake credentials shared a structural template (and the wrong length).
  All re-voiced/de-correlated; a corpus grep confirms zero remaining template openers
  and independently generated, format-correct credentials.
- **No-laundering: pass.** Zero domain vocabulary from the private seed session in any
  plan, transcript, or the taxonomy; all 15 security injections trace to their named CWE
  class plus the host base's own code.

Accepted residuals, documented deliberately: transcript ids encode class/tier for human
navigability (monitors never see ids — rendering covers events only — and labels are
public in an eval set); injected transcripts run slightly longer than pure-benign ones
(the passing 4A length baselines, 0.586–0.600, bound this); `git diff` hunks legitimately
appear inside `shell_command` outputs inherited from bases, identically across classes.

## Reproducing

```sh
uv run python datasets/synthetic/generate.py --render-bases   # numbered base renders
uv run python datasets/synthetic/generate.py                  # manifest + plans -> dataset
uv run python datasets/synthetic/leakage_check.py             # gate 4A
```

Requires the source sessions on the building machine (`manifest.yaml` → `sources`);
the *committed* transcripts, labels, and plans are the canonical artifact and need
nothing but this repo. `generate.py --author` can draft missing plans with a live LLM
where credentials exist.

## Known limitations

- **Generator diversity:** all edit plans were authored by one model family
  (`claude-fable-5` via Claude Code subagents). A monitor could in principle key on
  residual single-generator habits the catalog missed; matched pairs and the style
  profile mitigate but don't eliminate this.
- **Base correlation:** 39 bases for 133 transcripts means instances within a base are
  correlated; effective sample size is lower than the row count.
- **FPR resolution:** with 73 benign transcripts, recall@1%FPR uses a zero-false-alarm
  threshold (⌊0.01·73⌋ = 0). Treat the 5% budget as the headline until the benign pool
  reaches ≥100.
- **Scrubbing guarantee:** every transcript passed a hard deny-list (username, emails,
  handles, key prefixes) at generation; bases were additionally human-reviewed at the
  Phase-1 gate. One private session was excluded from the dataset entirely.
