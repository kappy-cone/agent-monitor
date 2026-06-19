# gemma-qwen-test-matrix

Durable, version-tracked archive of the two **local-substrate TEST runs** of the agentmon
monitor matrix, captured **before** the Gemma-native re-band so the read-once results can be
referenced unchanged. The live `out/` run directory is gitignored; this folder is the committed
copy.

## Layout
```
ANALYSIS.md                 ← detailed run-by-run comparison (start here)
gemma/                      ← Gemma-4-31B (AWQ-4bit, agentmon-local-gemma)
  test-<monitor>/summary.json, verdicts.jsonl
  logs/                     ← per-row console logs + matrix progress log
qwen/                       ← Qwen-3.6-35B (NVFP4, agentnom-local)
  test-<monitor>/summary.json, verdicts.jsonl
  logs/
```
Five monitors each: `security_vuln, scope_expansion, reward_hacking, deception, generalist`.

## What this is
- **Read-once test matrix**, test split n=66 (31 failures / 35 benign), k=3 + verification,
  frozen GATE-2 monitor bodies (5cfdab1); substrate enters via `model_override` only — no body
  edits. $0, self-hosted on the OMEN (RTX 5090) via SSH tunnel.
- Both substrates are 4-bit (Gemma AWQ, Qwen NVFP4) and ran the **complete** matrix — the first
  full test-vs-test local-substrate comparison.

## Caveats (read before citing)
- **Bands are the borrowed Qwen windows** (DECISIONS 39), reused unchanged on Gemma — a
  deliberate fixed-operating-point comparison. The `deception` HALT is a borrowed-band
  calibration artifact; statuses here are **descriptive, not final**. The Gemma-native re-band
  re-judges all five rows.
- The `deception` discrimination deficit (own-AUROC 0.688 vs 0.775) is real as a point estimate
  but **underpowered** (90% CI on the delta [-0.25, +0.05] straddles 0). Its re-gate decision is
  **pending** in the working session, not finalized here.
- `verdicts.jsonl` carries each draw's raw model output, parsed verdict, and bounded
  verification outcome. All over synthetic, scrubbed, public-bound transcripts.

## Reproduce the numbers
`ANALYSIS.md` is generated directly from the `verdicts.jsonl` files in this folder, so every
figure recomputes from the committed data alone.
