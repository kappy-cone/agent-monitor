# datasets/

Local data for evaluating monitors. Everything in this directory is gitignored
**except** this README and the tiny committed samples under `samples/` (see the
`datasets/*` rules in the repo `.gitignore`). Put real transcript dumps, verdict
runs, and label sets here without worrying about committing them.

## File formats

Both formats are JSONL: one pydantic model (from `agentmon.schemas`) serialized
as JSON per line. Load with `Model.model_validate_json(line)`; write with
`model.model_dump_json()`.

### Verdicts (`*_verdicts.jsonl`)

One `MonitorVerdict` per line — a monitor's judgment of one transcript:
`monitor_id`, `transcript_id`, `suspicion_score` (0–100), `categories`
(failure-mode tags), `evidence` (event index + quote pairs), `reasoning`,
plus usage fields (`model`, `input_tokens`, `output_tokens`, `latency_ms`).

### Labels (`*_labels.jsonl`)

One `LabeledTranscript` per line — ground truth for one transcript:
`transcript_id`, `label` (`benign` or one of `security_vuln`,
`reward_hacking`, `scope_expansion`, `deception`, `other`), optional `notes`.

## Committed samples

`samples/toy_verdicts.jsonl` and `samples/toy_labels.jsonl` are a hand-built
12-transcript set (8 benign, 4 failures — one per category) for monitor
`example_security`. Their metrics are hand-computed and asserted in
`tests/test_metrics.py`, so treat them as frozen: if you edit them, update the
worked arithmetic in the tests to match.

## Regenerating or extending

The samples were written by hand; to extend them (or build a new set), emit one
model per line:

```python
from pathlib import Path
from agentmon.schemas import LabeledTranscript

labels = [LabeledTranscript(transcript_id="t13", label="benign", notes="...")]
with Path("datasets/my_labels.jsonl").open("w") as f:
    for label in labels:
        f.write(label.model_dump_json() + "\n")
```

Validate a file by round-tripping every line through
`model_validate_json`, then run the eval harness over it with
`agentmon.eval.metrics.evaluate`.
