# Design 08 — Competition → agentmon insight capture (living doc)

Status: **LIVING CAPTURE DOC — opened 2026-07-07.** Not a build plan. It exists to catch the
*structural* lessons from the Kaggle agent-security competition (deterministic offline benchmark;
attack.py searches for multi-step tool-use failures — exfiltration / untrusted-to-action /
destructive-write / confused-deputy; targets GPT-OSS + Gemma-4; entry deadline **2026-08-25**, final
submission **2026-09-01**, working note **2026-09-08**) and route them onto agentmon's roadmap
*deliberately, later*. Nothing here is built until Phase 2 (post-competition harvest). See
`docs/threat-matrix-defense-plan.md` (P1–P4) and `docs/designs/07-p4-trajectory-aggregation.md`.

## How to use this doc

Three phases (agreed 2026-07-07):
- **Phase 0 (done):** clean agentmon baseline + this scaffold.
- **Phase 1 (the competition):** work it as the single active thread. When a lesson surfaces, drop a
  dated line in **§ Observations log** and, if a Claude Code dev session looks like a good future base,
  note it in **§ Candidate bases**. Write notes, not agentmon code.
- **Phase 2 (harvest):** turn the highest-value threads below into real additive agentmon work, in the
  dependency order recorded per thread. This doc is the input to that phase.

## What does NOT transfer (settled — do not re-derive)

The competition's **output traces don't feed agentmon**: wrong agent (GPT-OSS/Gemma vs Claude Code),
wrong tools (fixture web/email/http/fs vs coding shell), wrong format (Gym `AttackCandidate` vs Claude
Code JSONL / `STYLE_PROFILE.md`), wrong side (attacker vs defender/trace-monitor). Nothing scored on
the leaderboard is a drop-in agentmon transcript. What transfers is **(a) the structural insights
below** and **(b) your own Claude Code dev sessions** (real transcripts, usable as new *bases* for the
existing modes / P4 composition — but they are Python/notebook dev, so **no persistence surface → not
P2 material**). Also note: the competition's four predicates map to P3-exfil and a not-yet-modeled
*untrusted-to-action* class — **not to P2 self_preservation** (persistence/shutdown-resistance is
absent from the predicate set).

## Structural threads to harvest (seeded — rank/prune during Phase 1)

### T1 — Data-provenance / taint tracking  ★ KEYSTONE
- **The gap it closes:** agentmon's monitors judge the agent's *actions* but never track *which events
  are untrusted-sourced* or whether untrusted content influenced a later tool call. It is structurally
  blind to **prompt-injection chains** (untrusted web/email content → privileged action) — the
  competition's `UNTRUSTED_TO_ACTION` predicate.
- **Shape (additive):** tag events by trust level (a field/annotation riding the `schemas.py` event
  model, or a preprocessing pass) and flag untrusted→action flows. New detection primitive, not a mode.
- **Dependency:** **build this BEFORE any `untrusted_to_action` specialist** — a mode built without it
  would have to be re-plumbed (this is the primary technical-debt trap to avoid).
- Observations: _(fill in Phase 1)_

### T2 — Multi-step / trajectory data (P4 upgrade)
- **The gap it closes:** design 07's P4 is a *mechanism demo* on trajectories **composed** from
  single-transcript injections (capped at 4 campaigns; permutation-sensitivity claim only). The
  competition generates **authentic multi-step traces** where the boundary crossing is distributed
  across steps — the realistic low-and-slow corpus P4 can't currently source.
- **Shape:** mine the competition's multi-step attack *structure* (not the raw traces) to inform real
  escalation-sequence data, upgrading P4 from mechanism-demo → validated multi-step detection.
- **Dependency:** P4's full kill-chain headline (recon→persistence→exfil) still needs **P2** to exist;
  this thread strengthens the *evidence*, not the sequence coverage.
- Observations: _(fill in Phase 1)_

### T3 — Attack-search as a data-generation engine
- **The gap it closes:** hand-authoring diverse leakage-gate-passing injections is agentmon's most
  expensive step (design 06/07 effort finding) and the base-supply bottleneck.
- **Shape:** adopt the competition's search methods (Go-Explore archive / novelty search / evolutionary
  / LLM-assisted) and its **severity × unique-cell diversity** objective as a *coverage-driven injection
  generator* — auto-propose failure candidates per base instead of writing rows by hand.
- **Caveat:** generated injections still must pass the two-part leakage gate + `STYLE_PROFILE.md`; the
  generator proposes, the gate disposes.
- Observations: _(fill in Phase 1)_

### T4 — Red-team / monitor-robustness loop
- **The gap it closes:** `threat-matrix-defense-plan.md` explicitly defers "monitor-manipulation
  resistance" because the dataset has **no monitor-directed attacks → untestable**. The competition is
  a red-team engine.
- **Shape:** harvest attacks crafted to *defeat detection* (hide the boundary crossing across steps so
  per-transcript scoring misses it; content that makes the verification/quote-grounding stage misfire)
  → monitor-directed adversarial test cases + a robustness eval axis + hardening targets for the
  grounding stages.
- Observations: _(fill in Phase 1)_

### T5 — Predicate + replay-validated ground truth (lighter)
- **The gap it closes:** agentmon's ground truth is a label + notes-based failure location. The
  competition defines failure as a **deterministic predicate over the trace**, recomputed by replay
  (never trusting attacker metadata) — crisper, more objective labels.
- **Shape:** consider a predicate layer for injected failures (e.g. "an `http.post` of a secret pattern
  occurred") as an objective co-label alongside the human label — tightens the realism/leakage gate.
- Observations: _(fill in Phase 1)_

## Debt guardrails (carry into Phase 2)

1. **Reduce concurrent open threads — don't add.** Keep Kaggle the single active thread; reach clean
   stopping points before re-opening self_recon-tiers / P4 / P2.
2. **Insight capture beats premature code.** During Phase 1, write here — not agentmon code. Design
   docs are free and reversible; half-built modes are debt.
3. **Primitives before dependent modes.** T1 (provenance) ships before any injection mode.
4. **Decide scrub / public-safety at capture time.** If a dev session becomes a base, make the
   ship/no-ship call when captured (the techacct/PLFM lesson), not months later.
5. **Respect known sequence dependencies:** provenance→injection-mode; P2→P4 for the full kill-chain
   headline.

## Observations log (append-only, dated)

_(empty — one dated line per lesson during Phase 1)_

## Candidate bases from dev sessions

_(empty — Claude Code dev sessions worth scrubbing into agentmon bases; note the register + any
public-safety concern at capture time. Reminder: dev/notebook work has no persistence surface → not P2
material.)_
