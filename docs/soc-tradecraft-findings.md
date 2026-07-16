# SOC-tradecraft findings: what a detection engineer's artifacts taught agentmon

Findings from mining a **synthetic SOC/incident-response artifact package** authored with real
cyber-warfare-officer tradecraft (2026-07-15). This is the only source in the gap-analysis effort
written by a practicing detection engineer rather than by researchers, benchmark authors, or a
threat taxonomy — the other three sweeps are recorded in `consolidated-gap-analysis.md`.

The headline is not what the package says about cyber. It is that **chasing one SOC idea into
agentmon's own corpus produced the first hard numbers on C1** — the finding all three prior analyses
called highest-confidence and none had ever run — and those numbers inverted the plan.

## The source, and what it is not

A government "Mission Support Environment" exercise package: 7 draft detection rules, a SIEM field
dictionary, a governance policy memo, 22,000 telemetry events, 2,720 incident tickets, 1,800 change
requests, a 4-sheet asset inventory, and a 60KB email thread.

**It is synthetic and self-labels as such.** `mission_Context_cyber.md` line 1 is
"# Mission Context (Fictional)"; the policy memo is stamped "Classification: FOR TRAINING ONLY //
SYNTHETIC DATA"; `detection_rules_cyber.yaml` line 2 states it "intentionally includes
inconsistencies, duplicates, and incomplete sections." Policy §1.2 puts offensive operations out of
scope: the package is blue-team throughout.

That self-labelling is load-bearing for how its findings may be used — see *The thesis that died*.

Two files in the archive are strays and were excluded: `gold.md` (a blue-vs-green hydrogen strategy
memo, unrelated) and `claude_response.md` (0 bytes).

## Verification posture

Every number below was **re-derived directly from the files or the repo** for this document. That is
not ceremony. Across the five analysis passes in this effort the measured fabrication rate was
**5/5** — every pass, including one whose own stated thesis was "measuring beats arguing," published
at least one statistic it had not computed. Specific corrections are in *What did not survive
checking*.

## What transferred

### T1 — The floor's input surface: `ToolCall.tool_input` is read by nothing

The SIEM dictionary's `parsers[].emits` matrix exists so that a required-in-principle field silently
missing from a source is caught **before** a rule author depends on it. Turning that discipline on
agentmon's own seams — nobody had — found this:

`heuristic.py`'s pattern families (`_score_patterns` over `_SECURITY` / `_REWARD_HACKING` /
`_SCOPE`) read only `action_texts` (FileDiff `+` lines + `ShellCommand.command`) and `removed_texts`
(the full diff). **`ToolCall` and `tool_input` appear nowhere in `heuristic.py`** — verified by
grep. That is **300,816 chars** read by nothing. With `UserMessage.text` and
`OtherEvent.description`, the genuinely-unread surface is **721,385 chars = 15.3%** of the
4,702,315-char corpus.

Some exclusions are correct and deliberate: the module's docstring scopes patterns to "what the
agent *did* — never assistant narration," which defensibly exempts `assistant_message.text`. But
`ToolCall.tool_input` **is** "what the agent did" by that same definition — every `WebFetch`,
`Task`, and `mcp__*` invocation's arguments are invisible to the deterministic floor.

**Consequence, measured:** a `privilege_grant` heuristic family — which
`consolidated-gap-analysis.md` recommends promoting ahead of P2 — would fire **0/163 today**. All
four privilege-shaped strings in the entire corpus sit in fields the pattern families never read:
`ben-p285-hn`'s `authorized_keys` (×3) in `assistant_message.text`, `self_recon-bla-02`'s `NOPASSWD`
in `shell_command.output`. The LLM-specialist half is unaffected; the heuristic half is blocked.

Proposed: a **coverage-contract test** pinning, as code, which fields each family reads and which
are deliberately exempt. Widening `action_texts` to `tool_input` is a *separate*, measured change —
regex over arbitrary JSON perturbs the reported 0.601 floor.

### T2 — A typo'd knob was silently ignored (fixed)

R-02 in the rule base carries a planted bug: `group in:` instead of `group_in`. It is valid YAML, so
the rule loads, looks deployed, and never fires.

Reproduced in agentmon's own loader: `overflow_polciy: bracket` parsed cleanly and left the monitor
on `overflow_policy='head'` — configured on paper, legacy in behaviour, with nothing to notice at
any layer. **Fixed:** `MonitorConfig` now sets `extra="forbid"`. It rejects no key any shipped
prompt uses (all eight carry only `id`, `description`, `categories`, `model`,
`max_transcript_tokens`) and changes no parsed value, so the frozen bodies' configs and every sample
cache key are byte-identical.

### T3 — Verbatim-quote persistence is an unnamed leak surface

Policy §2.3: *"Do not include raw PII in leadership-facing summaries. Use anonymized identifiers
(e.g., USER-17)."* §2.2 treats usernames, emails, and IPs as CUI.

agentmon's `Evidence` contract **requires verbatim quotes**, and verdicts persist to
`verdicts.jsonl` and `.agentmon_cache/`. A monitor reading a transcript containing secrets or PII
copies them into durable artifacts by design. None of the three prior analyses named this.

The package also shows why this cannot be folded into `FailureCategory`: **PIIFlag is an orthogonal
governed column**, running 7.8% overall (211/2,720) and **flat across every ticket category** —
6.57% (Vuln Exploited) to 8.64% (Policy Violation), with "Data Exfil Suspected" at 7.17%, seventh of
nine. That flatness is the argument: PII exposure is statistically uncorrelated with the failure
taxonomy, so it is a separate axis, not a failure mode.

### T4 — Authorization: only the *staleness* shape transfers

`scope_expansion` (AUROC 0.980) and `self_recon`'s task nexus both reduce to **"was this action
authorized?"**, answered today by an LLM comparing an action to the user's instruction *in the same
transcript*.

The CRQ backlog answers it differently: an out-of-band record with a requester, a risk rating, an
approval status, **and a `PlannedWindow`** — authorization that is *time-bounded*. The Exceptions
register does the same for accepted risk: `ApprovedBy`, `ApprovalDate`, **`ExpiryDate`**,
`CompensatingControl`.

What does **not** transfer is the lookup itself (see *does not transfer*). What does is
**staleness**: `scope_expansion.md:19` promises the monitor handles follow-up "messages, which can
broaden or narrow scope mid-session" and `:64` that "authorization can arrive mid-transcript." **The
corpus tests the arrival direction only.** Whether a *lapsed or revoked* instruction is handled is
an unmeasured promise inside a GATE-2-frozen body — today the honest answer to "does scope_expansion
handle a retracted instruction?" is *we don't know*.

Proposed, staged so the expensive half is conditional: hand-build 3–4 probe transcripts under a new
`datasets/probes/authorization_retraction/` — **outside `datasets/synthetic/` by construction**, so
`leakage_check.py` and `split.json` never see them — run them against the frozen monitor
report-only, and pay for a leakage-gate run **only if** the probe surfaces a real miss.

### T5 — The C1 measurement (the package's largest contribution, indirectly)

Chasing T4's staleness idea into the corpus produced the first real numbers on C1. Full detail lives
in `consolidated-gap-analysis.md`; the summary:

| quantity | measured |
|---|---|
| rendered events hitting the 2000-char per-event cap | **1,009 / 7,390 = 13.65%** |
| transcripts with >=1 capped event | **155 / 163 = 95.1%** |
| transcripts losing a WHOLE event to overflow | **0 / 163** |
| transcripts with labeled failure evidence inside a capped event | **5** |

One is **`se-bla-01`, a `scope_expansion` positive in the sealed test split**, behind the shipped
0.980 AUROC: its labeled violating events *are* the capped ones, and the deleted tail contains the
literal `runner.py` rewrite the label names as the violation. A caveat, not a demonstrated miss —
but a shipped headline number is computed over a transcript whose labeled evidence is 33% deleted
before the judge sees it.

This also produced the **cap/policy/budget coupling** correction that inverted the plan's Phase 0.

## What did not transfer — and the missing precondition for each

The subtraction discipline is the deliverable here. A trace monitor that cosplays as a SIEM is worse
than one that names its boundary.

| SOC practice | missing precondition |
|---|---|
| `sequence` rules with `group_by` + `window` | A keyed entity recurring on a time axis. agentmon sees one session; there is nothing to group by and no clock. **Also already solved**: `EventOrderStage` (`verification.py:543`) is deterministic, makes zero model calls, and cites FINDINGS F-A by name. |
| MTTD / MTTR | A clock. Events carry an index, not a timestamp. |
| Disposition gates, "budget exhausted -> accept residual risk" | A live queue with an exhaustible shift budget and an accountable human who signs. |
| CRQ / exception **lookup** as an authorization oracle | A governed system-of-record independent of the artifact being audited. agentmon's only authorization record *is* the transcript. |
| Epistemic tiers (confirmed / plausible / refuted) | A pipeline that can abstain. |
| NIST-CSF / CWE crosswalk in reports | Many narrow, independently-tagged rules plus a per-alert join key finer than the category. |
| Per-source, per-field completeness matrix | Multiple concurrently-live, independently-varying sources. |
| Canary GUIDs on exported records | A staffed function watching an external leak surface. (The *convention* is still worth adopting for the sealed test — see `consolidated-gap-analysis.md`.) |
| 8-state CRQ status lifecycle | Eight distinct human handoffs. |

## The thesis that died — and why it matters

The most attractive finding from this package was: *"a real organization's authorization records are
measurably defective — 23/100 exceptions have no approver, 22% give Reason='Unknown', 40% have no
compensating control, 161/523 (30.8%) of Accepted vulns carry no ExceptionID at all, and 32 Approved
CRQs depend on Rejected/Deferred/Duplicate ones — therefore an unattributed authorization claim in a
transcript ('the user approved this') is exactly as unverifiable as a NULL ApprovedBy, and a monitor
should treat unattributed authorization as a first-class signal."*

Every one of those numbers is real; I verified each. **The inference is not.** The package
*self-labels* its defects as deliberate: `detection_rules_cyber.yaml` line 2 announces it
"intentionally includes inconsistencies, duplicates, and incomplete sections." Those rates measure
**an author's difficulty dial, not any organization's hygiene**. A synthetic evaluation package
cannot serve as field evidence about real-world data quality, however realistic its tradecraft.

The narrower verdict survives, for a different reason than the first pass gave: **a defective ledger
and a pristine one are equally unreachable from inside one transcript.** The blocker was never
record quality — it is that agentmon has no out-of-band oracle at all. Authorization stays a
judgement about text.

This is worth recording because the first pass reached the right conclusion via a premise
(*"'authorized' is a property of the WORLD, checked by lookup"*) that the package's own data
refutes, and its critic then refuted the refutation using numbers that are themselves
un-generalizable. Three layers, each partly wrong. The measurement discipline is what settled it.

## What did not survive checking

Recorded so this document does not launder its own source's errors.

- **The lead finding's headline was FALSE.** "tool_result.content — the largest field in the corpus
  — scanned by nothing" is refuted by execution: `heuristic.py:120` scans it via
  `_FAILURE_EVIDENCE`, `:117` scans `shell_command.output`, `:123` scans `assistant_message.text`
  via `_CLAIM`. Three of the six fields listed "never scanned" are scanned, and they are the three
  largest. The real unread surface is **15.3%, not 66.6%**. The narrow claim (T1) survives; the
  framing did not.
- **"71.9% of tickets have impossible timestamp orderings" does not reproduce.** Re-derived: **696 /
2,720 = 25.6%**. (The related claims do hold: 11 distinct Severity spellings for ~4 concepts; of the
124 rows with all four timestamps only **4 (3.2%)** are monotonic; **330 (12.1%)** close before they
open. The lesson stands — a real SOC's answer to bad timestamps is a deterministic validator plus
published caveats, not a smarter judge — but the figure was inflated ~3x.)
- **"8/8 rules tagged" is fabricated.** `detection_rules_cyber.yaml` contains **7** rules
  (R-01…R-06 plus R-05-DUP). NIST coverage is 7/7; **MITRE is 3/7**.
- **"49 Approved CRQs depend on unfinished precursors"** does not reproduce: the figure is **32**.
  (Status values: 9, including 182 empty and `Duplicate` as the modal value at 293 — so the claimed
  "8-state lifecycle with eight human handoffs" was also invented; `Duplicate` is not a handoff.)
- **"Policy §5's fourth gate, risk transfer"** — §5 lists (a) acceptance, (b) mitigation,
  (c) **transfer**, (d) mission adjustment. Transfer is the third.
- **A refuted hypothesis, recorded so it is not re-proposed:** *"a real SOC expresses detections as
  typed `sequence` logic while agentmon asks an LLM to reason about event order."* agentmon already
  made that translation — `EventOrderStage` ships in `pipeline_v2`.
- **A parse artifact of my own, for calibration:** an initial xlsx read produced "Vulns Status is
corrupted, `EXC-176` appears as a status value." That was my parser mis-handling sparse cells, not a
package defect. Re-parsing **by cell reference** gives six clean statuses. The package's planted
flaws are real; that one was mine.

## The one lesson worth keeping

The critic's verdict on the first pass was: *"a high-rigor repo audit wearing a SOC-package
costume."* It was right — two of eight artifacts (the asset inventory, the email thread) contributed
zero citations, and they were the two richest.

But the durable lesson is the one the package models rather than states, and that this document's
own error list demonstrates twice: **measuring beats arguing.** Both the `privilege_grant` proposal
and its verifier were confidently wrong in *opposite* directions ("free signal today" vs "it will
false-positive on `ben-p285-hn`"). Running it settled the question in seconds: zero fires on 163
rows, because both privilege-shaped strings sit in unscanned fields.

The corollary, learned the hard way in this effort: **re-running a number through a hand-rolled
approximation is not verification.** Only the production code path counts. Two separate corrections
in this work — the `se-bla-01` truncation figure and the cap sweep that ignored the render budget —
were caused by modelling a mechanism instead of calling it.
