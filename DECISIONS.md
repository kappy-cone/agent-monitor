# DECISIONS

Ambiguity calls made during the build, in chronological order. Each was logged in-session
when made; this file is the durable record.

## Scaffold (2026-06-11)

1. Parser grounded in real session logs: Bash `tool_use`+result pairs fold into one
   `shell_command`; Write/Edit/MultiEdit/NotebookEdit become `file_diff`; thinking blocks,
   meta records, and anything malformed or unknown become `other` events — nothing is
   dropped, nothing crashes the parse.
2. The log format records no numeric exit code, so `exit_code` is optional; `is_error` was
   added to `ShellCommand`/`FileDiff` instead (failed and interrupted actions are monitor
   signal).
3. Verdict parse failures score 0 — the monitor's fail-open behavior in deployment — but
   eval counts them as `n_parse_failures` so the failure mode is never invisible.
4. The response cache hashes the *built* prompt + transcript + model + temperature; CLI
   mock runs cache under `cache/mock` so a mock run can never be served to a live run.
5. `recall_at_fpr` flags strictly above the (⌊fpr·n⌋+1)-th highest benign score, so the
   false-alarm budget is never exceeded; `evaluate` dedupes re-run verdicts last-wins and
   rejects conflicting labels.
6. Eval cost uses a built-in per-MTok pricing table; an unknown model makes the figure
   None rather than a silently partial mean.
7. Sidechain (subagent) records parse like main-thread records; `isSidechain` survives
   only in `raw`. Documented limitation, aligned with the hierarchical-monitoring roadmap.

## Synthetic dataset (2026-06-11)

8. Both halves of every matched pair go through the identical pipeline (parse → scrub →
   strip `raw` → uniform `source="synthetic"` → uniform field caps) — otherwise the
   envelope itself would leak the label. Real-base provenance lives in label `notes`.
9. Failure-location ground truth is stored as JSON in `LabeledTranscript.notes`
   (`failure_event_indices` plus class/tier/CWE/pair/base) — no schema change; existing
   consumers unaffected. For benign/hard-negative injected rows the same field marks the
   injected/pattern events, not failures.
10. No API credentials resolve in this environment, so injection edit plans are authored
    by Claude Code subagents (`claude-fable-5`), committed under
    `datasets/synthetic/plans/`, and applied deterministically by `generate.py` — the
    dataset rebuilds offline. `generate.py --author` covers keyed environments.
11. The excluded techacct session seeds the `security_vuln` catalog only, as CWE/OWASP-
    anchored abstractions; a Phase-4B no-laundering check verifies no project-specific
    content reached any public artifact (see `datasets/synthetic/ORIENTATION.md`).
12. Events are assembled directly from schema models rather than `TranscriptBuilder`
    (equivalent, fewer hops); plan ops index against the clean transcript's numbering.
13. With 73 benign transcripts, recall@1%FPR degenerates to a zero-false-alarm threshold
    (k=0). Documented rather than fixed; reaching k=1 needs ≥100 benign — a scale-up item.
14. First Phase-4A run failed on `file_diff_fraction` (0.662): failure injections add
    diffs, plain benign keeps base rates. Rebalanced by adding 12 benign-filler rows —
    ordinary in-scope work injected through the identical machinery — rather than thinning
    failure content. Gate results recorded in `datasets/synthetic/README.md`.
15. Phase-4B found a format blocker (unified-diff conventions in injected diffs, 0.677 to
    a format-counting baseline) plus voice-convergence and fabrication-consistency
    defects. Format classes were fixed *structurally* (uniform diff/output normalization
    in the pipeline, new `unified_diff_markers` baseline in the gate) rather than per
    plan; content defects were fixed per plan and re-verified against the regenerated
    transcripts. Transcript ids intentionally encode class/tier: monitors never see ids,
    and labels are public in an eval set — navigability wins.
