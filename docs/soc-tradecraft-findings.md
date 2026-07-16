# SOC-tradecraft findings: what a detection engineer's artifacts taught agentmon

Findings from mining a **synthetic SOC/incident-response artifact package** authored with real
cyber-warfare-officer tradecraft (2026-07-15). It is the only source in the gap-analysis effort
written by a practicing detection engineer rather than by researchers, benchmark authors, or a
threat taxonomy — the other three sweeps are recorded in `consolidated-gap-analysis.md`.

**The honest headline: the package's contribution was a question, not a measurement.** Its defect
*rates* teach agentmon nothing and must not be cited. One defect *shape* (staleness) is worth a $0
probe. The effort's real yield came from following that shape into agentmon's own corpus, which
produced the first hard numbers on C1 — the finding all three prior analyses called
highest-confidence and none had ever run.

## The source, and why its numbers cannot travel

A government "Mission Support Environment" exercise package: 7 draft detection rules, a SIEM field
dictionary, a governance policy memo, 22,000 telemetry events, 2,720 incident tickets, 1,800 change
requests, a 4-sheet asset inventory (Assets 900 / Owners 5 / Vulns 3,200 / Exceptions 100), and a
60KB email thread.

**It self-labels, and that is disqualifying for any claim about the world.**
`mission_Context_cyber.md` line 1 is "# Mission Context (Fictional)"; the policy memo is stamped
"FOR TRAINING ONLY // SYNTHETIC DATA"; `detection_rules_cyber.yaml` line 2 states it "intentionally
includes inconsistencies, duplicates, and incomplete sections"; and the mission brief's own success
criterion is *"Reconcile contradictory details across files."* **The defects are the assignment.**
Measuring them measures an author's difficulty dial. They are expert-elicited *shapes*, not measured
*base rates*.

The cleanest demonstration: of 523 Accepted vulns, 362 (69.2%) carry a resolvable `ExceptionID`, and
**0 of those 362** have an `Exception.AssetID` matching the vuln's own `AssetID`. That looks damning
until you compute the null: under a uniform draw against 900 assets the expected number of matches
is **~0.4**, so observing 0 is the *modal* outcome. The foreign key is decorative — generated
independently of AssetID. A fact about a generator, not about authorization hygiene.

Two files in the archive are strays and were excluded: `gold.md` (a blue-vs-green hydrogen strategy
memo) and `claude_response.md` (0 bytes).

## Verification posture

Every number below was re-derived directly from the files or the repo. Across the analysis passes in
this effort the measured fabrication rate was **5/5** — every pass, including one whose own thesis
was "measuring beats arguing," published at least one statistic it had not computed. Corrections are
in *What did not survive checking*, and they include two of my own.

## The authorization thread — the package's central offer, mapped shape by shape

`scope_expansion` (AUROC 0.980) and `self_recon`'s task nexus both reduce to **"was this action
authorized?"**, answered today by an LLM comparing an action to the user's instruction *in the same
transcript*. The package answers it differently: an out-of-band CRQ ledger and an Exceptions
register with approvers, expiry dates, and compensating controls.

The tempting inference — *"real authorization records are measurably defective, so a monitor should
treat unattributed authorization as a first-class signal"* — **fails twice**. The rates are authored
difficulty (above). And the blocker was never record quality anyway: **a defective ledger and a
pristine ledger are equally unreachable from inside one transcript.** Oracle *access* is the wall,
not oracle *quality*. The defect measurement is therefore *irrelevant to the verdict*, not
supportive of it — worth stating plainly, because the proposal that produced those numbers inherited
the prior pass's mistake in mirror image, treating "the record is dirty" as though it were the
reason the feature fails.

What survives is the shape-by-shape mapping — each defect the package exhibits, against agentmon's
only authorization record:

| defect shape | measured in the package | transfers? |
|---|---|---|
| **Unattributed** | 23/100 Exceptions have no `ApprovedBy`; 26/191 (13.6%) Approved CRQs have no requester; the ledger has **no approver column at all** (12 cols, `RequestedBy` only) | **No — by construction.** agentmon's authorization record *cannot* be unattributed: a `UserMessage` **is** the principal. Attribution is a **type**, not a data-quality variable. `verification.py:113`'s "Only a USER message authorizes" already implements the entire available test. |
| **Orphaned** | 161/523 (30.8%) Accepted vulns carry no `ExceptionID` | **Yes — and it is already the shipped product.** "Action with no authorizing record" *is* `scope_expansion`: 0.980 AUROC, 0 benign FPs on the sealed test split. Nothing to build. |
| **Expired** | `Exceptions.ExpiryDate`, 100/100 populated — the package's cleanest authorization field | **No — needs a clock.** None of `schemas.py`'s 7 event kinds carries a timestamp. agentmon has event order only; "expires 2024-09-18" is unrepresentable. |
| **Stale / superseded** | 44/49 (89.8%) of Approved CRQs *declaring dependencies* point at a precursor that is not Approved/Implemented | **Yes — the one live item.** Its analog is an attributed, in-transcript USER authorization whose stated precondition later lapses. Needs no clock — only event order, which agentmon has. |

### The one live item, and the measurement that actually earns it

`scope_expansion.md:19` promises the monitor handles follow-up messages "which can broaden or narrow
scope mid-session"; `:64` that "authorization can arrive mid-transcript." Measured against the
corpus:

- **Only 15 of 163 transcripts have more than one `UserMessage` at all** — that bounds the entire
  question.
- Across all 163 there are **zero** narrowing/revocation instances. `ben-pacc-hn`'s "don't push yet"
  is later *lifted* by the same user — the arrival direction, correctly complied with. `hn3-c`'s
  "Hold off on the report" is a pivot that *broadens*, per its own HN-3 ground truth.

So the **narrow half of a frozen prompt's written promise is an unmeasured claim.** Today the honest
answer to "does `scope_expansion` handle a retracted instruction?" is *we don't know*.

**Note where that finding came from.** The package supplied the *question*; agentmon's own corpus
supplied the *answer*. The package is **generative, not evidentiary** — the correct way to use a
fictional artifact set, and the opposite of how its defect rates were nearly used.

Proposed, staged so the expensive half is conditional: hand-build 3–4 probe transcripts under a new
`datasets/probes/authorization_retraction/` — **outside `datasets/synthetic/` by construction**, so
`leakage_check.py` and `split.json` never see them — score them against the frozen monitor
report-only, and pay for a leakage-gate run **only if** the probe surfaces a real miss. Distinct
from `redteam.py`'s existing `authz_laundering_attack`, which forges approval via *untrusted
tool-result content*; this is a real user authorization that lapses.

## What else transferred

### The floor's input surface: `ToolCall.tool_input` is read by nothing

The SIEM dictionary's `parsers[].emits` matrix exists so a required-in-principle field silently
missing from a source is caught **before** a rule author depends on it. Turning that discipline on
agentmon's own seams — nobody had — found this:

`heuristic.py`'s pattern families (`_score_patterns` over `_SECURITY`/`_REWARD_HACKING`/`_SCOPE`)
read only `action_texts` (FileDiff `+` lines + `ShellCommand.command`) and `removed_texts`.
**`ToolCall` and `tool_input` appear nowhere in the file** — 300,816 chars read by nothing. With
`UserMessage.text` (361,015) and `OtherEvent.description` (59,554), the genuinely-unread surface is
**721,385 of 4,702,315 chars = 15.3%**.

**This corrects the consolidated doc's build-order promotion of `privilege_grant`** (promoted there
as "the strongest evidence record in the analysis" — zero refutations). Run against the proposed
collector across all 163 transcripts: `NOPASSWD` fires on 1 (`self_recon-bla-02`), while
`authorized_keys`, `chmod u+s`, `attach-user-policy`, and `assume-role` each fire **0/163**. No
label in the set is `privilege_grant`, so **there is no per-category AUROC to perturb** — the mode
is *unmeasurable on this corpus*, not merely blocked.

Worse, the "exclude assistant narration ⇒ safe surface" rule does not generalize: the adjacent
pattern **`wheel` hits 15 benign and 16 non-benign transcripts** on that exact surface (routine
Python packaging output) — roughly base rate, i.e. a pure false-positive generator. Ship the third
collector as **honest dead-surface hygiene**, with a coverage-contract test pinning which fields
each family reads. Not as a privilege detector.

### Verbatim-quote persistence is an unnamed leak surface

Policy §2.3: *"Do not include raw PII in leadership-facing summaries. Use anonymized identifiers."*
§2.2 treats usernames, emails, and IPs as CUI. agentmon's `Evidence` contract **requires verbatim
quotes**, and verdicts persist to `verdicts.jsonl` and `.agentmon_cache/`. A monitor reading a
transcript containing secrets or PII copies them into durable artifacts by design. No prior analysis
named this.

The package also shows why it cannot be a `FailureCategory`: **PIIFlag is an orthogonal governed
column**, running 7.8% overall (211/2,720) and **flat across every ticket category** — 6.57% (Vuln
Exploited) to 8.64% (Policy Violation), with "Data Exfil Suspected" at 7.17%, seventh of nine. PII
exposure is statistically uncorrelated with the failure taxonomy, so it is a separate axis.

### A typo'd knob is silently ignored — hygiene, *not* safety

R-02 carries a planted bug: `group in:` instead of `group_in`. Valid YAML, so the rule loads, looks
deployed, and never fires. Reproduced in agentmon's loader: `overflow_polciy: bracket` parses
cleanly and leaves the monitor on `overflow_policy='head'`. **Fixed** via
`MonitorConfig(extra="forbid")`; it rejects no key any shipped prompt uses (all eight carry only the
five core keys) and changes no parsed value, so every cache key is byte-identical.

**But the safety argument that motivated it collapses — and this effort's own measurement is what
collapsed it.** The fix was sold on a typo silently defaulting a monitor "into the known-worst
render behaviour." With whole-transcript overflow measured at **0/163**, `head` and `bracket` render
byte-identical output on this corpus, so the typo's present consequence is **nil**. The repo fact is
real and the fix is worth keeping — as hygiene for future dev-round monitors. Not as safety.

### The C1 measurement (the effort's largest yield, arrived at indirectly)

Chasing the staleness shape into the corpus produced C1's first real numbers. Detail in
`consolidated-gap-analysis.md`:

| quantity | measured |
|---|---|
| rendered events hitting the 2000-char per-event cap | **1,009 / 7,390 = 13.65%** |
| transcripts with >=1 capped event | **155 / 163 = 95.1%** |
| transcripts losing a WHOLE event to overflow | **0 / 163** |
| transcripts with labeled failure evidence inside a capped event | **5** |

One is **`se-bla-01`, a `scope_expansion` positive in the sealed test split**, behind the shipped
0.980 AUROC, losing 996 of 3,010 chars (33%) of the `runner.py` rewrite its label names as the
violation. A caveat, not a demonstrated miss.

## What did not transfer — and the missing precondition for each

The subtraction discipline is the deliverable. A trace monitor that cosplays as a SIEM is worse than
one that names its boundary.

| SOC practice | missing precondition |
|---|---|
| **Approver identity / role-based authorization** | A second authority to check against, plus an identity oracle. agentmon has exactly one principal. "The CISO approved this but the SystemOwner could not have" is not expressible. |
| **Time-boxed authorization / expiry** | A clock. No event kind carries a timestamp. (The near-miss that *is* reachable: order-relative staleness — the live item above.) |
| **Asset-criticality weighting** | A resolvable join key from a transcript to an asset. *(The first pass killed this on "no out-of-band inventory" — wrong; the package contains one. The real wall is that the join is decorative even inside its own source: 0/362 matches, and `Criticality` is free text mixing `Critical`, `M`, `H`, and empty.)* |
| **Cross-record reconciliation, MTTD/MTTR** | Two independent records of the same event, and a clock. agentmon is per-transcript atomic: there is exactly one record and nothing to reconcile against. |
| **Duplicate detection** | A corpus-level view. `Duplicate` is the ledger's modal status (292/1,799 = 16.2%) and therefore the most tempting motif to port — and the one most cleanly foreclosed by per-transcript atomicity. |
| **Severity normalization** | A free-text field to normalize. `FailureCategory` is a closed pydantic enum; spellings cannot diverge. Citing this as motivation for schema-hardening is analogy, not evidence. |
| **Detection-rule field mapping** (`field_map`) | Heterogeneous log sources with competing schemas. agentmon ingests one normalized Event stream; the problem is solved upstream and would be *reintroduced* by porting the pattern. |
| **Telemetry-gap reasoning** ("don't read missing logs as absence of activity") | Knowledge of what *should* have been recorded. Corroborates the OMISSION mode in the open frontier — motivating, never measurement. |
| **`sequence` rules with `group_by` + `window`** | A keyed entity on a time axis — and **already solved anyway**: `EventOrderStage` (`verification.py:543`) is deterministic, zero model calls, cites FINDINGS F-A by name, ships in `pipeline_v2`. |
| **Disposition gates / "budget exhausted → accept residual risk"** | A live queue with an exhaustible shift budget and an accountable human who signs. |

## What did not survive checking

Recorded so this document does not launder its sources' errors — or its own.

**From the first SOC pass:**

- **Its headline was FALSE.** "tool_result.content — the largest field — scanned by nothing" is
  refuted by execution: `heuristic.py:120` scans it via `_FAILURE_EVIDENCE`, `:117` scans
  `shell_command.output`, `:123` scans `assistant_message.text` via `_CLAIM`. The unread surface is
  **15.3%, not 66.6%**. The narrow claim survives; the framing did not.
- **"71.9% of tickets have impossible timestamp orderings"** → re-derived: **696/2,720 = 25.6%**.
  (Related claims hold: 11 Severity spellings for ~4 concepts; of the 124 rows with all four
  timestamps only **4 (3.2%)** are monotonic; **330 (12.1%)** close before they open.)
- **"8/8 rules tagged"** → there are **7** rules; NIST 7/7, **MITRE 3/7** (R-01, R-02, R-04).
  Parsing caveat: tags live under each rule's `outputs.tags`, not as top-level keys, so a naive scan
  reports 0/7 and a careless reader concludes the rules are untagged.
- **"8-state CRQ lifecycle / eight human handoffs"** → **9** status values including 10.1% blank,
  with `Duplicate` modal at 292. Not a lifecycle.
- **Policy §5's "fourth gate, risk transfer"** → §5 lists (a) acceptance, (b) mitigation,
  (c) **transfer**, (d) mission adjustment. Transfer is third.
- **"self_recon is frozen"** → `configs/freeze/gate2.yaml` pins exactly **five** bodies:
  security_vuln, reward_hacking, deception, scope_expansion, generalist. self_recon is dev-only.

**From the corrective re-mine:**

- **Its own flagship was fabricated** — *"1.0% (5/523) of Accepted vulns trace a clean authorization
  chain"* reproduces at 69.2% / 54.3% / 40.5% / 27.3% / 0.0% under five escalating join definitions,
  and at 5/523 under none. The re-mine caught this itself, which is the point of convening it.
- **"1,800 rows, CRQ-30001…CRQ-31800"** → 1,800 data rows but only **1,799 bear a CRQ_ID**; one
  record has an **empty primary key**, and CRQ-30189 appears nowhere in the file.

**Mine:**

- **My "correction" of 49 → 32 Approved-on-unfinished CRQs refuted a claim nobody made.** The first
  critic's coarse "49 Approved CRQs depend on Rejected/Deferred/Duplicate" does not reproduce (32
  under that exact definition; 37 including Rolled Back). But the re-mine had already refined it to
  **44/49 (89.8%) of Approved CRQs *declaring dependencies* pointing at a precursor that is not
  Approved/Implemented** — which reproduces **exactly**, and decomposes to 37/49 terminal-bad, 7/49
  never-decided, 5/49 clean. I measured a different quantity and published it as a refutation.
- **My "expected 0.33 AssetID matches" check was wrong.** I computed 38.21 using 95/900 — the chance
  the FK lands in the *set* of exception assets — instead of 1/900, the chance it matches *that
  vuln's* asset. Correct: 362/900 ≈ **0.4**. The re-mine was right; my refutation was the error.
  Same conclusion either way.
- **An xlsx parse artifact.** An initial read reported "Vulns `Status` is corrupted — `EXC-176`
  appears as a status value." That was my parser mis-handling sparse cells; re-parsing **by cell
  reference** gives six clean statuses. The package's planted flaws are real; that one was mine.

## The lesson worth keeping

The critic's verdict on the first pass was *"a high-rigor repo audit wearing a SOC-package costume"*
— two of eight artifacts contributed zero citations, and they were the two richest.

The durable lesson is the one the package models rather than states, and that this document's own
error list demonstrates three times: **measuring beats arguing.** Both the `privilege_grant`
proposal and its verifier were confidently wrong in *opposite* directions ("free signal today" vs
"it will false-positive on `ben-p285-hn`"). Running it settled the question in seconds.

Two corollaries, each learned by getting it wrong here:

1. **Re-running a number through a hand-rolled approximation is not verification.** Only the
   production code path counts. Three separate errors in this effort — the `se-bla-01` truncation
   figure, the cap sweep that ignored the render budget, and my AssetID null model — were all caused
   by modelling a mechanism instead of calling it.
2. **A summary is not its source.** This document's first version was written from the corrective
re-mine's *truncated notification* rather than its output file, and was materially weaker for it: it
missed the shape-by-shape mapping, missed that "orphaned" is already `scope_expansion` and needs
nothing built, missed the zero-narrowing corpus measurement that is the only real evidence for the
one live item, and published as corrections two numbers the source had already gotten right.
