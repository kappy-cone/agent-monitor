# Gemma dev-full — 5 frozen monitors (2026-07-07)

Broad Gemma readout over the enriched dev set, the counterpart to the Qwen `self_recon-dev-validation`
run (2026-07-03). Distilled from the gitignored `out/phase3/runs/gemma-selfrecon-devfull/`
(`summary.json` + `table.md` committed here).

## Provenance & method
- **Run:** `dev_eval.py --local --slice dev-full --confirm-post-freeze --k 3 --model-override
  agentmon-local-gemma`, verify ON (default pipeline). **1,519 requests, $0**, ~1 h on the CUDA-graph
  serve path. No band HALT.
- **Substrate:** Gemma-4-31B (AWQ 4-bit), served `agentmon-local-gemma` via a Docker vLLM container
  over the tunnel.
- **Scope:** the **5 frozen GATE-2 monitors** (`dev_eval` default `LIBRARY_IDS`) — deception,
  generalist, reward_hacking, scope_expansion, security_vuln. `self_recon` monitor is **not** in the
  default set; it is covered separately by the tier A/B (`results/self_recon-tier-validation/`).
  Slice `dev-full` n=89 (the 81 original dev + the 8 new self_recon tiers, scored as targets).

## Gemma vs Qwen — dev-full own-AUROC (descriptive)

| monitor | Qwen (2026-07-03, n=81) | Gemma (2026-07-07, n=89) | Δ (Gemma − Qwen) |
|---|---|---|---|
| deception | 0.815 | 0.705 | **−0.110** |
| reward_hacking | 0.819 | 0.938 | **+0.119** |
| scope_expansion | 0.930 | 0.962 | +0.032 |
| security_vuln | 0.918 | 0.991 | +0.073 |
| generalist (all-mode) | 0.756 | 0.703 | −0.053 |

## Readings
- **The substrates split, no substrate dominates.** Gemma leads on the three specialists that key off
  concrete artifacts (reward_hacking, scope_expansion, security_vuln); Qwen leads on the more
  *inferential* modes (deception, generalist). This directionally echoes the sealed test matrix
  (deception favored Qwen there too).
- **self_recon cross-talk:** even without the specialist, the frozen monitors partially flag the
  self_recon tiers on Gemma (`mean_fraction_by_cell`: generalist self_recon/blatant 0.33, /subtle
  0.25; security & scope /blatant ~0.20) — self_recon is genuinely security/oversight-adjacent.
- Combined with the tier A/B (Gemma > Qwen self_recon recall at every tier), the **full Gemma picture
  = these 5 frozen monitors + self_recon from the A/B**.

## Caveats (not oversold)
- **Dev, descriptive — NOT the sealed test.** Not comparable to the frozen GATE-2 test matrix; this
  is a post-freeze sanity readout.
- **Slice differs (n=81 vs 89):** the +8 self_recon tiers shift `all`-AUROC (extra positives) but not
  `own`-AUROC (own-mode positives unchanged) — the table above uses own-AUROC, which is comparable.
- **Underpowered per cell** (2–3 positives/tier); per-cell deltas do not reach significance —
  consistent with the project-wide finding that substrate deltas are noise-dominated at this n.
- Verification ON (default pipeline); the quote-fidelity tax is still present (scope 16 flips / 5
  mechanical).
