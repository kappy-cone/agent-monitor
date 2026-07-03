# Architecture deepening — capability expansion (2026-07-01)

> **Status (2026-07-03, `monitor-composition` branch; DECISIONS 46).** All five designs are
> implemented code-side and green (golden master byte-identical, both layers); every live run and
> sealed read remains gated:
>
> - **01+05 (rendering program)** — landed: `agentmon.rendering` (view-true grounding with the
>   merge-born `elided` status, bracket policy, calibrated budget pinned 3.0/3.0 from the offline
>   run in `results/dev-render-calibration/`), grounded stages at the candidate-1 seam. Offline
>   validations RAN ($0, zero completions): the would-ground column in
>   `scripts/diagnose_catch_loss.py` (design 01 Instrument 2 — recoverable-class draws all
>   ground, paraphrase draws all stay, se-bla-05 back as `normalized`) and the dev cache-replay
>   conversion matrix (`scripts/dev_grounding_replay.py`, design 05 Validation 4 — acceptance
>   PASS on both substrates, `results/dev-grounding-replay/`). NOT run: the live grounding A/B
>   (`scripts/grounding_ab.py`, delivered behind `--execute`) and the legacy-vs-calibrated
>   budget A/B (no runner delivered yet) — both wait on a declared live-substrate dev round.
> - **02 (eval harness)** — landed in full: `agentmon.eval`
>   (gate/bands/accounting/summary/workspace + `llm/live`), parity oracle over the 10 committed
>   rows, bands as recompute-validated config, scripts as thin adapters. Nothing waits on a gate.
> - **03 (hierarchical live)** — code landed: provenance carry-through, `HeuristicMonitor`, the
>   composite gate + two-pass `replay_coverage`, and both runners
>   (`scripts/dev_composite_verified_eval.py`, `scripts/test_run_composite.py`). NOT run: the
>   verified dev probe (offline, machine-cache replay — its dev rates feed the composite bands);
>   the sealed composite read waits on an explicit user GO recorded in DECISIONS (Phase 4).
> - **04 (stage promotion)** — machinery landed: `terminal`/confirm, `ScopedStage`,
>   `pipeline_v2`, `agentmon.eval.stage_report`, `scripts/dev_stage_eval.py`, the stage-aware
>   catch-loss diagnostic. NOT run: the event-order replay arm and the claim-grounding live arm
>   (cache population + panel; `--execute`); the promotion decision waits on that evidence, and
>   any sealed re-score is GATE-3.
> - **Candidate-5 rider** — landed: `complete_cached` relocated to `agentmon.cache`, `runner.py`
>   retired, the CLI on the calibrated path at k=1.

Expansion of the [architecture-deepening backlog](architecture-deepening.md), focused on the
ideas that raise the **capabilities** of the monitor (what it can catch, at what cost, with what
guarantees) rather than only its hygiene. Produced by a multi-agent review: six readers over the
live code and docs, one designer per candidate, each design adversarially verified against the
actual code (every file:line claim checked; several capability claims were **refuted by replaying
the caches** and corrected before landing here). Full designs live in `docs/designs/`:

| Design | Expands | Capability at stake | Doc |
|---|---|---|---|
| Grounding + render budget | Candidate 4 | Recover verification-killed catches; stop context overflow | [01](designs/01-grounding-and-render-budget.md) |
| Eval harness | Candidate 2 | Reusable, tested QC gate; gate composite/new-substrate rows | [02](designs/02-eval-harness.md) |
| Hierarchical monitoring live | Candidate 3 follow-through | Ensemble/cascade test rows (dev AUROC 0.905 vs 0.784 best leaf) | [03](designs/03-hierarchical-monitoring-live.md) |
| Verification-stage promotion | Candidate 1 follow-through | Promotion methodology for F-A/F-B stages; verified ensembles | [04](designs/04-verification-stage-promotion.md) |
| Pre-processing / overflow | The uncovered fifth capability | Evidence-preserving truncation; view-true grounding | [05](designs/05-preprocessing-and-overflow.md) |

Everything below is dev-side and $0 by construction: golden master stays green, the sealed test
set is only ever re-scored at a declared gate, `results/` stays immutable, and both substrates are
self-hosted.

## Corrections to the backlog (facts that changed under it)

1. **Candidate 5 is stale.** `runner.py` can no longer be deleted wholesale: since the Phase-1
   verification refactor it hosts `complete_cached` (runner.py:55–75), imported by
   `calibration.py:24` and `verification.py:26`. Only `run_monitors` + `cache_key` remain
   pass-throughs (and `run_monitors` still inlines its own cache loop beside the shared helper in
   the same file). Retirement now means: relocate `complete_cached`/`load_cached_response` first,
   then point the CLI at `run_calibrated(k=1, verify=False)`. Still low-risk cleanup; near-zero
   capability content — ride-along only.
2. **The DECISIONS-44 watch-out is already half-closed.** `VerifiedEnsembleMonitor`
   (per-member verify-then-OR, DECISIONS 45) exists with the `self_verifies` runtime guard. What
   is missing is not the mechanism but the **evidence and plumbing**: it has zero dev result rows,
   the QC gate crashes on (or is silently blind to) composites, and no gated test-read path exists.
3. **`EventOrderStage` does not catch the documented F-A instance.** Replaying
   `_ordering_contradiction` over the real cached `deception × hn1-b` draws (the 3/3 score-75
   F-A evidence): the stage **abstains on all three** — the monitors phrase the inversion as
   bracketed `[22]` citations plus clause-level before/after, exactly the phrasing the D43
   anti-over-refutation hardening excludes; the pinned test strings are paraphrases, not the cached
   phrasing. And on Qwen/Gemma, hn1-b never flags deception at all (F-A is a Gemini-substrate
   fact). The stage is honest **insurance** (provably harmless veto), not measured F-A mitigation
   — promotion claims must be worded accordingly (design 04).
4. **The QC gate is vacuous for self-verifying composites.** `flip_report` reads
   `verifications`, which a self-verifying composite leaves `[None]*k` (reports all zeros), and the
   catch-loss halt guard computes pre-flags from `sample.categories`, which
   `VerifiedEnsembleMonitor` has already cleared on refuted draws — pre==post, the guard can never
   trip. `CascadeMonitor` additionally **destroys** its deep member's provenance on return
   (composite.py:332–343), making "escalated but all refuted" indistinguishable from a deep miss.
5. **The per-event 2,000-char cap is a live, unowned pre-processing pass.** It fires on 822
   events across 125/133 transcripts and slices **6 ground-truth failure events** (incl.
   dec-sub-05 event 67, a known dev catch-loss). Verification then re-renders cited events
   **uncapped** (verification.py:135), so the verifier prompt's "the cited events, exactly as the
   monitor saw them" is false for every capped event. Nobody owns "what the monitor saw."
6. **Whole-transcript truncation is dormant but maximally wrong.** 0/133 transcripts overflow
   today's 64k-char budget — but ground-truth evidence sits late (median position 0.85; 84/94
   at ≥0.5) and `render_transcript` drops from the **tail**. At the 8,192-token window the Gemma
   saga collided with, 9/94 failure transcripts overflow and head-first truncation drops
   ground-truth evidence in **9/9**; a deterministic head+tail bracket policy drops **0/94**. A
   calibrated (tighter) token budget *activates* this cliff: at 3.0 chars/token, 3/133 newly
   overflow and dec-med-03 loses its deception evidence under head-first.

## The merged recommendation: one Rendering program (designs 01 + 05)

Designs 01 and 05 independently arrived at the same new module (`src/agentmon/rendering.py`) from
two directions; they merge into one program, and correction 5/6 above says why neither half ships
alone:

- **Contract (from 05):** a `MonitorView` — the exact post-cap, post-budget text the monitor saw,
  with explicit `elided` index ranges — consumed by prompt building, grounding, and verification
  rendering alike. Grounding checks the view, not an uncapped re-render ("view-true").
- **Grounding taxonomy + stages (from 01):** `exact / normalized / ellipsis / relocated /
  unmatched / no_such_event` statuses; whitespace-only normalization (content, case, order stay
  sacred — F-B says monitors rationalize, so the mechanical check stays adversarially robust);
  quote relocation repairs the pointer, never the vote; opt-in
  `NormalizedGroundingStage`/`GroundedSemanticStage` at the candidate-1 seam, new cache-key kind,
  never on the default path. The grounded verifier prompt annotates quote *presence* only, never
  support.
- **Budget (from 01, guarded by 05):** `RenderBudget` in token space via a chars-per-token ratio
  calibrated offline from the 990 committed sample records per substrate (p05 rule pinned before
  the numbers are read, DECISIONS-42 discipline), plus `preflight_fits()` — but shipped **only
  together with** the bracket overflow policy, because the tighter budget is what un-dormants the
  head-first evidence cliff.

Capability targets (dev, pre-registered): own-mode catch-loss 3/26 → ≤1/26; deception
recall@5%FPR back to ≥0.75 and scope to 1.00 under verify-on; benign-FP removal power preserved
(security-row precedent: 4 benign FPs removed, own-AUROC 0.655→0.673 — the tax cut must not buy
recall with leniency); 0 context overflows, detectable pre-spend. The quote-fidelity tax is the
#2 documented failure mode (4/17 diagnosed test catch losses; se-bla-05 was a *blatant* catch
killed by the renderer's 4-space indent), so this is the highest direct-recall lever in the
backlog.

## Taking hierarchical monitoring live (design 03, enabled by 02)

The headline capability number in the repo is stranded: dev 4-specialist OR ensemble AUROC
**0.905 (Qwen) / 0.861 (Gemma)** vs best leaves 0.784/0.712, and the FINDINGS ensemble rows have
been "pending" since the table was written — because verification × composites and composite
bands are open. The path (design 03), at **$0 and zero new completions** (every member draw and
member verification a composite needs is already in the machine-local cache from the leaf
`verify=True` runs):

1. Additive provenance (`member_flagged`, `member_verifications`, recursive `inner` so cascades
   stop destroying deep provenance).
2. `HeuristicMonitor` adapter (~10 lines) so the zero-cost floor sits at the composition seam —
   admitted only as an **OR-widener** of the triage (union recall can only grow); rejected as a
   series gate (it would cap cascade recall at ≈0.57 and forfeit the ensemble gain).
3. `agentmon.eval.gate` — composite-aware band check: one band on the composite's own dev flag
   rate via the pinned blind-to-test rule (unit test reproduces all five committed Gemma bands
   exactly), escalation band + gate-miss report for cascades (gate miss ≠ catch-loss), two-pass
   replay-coverage preflight (draw keys *and* reconstructed verification keys), `_NoCallClient`
   by construction.
4. A verified-composite dev probe (verify **ON** this time), then a single gated, user-approved
   **replay row** per pre-registered candidate to fill the two pending FINDINGS ensemble rows —
   with the semantics choice (self-verifying, not bare max) recorded in DECISIONS before the read.

Design rule that survived adversarial review: **the flip applies to the member draw, never the
composite verdict**; driver-only verification of a plain ensemble is rejected for live use
(mis-keyed for cross-substrate pins; a loud refuted FP driver drops a quieter member's true
OR-catch — amplifying the exact 4/17 catch-kill failure mode the ensemble should help).

## The eval harness (design 02) is the enabler, not the garnish

The gate has already decided real rows (the security verification-band HALT, D35b; the Gemma
deception borrowed-band HALT and the by-hand D42 re-band) while living untested in ~700 LOC of
`scripts/`. Lifting it into `agentmon.eval` (`bands.py`, `gate.py`, `accounting.py`,
`summary.py`, `workspace.py`, `llm/live.py`) buys three capabilities: the halt semantics get
pinned by a **parity oracle over the 10 committed rows** (including reproducing Gemma-deception's
recorded halt string) before any code moves; bands become versioned config whose loader
**re-derives every band from its recorded dev rate and refuses mismatches** (blind-to-test
enforced by code, not discipline — substrate onboarding becomes a config commit); and the gate
becomes composite- and stage-aware (provenance-driven catch-loss, member-fan-out call accounting),
which is the precondition for design 03's gated read. Refusal guards (`--confirm-phase4` etc.)
deliberately stay in the script adapters, never the library.

## Verification-stage promotion (design 04): methodology over mechanism

The missing piece for candidate 1's opt-in stages is not code but an **evidence standard**:

- **Stage confusion accounting** (`agentmon.eval.stage_report`): per-stage catches-killed /
  catches-saved / FPs-suppressed / FPs-restored / calls-saved between a baseline and candidate
  pipeline, draw-level counts primary (dev metrics are quantized: 4 deception positives ⇒
  recall moves in 0.25 steps).
- **Vote lattice:** refute = terminal always; **confirm** = new terminal-uphold
  (`StageOutcome.terminal`, additive), granted only to category-scoped specialists on a positive
  finding, never on fail-open; order is the resolution policy. `ScopedStage` is load-bearing —
  an unscoped `ClaimGroundingStage` is a recall bomb (it would refute most correct
  scope/security/reward flags).
- **Named pipeline versions** (`v1` frozen forever ≡ default; `v2` = quote-grounding →
  event-order → scoped claim-grounding(confirm) → semantic). Promotion means "recommended for
  new dev work", never an edit to `default_pipeline()`. Re-scoring the sealed test is a separate
  declared **GATE-3**.
- **Honest claims:** `EventOrderStage` promotes as zero-kill insurance only (see correction 3);
  `ClaimGroundingStage` is the real lever — deception is the worst-FP-load monitor (5 test benign
  FPs) and dec-sub-05 is the single verification-killed dev deception catch its confirm semantics
  should recover (0.50 → 0.75 own-row recall@5%FPR, exactly one transcript). Verify-then-combine
  beats combine-then-verify by set inclusion plus the loud-FP asymmetry; combine-then-verify is
  additionally disqualified outright for cross-substrate ensembles (mis-keyed verification cache).

## Sequencing

1. **Rendering program** (merged 01+05, phases R0→R3): highest direct recall lever; also ships the
   first new verification stages through the candidate-1 seam. Behaviour-preserving extraction is
   pinned by a byte-equivalence census over all 133 transcripts before anything else moves.
2. **Eval harness** (02, phases 0→3): parity oracle first; unlocks everything gated.
3. **Hierarchical live** (03, phases 0→4): provenance + gate awareness + verified dev probe; ends
   at the user-approved replay read that fills the two pending FINDINGS rows. Depends on 02's gate
   module (or ships its `eval/gate.py` slice first — the designs are written to compose).
4. **Stage promotion** (04): runs on the same dev slices; its `stage_report` module and the
   rendering program's grounded stages feed each other (a `v3` = grounded + scoped pipeline is the
   natural follow-on candidate).
5. **Candidate 5** rider, corrected: relocate `complete_cached` out of `runner.py`, then retire
   `run_monitors`/`cache_key` and point the CLI at the calibrated path.

Shared safety rails across all five: schemas.py changes are additive-only and land first; every
phase ends with `env -u UV_PYTHON uv run ruff check && uv run ruff format && uv run pytest`
(keyless, mock-first); golden master (flip-rule + 66/66 cache replay) green throughout; new LLM
stages get new cache-key kinds; nothing constructs a composite or a non-default pipeline unless a
caller opts in.
