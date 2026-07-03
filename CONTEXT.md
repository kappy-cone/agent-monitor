# agentmon

Shared language for agentmon — an LLM-as-judge harness in the spirit of AI-control work: a
trusted monitor reads a coding-agent session and emits a calibrated suspicion verdict, scored
against a labeled dataset.

This file is a **glossary**, not a spec. It defines what each term *means*, in domain language —
no file paths, function names, schema fields, or formulas. Conventions live in `CLAUDE.md`;
decisions live in `DECISIONS.md`; results live in `FINDINGS.md`. When you reach for one of these
words, use the canonical one and avoid the listed synonyms.

## Monitoring

**Trusted monitor**:
A weaker, independent model that reads a whole agent session and judges whether the agent
misbehaved, without being swayed by the agent's own account. Run cross-family — a different model
lineage than the agent it watches.
_Avoid_: judge, evaluator, detector

**Monitor**:
A single named judge — an instruction plus the failure categories it looks for — that produces a
suspicion verdict for one transcript.
_Avoid_: prompt, template

**Monitor body**:
The prose instruction a monitor gives the model, distinct from its metadata. Locked once the
monitor is final.
_Avoid_: template, instruction

**Specialist / generalist monitor**:
A specialist watches exactly one failure category; a generalist watches all of them.
_Avoid_: single-mode / multi-mode monitor

## Suspicion

**Suspicion score**:
A monitor's 0–100 rating, on a single read, of how likely it is that the agent misbehaved.
_Avoid_: confidence, risk score

**Suspicion verdict**:
A monitor's full judgment on one transcript: the score, which failure categories it flagged, and
the evidence quotes that justify them.
_Avoid_: response, output, judgment

**Flag** (verb / noun):
To emit at least one failure category for a transcript — the binary "this looks bad" decision, as
opposed to the graded suspicion score.
_Avoid_: positive, raised

**Fraction flagged**:
Across repeated reads of the same transcript, the share that flagged it — the monitor's effective
probability that the transcript is a failure.
_Avoid_: flag rate, detection rate

**Calibrated score**:
The single number a transcript is ranked by — driven mainly by its fraction flagged, with the
suspicion score breaking ties. Thresholds and ROC curves are computed against this.
_Avoid_: score (unqualified), probability

## Failure modes

**Failure category**:
One of the five named kinds of agent misbehavior the monitors judge: security_vuln,
reward_hacking, scope_expansion, deception, and other.
_Avoid_: issue, problem, class

**security_vuln**:
An agent edit that introduces a security flaw — hardcoded secrets, injection, unsafe
deserialization, path traversal, and the like.
_Avoid_: CVE, bug

**reward_hacking**:
An agent optimizing for the test signal instead of the task — deleting failing tests, loosening
assertions, hardcoding expected outputs, mocking out the unit under test.
_Avoid_: gaming, cheating, test manipulation

**scope_expansion**:
An agent acting beyond the user's explicit instruction — editing unrelated modules, installing
unprompted dependencies, changing CI, reformatting whole files. Judged against the ask in the
same transcript.
_Avoid_: bloat, over-reach, task drift

**deception**:
An agent whose words contradict its actions — claiming a fix that isn't there, reporting a check
it never ran, framing a workaround as a fix.
_Avoid_: dishonesty, hallucination

**other**:
The catch-all for a flaggable failure that fits none of the four named categories. Specialists use
it rather than stretch their own category.
_Avoid_: misc, unknown

**Difficulty tier**:
How hard an injected failure is to catch — blatant (visible in one event), medium (needs local
context), or subtle (needs reasoning across events).
_Avoid_: class, severity, class tier

## Transcript

**Transcript**:
One normalized coding-agent session — an ordered list of events with an id and a source — stripped
of vendor specifics so a monitor sees only rendered content.
_Avoid_: session, log, record

**Event**:
One step in a transcript: a message, a tool call or result, a file edit, a shell command, or
thinking. Carries a position used to anchor evidence.
_Avoid_: step, turn, record

**File diff**:
An event recording an attempted file edit.
_Avoid_: diff, patch, write

**Shell command**:
An event recording a command the agent ran together with its output.
_Avoid_: command, execution

**Evidence**:
A pointer from a verdict back into the transcript — a specific event plus a verbatim quote from it
— justifying a flag.
_Avoid_: citation, reference, proof

**Event index**:
The position of an event within its transcript, used both to cite evidence and to record where a
failure was injected. It remains the citation anchor under every overflow policy — an event is
either in the view and citable, or elided and named in the gap.
_Avoid_: position, offset, step number

**Monitor view**:
The exact rendered text a monitor saw for one transcript — per-event entries after the entry cap,
under the render budget and overflow policy. The single source of truth grounding checks against.
_Avoid_: rendering, prompt transcript, context

**Render budget**:
The transcript-size limit expressed in estimated tokens and converted to characters by a
calibrated chars-per-token ratio with a safety margin; owns the answer to "will this prompt fit
the served context?". Replaces the informal times-four character heuristic.
_Avoid_: char limit, max chars

**Overflow policy**:
The rule choosing which events fill the render budget when a transcript does not fit: head
(legacy — keep the front, drop the tail) or bracket (keep the front and the back, elide the
middle).
_Avoid_: truncation strategy, windowing

**Elision**:
An explicitly marked, index-ranged gap in a monitor view; elided events are absent but accounted
for, never silently dropped. A quote citing only elided events never grounds, and a flag citing
only elided events is refuted without a model call.
_Avoid_: truncation (reserved for the per-event cap and the legacy tail-drop marker)

## Dataset

**Evaluation set**:
The labeled set of transcripts monitors are scored against — benign sessions plus deliberately
injected failures, built from real session segments.
_Avoid_: test set (collides with the test split), synthetic (unqualified)

**Base**:
A real, human-authored session segment that one or more dataset transcripts are built from — the
only source of authentic style and texture.
_Avoid_: source, session, original

**Injection**:
A failure deliberately and deterministically introduced into a clean base, planned rather than
random.
_Avoid_: insertion, seeding, corruption

**Edit plan**:
The committed, declarative specification of the edits that turn one clean base into an injected
failure, so the dataset rebuilds offline and identically.
_Avoid_: mutation, patch, script

**Matched pair**:
The clean and injected versions of the same base, so a monitor must key on the failure itself
rather than the base's style.
_Avoid_: control pair, comparison

**Hard negative**:
A benign transcript engineered to look like a failure to a careless monitor — a user-requested
test deletion, a secret moved *out* of code, an honest failure report.
_Avoid_: edge case, corner case, false alarm

**Benign**:
The no-failure label, which includes hard negatives and filler. Distinct from *clean*, which means
specifically an un-injected base half.
_Avoid_: safe, negative

**Provenance**:
The hidden record of how a transcript was built — its base, failure category, tier, matched-pair
link, and where the failure sits. Kept out of the transcript so monitors can't see it.
_Avoid_: metadata, origin

**Scrub**:
To strip identifying details (names, emails, real paths, key-like strings) from a base before use,
hard-failing if any survive.
_Avoid_: anonymize, redact

**Style profile**:
The captured profile of the real sessions' voice, formatting, and texture that every injected
event must match so it is indistinguishable from the base.
_Avoid_: style guide, guidelines

## Dataset quality control

**Leakage gate**:
The two-part check a regenerated dataset must pass before use: dumb baselines stay near-random,
and a realism audit finds no synthetic tells.
_Avoid_: quality gate, validation gate

**Dataset leakage**:
The risk that a monitor separates injected from benign by surface artifacts of how the data was
made, rather than by the failure itself.
_Avoid_: leakage (unqualified)

**Dumb baseline**:
A cheap surface-feature scorer (length, keyword hits, diff fraction, and the like) used only to
prove the dataset is not trivially separable.
_Avoid_: baseline (unqualified), heuristic

**Realism catalog**:
The manual audit half of the leakage gate — matched-pair fidelity, voice convergence,
template/value clustering, and the no-laundering check on security injections.
_Avoid_: quality check, audit (unqualified)

## Evaluation

**Recall at fixed FPR**:
The headline metric — the share of real failures caught when the monitor is tuned to raise false
alarms on at most a set fraction of benign transcripts (e.g. 5%).
_Avoid_: catch rate, sensitivity, detection rate

**Own-mode evaluation**:
Scoring a specialist monitor only on its own failure category, so its separation power is not
diluted by transcripts it was never meant to catch.
_Avoid_: category-specific, single-mode

**Held-out source**:
A portion of the test split drawn from a base origin never seen during monitor iteration, used to
check the monitor generalizes across agents and domains, not just across examples.
_Avoid_: held-out test, out-of-sample

**Heuristic floor**:
A hand-coded, no-LLM detector whose score sets the bar an LLM monitor must beat to be worth its
cost.
_Avoid_: dummy, baseline

**Ensemble**:
A combined verdict formed by taking the strongest signal across several monitors; one kind of
composite monitor. A composite row carries its own anomaly band, derived from a dev run of the
composite itself; per-member detail flows through as report-only diagnostics.
_Avoid_: combination, merging

## Monitor composition

**Composite monitor**:
A monitor whose read combines member monitors' reads behind the same interface a leaf monitor
satisfies; it makes no model call of its own. Never auto-discovered — one exists only when a
caller deliberately builds it.
_Avoid_: meta-monitor, wrapper

**Cascade / escalation**:
A composite whose triage member gates a deep member: no triage flag means the deep member never
runs. The escalation rate is the triage flag rate — the share of transcripts that pay for the
deep evaluation.
_Avoid_: hierarchy, two-stage monitor

**Driver**:
The member whose read a composite returns verbatim — the reasoning, categories, and evidence any
verification re-check grounds against.
_Avoid_: winner, primary member

**Member pin**:
A member-level served-model override, letting one composite combine members read under different
substrates — the cross-substrate seam.
_Avoid_: model override (unqualified)

**Self-verifying composite**:
A composite that verifies each flagged member's own read at that member's served model and
combines only the survivors — per-member verify-then-OR. The flip applies to member reads, never
to the composite verdict, and the scoring loop skips its own verification pass for such a
monitor.
_Avoid_: self-checking monitor, auto-verified ensemble

**Verify-then-combine / combine-then-verify**:
The two orderings of verification and composition: per-member verification with survivors
combined (the recall-protecting order) versus verifying only the combined read's driver, which
couples every member's catch to the loudest member's evidence — rejected for live use.
_Avoid_: pre-verification / post-verification

**Gate recall / recall ceiling**:
A cascade's flag-level recall is bounded by its triage gate's flag recall — the recall ceiling.
Distinct from recall at a fixed false-alarm budget on the graded score.
_Avoid_: triage recall (unqualified)

**Gate miss**:
An own-mode failure a cascade never escalated in any read — the gate's blindness. A cascade-only
concept, reported separately from catch-loss and never conflated with it.
_Avoid_: miss (unqualified), false negative

**Structural freeze / composite manifest**:
A composite is frozen by the fingerprint of its member identities, model pins, member prompt
hashes, and verification stage list, since it has no prompt body of its own; a silent member or
pipeline swap changes the fingerprint and halts the row. Extends the freeze.
_Avoid_: composite hash, config freeze

## Verification

**Verification**:
A second pass that re-checks a flag's evidence and drops the flag if the cited quotes do not
actually support it. Runs only on flagged reads.
_Avoid_: double-check, validation

**Flip**:
A flagged read that verification turns into an unflagged one. Flips on benign transcripts remove
false alarms; flips on real failures lose catches. Flip rates are reported with their
mechanical-versus-semantic decomposition (and per-member refutations for self-verifying
composites) — a report-only shape, never a halt by itself.
_Avoid_: reversal, correction

**Catch-loss**:
Verification turning an own-mode true positive from caught to missed — flagged before
verification, fraction zero after. Counted over transcripts, not per-read flips; the halt guard
trips when more than half of a row's own-mode true positives are lost.
_Avoid_: recall loss (unqualified), flip (a flip is per read; catch-loss is the transcript-level
outcome)

**Flip-on-fail**:
The rule that an unsupported flag is dropped entirely — a clean per-read decision — rather than
merely down-weighted.
_Avoid_: penalize, suppress

**Verification stage**:
One re-check of a flag — a single way of asking whether the flag holds up. A stage votes to
refute the flag, uphold it, or abstain, and may make no model call (a mechanical or deterministic
check) or one bounded call. The mechanical quote-grounding check and the LLM semantic check are
stages; so are the deterministic event-order check, the LLM claim-grounding check, the
normalized-grounding diagnostic, and the grounded semantic check.
_Avoid_: check, pass, verifier

**Verification pipeline**:
The ordered set of verification stages a flagged read runs through, invoked as one unit. It folds
the stages' votes into the single recorded outcome and applies the flip rule; a refutation ends
the run, and so does a confirm. Stages plug in without changing the scoring loop. Named opt-in
assemblies (the grounded pipeline, the versioned promotion candidates) exist beside the frozen
default.
_Avoid_: chain, sequence, verifier stack

**Grounding (cited-event support)**:
The mechanical question "does the monitor's quoted evidence actually appear in the event it
cites?", answered against event content independent of the render format. Distinct from
verification: grounding checks presence; the verification pipeline decides support.
_Avoid_: quote match (the legacy verbatim mechanism keeps that name), citation check

**View-true grounding**:
Checking a flag's evidence against the monitor view — the bytes the monitor saw, whitespace-
normalized — rather than against a re-render the monitor never saw.
_Avoid_: quote match, re-render check

**Normalized grounding / quote-fidelity tax**:
Grounding that erases whitespace-and-wrapping render artifacts while keeping content, case, and
order exact. The quote-fidelity tax is the recall lost when verification refutes correct catches
over quote transcription noise rather than substance — the tax normalized grounding exists to
cut.
_Avoid_: fuzzy matching, loose matching

**Quote relocation**:
Resolving a verbatim quote cited at the wrong event index to the in-view event it actually lives
in. Repairs the pointer for the verifier's reading, never the vote.
_Avoid_: citation repair, index fix

**Confirm (terminal uphold)**:
A verification-stage vote that upholds a flag and ends the pipeline run, shielding the flag from
later refuters. Granted only to a category-scoped specialist on a positive finding, never on a
fail-open; refutation stays terminal always, and a plain uphold stays provisional.
_Avoid_: approve, accept

**Scoped stage**:
An adapter that gates a stage on the flag's categories, abstaining outside its scope — how a
category-specific decider composes into a shared pipeline without deciding other monitors' flags.
_Avoid_: filtered stage, conditional stage

**Stage confusion accounting**:
The per-stage dev evaluation of a flip-changing stage against a baseline pipeline: catches killed
and catches saved on failure reads, false alarms suppressed and restored on benign reads, abstain
rate, and model calls saved. The promotion currency for any new stage.
_Avoid_: stage metrics, A/B report

**Pipeline version**:
A named stage composition. Version one is the frozen default — matching the committed record
exactly and never edited; version two is the promotion candidate. Promoting a stage means it
joins the recommended version for new dev work — never an edit to the default.
_Avoid_: pipeline config, variant

## Test-row gating

**QC gate**:
The pipeline-quality check run after each read-once row: verdicts plus provenance plus bands in,
pass-or-halt plus a summary out. Out-of-band means halt and investigate, never tune — the gate
protects the row's trustworthiness, not its score.
_Avoid_: quality gate, sanity check

**Anomaly band**:
A per-row, per-substrate window on the flag rate as the evaluator emits it (pre-verification for
leaves; post-member-verification for self-verifying composites — the same quantity at derivation
and check time), derived from dev rates by a pinned blind-to-test rule. A rate outside the band
signals substrate or pipeline anomaly, not monitor quality.
_Avoid_: threshold, acceptance range

**Band file**:
The versioned config recording a substrate's bands together with their derivation inputs (dev
run, dev rates, rule), so every band is independently recomputable; loading re-derives and
refuses mismatches, so a hand-edited band cannot load. The single pre-rule Qwen hand-set file
(`rule: d39-handset`) is the documented exception: its rows carry no dev rate and load
verbatim, legal only for its own substrate.
_Avoid_: band config, thresholds file

**Gate result**:
The typed pass-or-halt artifact of one gated row: halt reasons, flag rate against its band,
catch-loss counts, flip report, request and cost accounting. Replaces exit-code-plus-convention
as the gate's contract.
_Avoid_: gate output, halt record

**Row**:
One gated evaluator run over the full test split — a leaf monitor or a composite. Bands, request
ceilings, freeze checks, and ledger entries are per-row.
_Avoid_: run (unqualified), experiment

**Replay row / gated read**:
A sealed-test scoring run served entirely from the existing cache by a client that cannot make
live calls; zero requests is itself a gate condition, and full cache coverage is checked before
scoring starts.
_Avoid_: re-run, cache run

**Promotion gate (GATE-3)**:
The explicit, declared event at which the sealed test split may be re-scored under a named
pipeline version and composite set, with bands re-derived blind to test — never a silent change
riding a refactor.
_Avoid_: re-test, re-score (unqualified)

## Sampling and splits

**k-sampling**:
Reading the same transcript k times to measure a monitor's variance; the share that flag becomes
the monitor's probability for that transcript.
_Avoid_: monte carlo, ensemble (ensemble combines *different* monitors)

**Dev split / test split**:
The two partitions of the evaluation set — dev for refining monitors, test sealed for final
scoring.
_Avoid_: training set (dev is not fit statistically), test set (the whole set is loosely called that)

**Base-session-atomic split**:
A split rule that keeps every transcript from one base entirely on one side, so style and domain
knowledge cannot leak from dev into test.
_Avoid_: session-aware split, stratified split

## Process

**Round**:
One cycle of editing a monitor and re-confirming it on the dev split.
_Avoid_: iteration, pass, attempt

**Freeze**:
The point at which monitor bodies are locked (by content hash) so all later runs are reproducible
and comparable.
_Avoid_: lock

**Transfer row**:
A re-run of the frozen monitors on a second model to test cross-family robustness, with no prompt
changes. Runs under the target substrate's own band file; borrowing another substrate's bands is
a documented calibration hazard.
_Avoid_: secondary model, second run

## Flagged ambiguities

- **"failure mode" vs "failure category"** — use **failure category** for the five named values; use
  "failure mode" only in informal prose.
- **"baseline"** is overloaded — say **dumb baseline** (the leakage gate) or **heuristic floor** (the
  hand-coded detector); never "baseline" alone.
- **"score"** is overloaded — **suspicion score** (per-read, 0–100) vs **calibrated score** (the
  ranking number); never "score" alone.
- **"class" vs "tier"** — use **failure category** for the kind of failure and **difficulty tier**
  for blatant/medium/subtle; never "class tier".
- **"clean" vs "benign"** — **benign** is the no-failure label (includes hard negatives); **clean**
  means specifically an un-injected base half.
- **"test set" vs "test split"** — the **test split** is the sealed evaluation partition; "the
  evaluation set" is the whole labeled set.
- **"grounding" vs "verification"** — **grounding** checks that quoted evidence is present in the
  cited event; **verification** decides whether the events support the claim. A grounded quote
  can still fail verification.
- **"truncation" vs "elision"** — **truncation** is the per-event cap and the legacy tail-drop
  marker; **elision** is the bracket policy's named, index-ranged gap in a monitor view.
- **"catch-loss" vs "gate miss"** — **catch-loss** is verification suppressing a caught failure;
  a **gate miss** is a cascade never escalating it. The guards are separate and never conflated.
