# Render-budget calibration — committed test-matrix sample records (offline, $0)

Rule (pinned in scripts/calibrate_render_budget.py before the numbers were read):
pinned cpt = max(3.0, floor(p05 * 10) / 10); p05 = sorted ratio at index floor(0.05*n).
Coverage (estimate_tokens >= input_tokens under the pinned cpt) is REPORT-ONLY.

| substrate | served model | n | mean | p05 | min | pinned cpt | coverage |
|-----------|--------------|---|------|-----|-----|------------|----------|
| qwen | agentnom-local | 990 | 3.4219 | 3.0429 | 2.9936 | 3.0 | 0.9970 |
| gemma | agentmon-local-gemma | 990 | 3.2183 | 2.8400 | 2.7714 | 3.0 | 0.7758 |

Unknown-model fallback ("" entry): min of the per-substrate pins = 3.0.
Pinned into `_CALIBRATED_CPT` in src/agentmon/rendering.py.
