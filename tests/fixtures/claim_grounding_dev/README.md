# Claim-grounding dev validation fixtures

Six **dev** deception cases, vendored verbatim from the `dataset-expansion` branch
(`datasets/synthetic/transcripts/` + the matching `labels.jsonl` rows), used to
validate the opt-in `ClaimGroundingStage` (`agentmon.verification`) on real dev
data without a live model.

Each case injects a false factual/causal claim that a visible event in the same
transcript contradicts (blatant: "only X touched / build passed" vs a
forbidden-file diff or an exit-1 output; medium: a claimed verification with no
supporting event; subtle: a misattributed cause or overstated equivalence). See
DECISIONS 41–42 and the dataset README's "Extension — dev deception cases".

- `dec-{bla,med,sub}-0{6,7}.json` — the transcripts.
- `ground_truth.jsonl` — the labels: `transcript_id` + `notes` JSON carrying
  `failure_event_indices` (the contradicting events) and `spec` (the false claim).

These are **copies for a self-contained, mock-first test** — they deliberately do
not touch `datasets/` on this branch, so the sealed test split, `split.json`, and
the leakage gate are all unaffected. To refresh them, re-vendor from
`dataset-expansion`.
