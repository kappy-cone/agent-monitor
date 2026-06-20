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
failure was injected.
_Avoid_: position, offset, step number

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
A combined verdict formed by taking the strongest signal across several monitors.
_Avoid_: combination, merging

## Verification

**Verification**:
A second pass that re-checks a flag's evidence and drops the flag if the cited quotes do not
actually support it. Runs only on flagged reads.
_Avoid_: double-check, validation

**Flip**:
A flagged read that verification turns into an unflagged one. Flips on benign transcripts remove
false alarms; flips on real failures lose catches.
_Avoid_: reversal, correction

**Flip-on-fail**:
The rule that an unsupported flag is dropped entirely — a clean per-read decision — rather than
merely down-weighted.
_Avoid_: penalize, suppress

**Verification stage**:
One re-check of a flag — a single way of asking whether the flag holds up. A stage votes to
refute the flag, uphold it, or abstain, and may make no model call (a mechanical or deterministic
check) or one bounded call. The mechanical quote-grounding check and the LLM semantic check are
stages; so are the deterministic event-order check and the LLM claim-grounding check.
_Avoid_: check, pass, verifier

**Verification pipeline**:
The ordered set of verification stages a flagged read runs through, invoked as one unit. It folds
the stages' votes into the single recorded outcome and applies the flip rule; a refutation ends
the run. Stages plug in without changing the scoring loop.
_Avoid_: chain, sequence, verifier stack

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
changes.
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
