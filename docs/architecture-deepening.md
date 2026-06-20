# Architecture deepening backlog

Deepening opportunities for **monitor reliability**, surfaced by an architecture review
(2026-06-19). Each candidate turns a **shallow** module — interface nearly as complex as the
implementation — into a **deep** one: a lot of behaviour behind a small interface, so callers get
**leverage** and maintainers get **locality** (change, bugs, and tests concentrate in one place).

Vocabulary is deliberate: *module, interface, implementation, depth, seam* (a place behaviour can
be altered without editing in place), *adapter* (a concrete thing satisfying an interface at a
seam), *leverage*, *locality*. The **deletion test**: if deleting a module makes complexity vanish
it was a pass-through; if complexity reappears across callers it was earning its keep.

These map to the targeted capabilities — multi-step verification, hierarchical monitoring,
ensembles, log pre-processing, and reusable evaluation frameworks. Nothing here is decided; each is
a candidate to grill, design an interface for, and implement. The companion HTML review (visual
before/after diagrams) is generated to a temp path by the `improve-codebase-architecture` skill and
is **not** committed — this file is the durable record.

| # | Candidate | Strength | Primary seam |
|---|---|---|---|
| 1 | Verification pipeline | **Strong** | `agentmon.calibration` verification |
| 2 | QC band gate → eval harness | **Strong** | `scripts/` → `agentmon.eval` |
| 3 | Monitor-composition seam | **Strong** | `agentmon.monitors` Monitor |
| 4 | Render-decoupled grounding + token budget | **Strong** | `agentmon.monitors` rendering |
| 5 | Retire the legacy single-pass path | Worth exploring | `agentmon.runner` |

---

## 1 · Lift the verification flip into a pipeline seam — **Strong** · top recommendation

> **Phase 1 implemented** (2026-06-19, `verification-pipeline` branch; DECISIONS 43). The flip is
> now a `VerificationPipeline` of `VerificationStage` adapters in `src/agentmon/verification.py`;
> the default two stages (quote-grounding · semantic) reproduce the committed verdicts byte-for-byte
> (golden master green). The opt-in Phase 2 stages — event-order (F-A) and claim-grounding (F-B) —
> are staged on the branch and land separately. Candidate 4 (render-decoupled grounding) remains the
> natural next new stage.

**Files:** `src/agentmon/calibration.py` (`run_calibrated` 398–408, `verify_sample` 225–274,
`quotes_match` 208–222), `src/agentmon/schemas.py` (`VerificationOutcome` 134–158).

**Problem.** Verification is one inlined flip-on-fail pass toggled by a single `verify: bool`. The
`verifications` field is a flat list parallel to the samples, so it cannot hold the result of a
*second* stage. Adding a stage means editing the scoring loop in place — the scaffolding is shallow.

**Solution.** A `VerificationPipeline` of `VerificationStage` **adapters** behind one interface the
scoring loop invokes once; outcomes stored keyed by stage. Today there are already **two adapters**
behind the flip — mechanical quote-grounding (`quotes_match`) and the LLM semantic check — so the
seam is real, not hypothetical.

**Before → After.** Before: `run_calibrated` → `if flagged and verify` → `verify_sample` (one box,
no plug point). After: `run_calibrated` → `pipeline.run(sample)` → \[quote-grounding · semantic ·
event-order · claim-grounding\] adapters → keyed outcomes → flip.

**Wins.**
- New stages plug in as adapters; the scoring loop stays untouched — *leverage*.
- The event-order check (FINDINGS **F-A**, confidently-wrong temporal reads) gets a home.
- Claim-grounding (FINDINGS **F-B**; the deferred deception fix, DECISIONS 41–43) gets a home.
- *Locality*: flip logic and stage results concentrate in one module.
- The interface is the test surface — each stage tested through one seam, mock client.

**Reliability stake.** The #2 documented failure mode is verification *killing correct catches*
(the quote-fidelity tax). This is where reliability actively leaks.

**Watch-out.** Verification stages change how flags flip → they change scored output. Develop and
validate on **dev** behind opt-in configuration; do not silently alter the frozen test scoring
(GATE-2 freeze, DECISIONS 33d). Candidate **4** is the natural first new stage.

**Would introduce (CONTEXT):** *verification stage*, *verification pipeline*.

**Implementation strategy (behaviour-preserving).** The committed test verdicts are the oracle, and
a golden-master test already pins them — `tests/test_verification_golden.py`:

- *Flip-rule layer* (CI-durable): recompute the flip and the calibrated score from each committed
  verdict's `samples` + `verifications` and assert they equal the recorded values.
- *Cache-replay layer* (local; skipped where the gitignored `.agentmon_cache` is absent): replay
  `run_calibrated` over the test split against the on-disk cache with a client that raises on any
  call, and assert byte-identical `CalibratedVerdict`s — proven 66/66 today.

Refactor in two phases against it:

- **Phase 1 — behaviour-preserving.** Extract the flip into the pipeline; default pipeline = the
  current two stages. The golden master must stay green. Three invariants are the *only* ways
  behaviour drifts: (a) **cache-key stability** — keep the semantic stage's verification cache key
  byte-identical; new stages get new, distinct keys; (b) **schema additivity** — keep
  `CalibratedVerdict.verifications` as-is and add an optional `stage_outcomes` (default `None`) so
  the old committed verdicts still deserialize; (c) **default = the current two stages** ⇒
  byte-identical verdicts.
- **Phase 2 — additive stages.** Event-order + claim-grounding ship *off by default*, validated on
  **dev** (the claim-grounding validation set is the six purpose-built dev cases on the
  `dataset-expansion` branch). New stages never run in the default path and never re-score the
  sealed test set; `results/` is immutable.

---

## 2 · Lift the QC band gate out of scripts into the eval harness — **Strong**

**Files:** `scripts/test_run.py` (`band_check` 132–168, `FLAG_BANDS` 66–72, `_own_mode_tp` 85–95,
`flip_report` 98–114, catch-loss guard 152–161), `scripts/dev_eval.py` (`summarize` 172–261,
`run_cost_usd` 152–161, `n_calls` 164–169, `build_live_client` 76–102).

**Problem.** The proceed/halt **QC gate** — the anomaly-band flag-rate windows, the own-mode-TP
catch-loss guard, retry-surge, request ceiling, cache-hit preflight — plus the cost/calls/summary
accounting live in ~700 LOC of `scripts/`, untested, with `test_run.py` importing helpers from
`dev_eval.py` (script leaks into script). **Deletion test:** delete the scripts and the gate and
accounting vanish — they have no `src/` home; complexity reappears, so they were earning their keep
in the wrong place.

**Solution.** A deep `agentmon.eval` harness: `band_check` + bands-as-versioned-config, `summarize`,
and accounting behind one interface — *verdicts + provenance + bands → pass/halt + summary*. The
scripts become thin **adapters** that wire a client and call the harness.

**Wins.**
- The gate finally has a test surface (normal / out-of-band / catch-loss fixtures).
- Delete the script-to-script import; orchestration depends on the library, not a sibling script.
- *Locality*: every halt condition in one module.
- *Leverage*: one harness, N experiment scripts and future evaluation runs.

**Reliability stake.** The gate decides whether a read-once test row is trusted or halted. An
untested gate is an untrusted reliability backbone.

**Would introduce (CONTEXT):** *anomaly band* / *QC gate* (recurs in DECISIONS 35–42, undefined in
the glossary).

---

## 3 · Open a monitor-composition seam (hierarchical + ensemble) — **Strong**

**Files:** `src/agentmon/monitors/base.py` (`Monitor` 178–260), `src/agentmon/calibration.py`
(flat monitor×transcript loop 378–430), `src/agentmon/eval/metrics.py` (per-monitor only 315–378),
`FINDINGS.md` (ensemble rows, pending).

**Problem.** A `Monitor` is a flat prompt→verdict object; `grep ensemble|escalat|cascade|hierarch`
over `src/` and `scripts/` returns **zero**. "Cheap monitor flags → escalate to a deeper monitor"
and "ensemble = max across monitors" both require editing the scoring loop. The ensemble rows in
FINDINGS are documented as *pending* with no code.

**Solution.** A `CompositeMonitor` that **satisfies the Monitor interface** and owns the composition
rule — cascade (cheap gates deep) and ensemble (combine verdicts) behind the same seam. The scoring
loop never learns a monitor composes.

**Wins.**
- Hierarchical monitoring becomes expressible — the headline targeted capability — with no loop edits.
- Ensemble stops being "pending"; it's an adapter, not a special case in the eval.
- Composites reuse the existing scoring loop and cache — *leverage*.
- A cheap first pass that gates a deep one cuts cost per transcript.
- Sub-monitors mock cleanly in tests.

**Would introduce (CONTEXT):** *composite monitor*, *cascade / escalation* (extends the existing
*Ensemble* entry).

---

## 4 · Render-decoupled grounding + token-aware budget — **Strong**

**Files:** `src/agentmon/monitors/base.py` (`render_event` 56–76, `render_transcript` 79,
`build_prompt` token×4 budget 218), `src/agentmon/calibration.py` (`quotes_match` substring match
220).

**Problem.** One shallow rendering module leaks its implementation format into two real reliability
bugs. (a) The render budget is `max_transcript_tokens * 4` characters — a ~30% under-count for
code-dense transcripts that overflowed a model's context window (the Gemma 8k/16k re-serve saga).
(b) `quotes_match` does a verbatim **substring** match against `render_event`'s exact output, so the
4-space continuation indent silently flipped a correct catch from supported to unsupported
(`se-bla-05`, DECISIONS 40). Verification reaches *through* the render format — the interface leaks
its implementation.

**Solution.** A deep `Rendering` module owning (i) a token-aware budget (real tokenizer, or a safe
margin) and (ii) a whitespace-normalized, **cited-event** grounding check that asks "do the rendered
cited events support the claim?" rather than substring-matching a quote. The render format becomes
private to the module.

**Wins.**
- Two render-coupled bugs concentrate in one module — *locality*.
- The quote-fidelity tax drops (verification stops discarding well-grounded catches; 4/17 of the
  diagnosed losses).
- The budget stops overflowing real context.
- Grounding is decoupled from render formatting — the format can change freely.
- Slots in as the first new stage of candidate **1**.

**Would introduce (CONTEXT):** *grounding* (cited-event support), *render budget*.

---

## 5 · Retire the legacy single-pass scoring path — Worth exploring

**Files:** `src/agentmon/runner.py` (`run_monitors`, `cache_key` 28–81), `src/agentmon/calibration.py`
(`_complete_cached` 191–205, `sample_cache_key` 90–122), `src/agentmon/cli.py` (183).

**Problem.** Two scoring paths duplicate the cache-miss → complete → write loop and run two
cache-key schemes; `runner.cache_key` omits `sample_index` — a documented footgun (a key without it
would serve one response k times and collapse fraction-flagged to {0, 1}). `run_monitors` is used
only by the `agentmon run` CLI and its own test. **Deletion test:** delete `runner.py` and only the
CLI + its test break; the cache-miss loop already lives in `_complete_cached`, so complexity does
not reappear — it was a pass-through.

**Solution.** Point the CLI at `run_calibrated(k=1, verify=False)`, delete `runner.py`, keep one
scoring path and one cache contract.

**Wins.**
- Delete a shallow module.
- One cache contract, no sample-index footgun.
- *Locality*: scoring lives on one path.
- The CLI shares the scoring interface with the experiments.

---

## Sequencing

Start with **1** (highest reliability leverage; the home for the F-A and F-B levers), with **4** as
its first new stage. **2** is the largest untested-debt removal and unblocks reusable evaluation.
**3** unlocks the hierarchical/ensemble capability. **5** is low-risk cleanup that can ride along.
