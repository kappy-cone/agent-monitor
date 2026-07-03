"""Honest call/cost accounting and the spend ledger for gated rows.

``n_calls`` is provenance- and stage-aware so multi-call pipelines and
composite fan-out are counted as the requests they actually make (the
DECISIONS 45-C6 leaf undercount, closed). The cost convention lives here in
one audited place: list-price-equivalent from ``PRICING``, ``None`` on any
unpriced model (whole-run, never partial), actual $0.00 stated separately by
the adapters. ``quota_day`` keys the request ledger to the primary's free-tier
reset (midnight UTC-7 — the D-era convention).
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

from agentmon.eval.metrics import PRICING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agentmon.schemas import CalibratedVerdict, MonitorVerdict

#: Google's free-tier request day resets at midnight PT (UTC-7 in the D-era runs).
QUOTA_TZ = datetime.timezone(datetime.timedelta(hours=-7))


def member_supported_entries(sample: MonitorVerdict) -> dict[str, bool]:
    """Per-member verification votes behind one draw, walking re-stamped provenance.

    A verified ensemble records ``member_supported`` on its own provenance; a
    cascade re-stamps the returned verdict and carries the member's original
    provenance under ``inner``, so the walk follows the ``inner`` chain. Empty
    for leaves and unverified composites.
    """
    entries: dict[str, bool] = {}
    provenance = sample.provenance
    while provenance is not None:
        entries.update(provenance.member_supported)
        provenance = provenance.inner
    return entries


def n_calls(verdicts: Sequence[CalibratedVerdict]) -> int:
    """Model calls behind a verdict stream, provenance- and stage-aware.

    Per verdict: each sample counts its member draws (``len(member_scores)``
    via provenance — a cascade naturally yields 1 or 2 through escalation, an
    ensemble its full fan-out; leaves count 1) plus per-member verifications
    via ``member_supported``; then parse retries/repairs; then verification
    calls — per-stage outcomes with a model set when an opt-in pipeline
    recorded them, else the folded outcomes — mirroring
    ``CalibratedVerdict._verification_usage`` so calls and cost agree. A
    pipeline injected into ``VerifiedEnsembleMonitor`` stays uncounted
    (bool-only ``member_supported``, combined-only usage fold; deferred with
    the composite live box), as does a nested composite's inner fan-out.
    """
    calls = 0
    for verdict in verdicts:
        for sample in verdict.samples:
            provenance = sample.provenance
            calls += (len(provenance.member_scores) or 1) if provenance is not None else 1
            calls += len(member_supported_entries(sample))
        calls += verdict.parse_retries + verdict.parse_repairs
        if verdict.stage_outcomes is not None:
            calls += sum(
                1 for outcomes in verdict.stage_outcomes if outcomes for o in outcomes if o.model
            )
        else:
            calls += sum(1 for o in verdict.verifications if o is not None and o.model)
    return calls


def run_cost_usd(verdicts: Sequence[CalibratedVerdict]) -> float | None:
    """List-price-equivalent cost of a run; ``None`` when any model is unpriced.

    A partial sum over only the priced verdicts would silently understate
    cost, so one unknown model makes the whole figure ``None`` (the FINDINGS
    cost convention; ``PRICING`` stays in ``metrics.py``).
    """
    total = 0.0
    for verdict in verdicts:
        prices = PRICING.get(verdict.model)
        if prices is None:
            return None
        total += (
            verdict.total_input_tokens * prices[0] + verdict.total_output_tokens * prices[1]
        ) / 1e6
    return total


def quota_day(now: datetime.datetime | None = None) -> str:
    """The ledger day (ISO date) a moment falls in, keyed to the UTC-7 reset."""
    moment = now if now is not None else datetime.datetime.now(tz=QUOTA_TZ)
    return moment.astimezone(QUOTA_TZ).date().isoformat()


class LedgerEntry(BaseModel):
    """One appended spend-ledger record.

    The ``{date, label, model, requests}`` shape both existing ledger files
    (``out/phase3`` and ``out/phase4`` ``requests.jsonl``) already share, now
    typed; field order matches the committed lines.
    """

    date: str
    label: str
    model: str
    requests: int


def append_ledger(path: Path, entry: LedgerEntry) -> None:
    """Append one entry as a JSONL line, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry.model_dump_json() + "\n")


def day_requests(path: Path, day: str) -> int:
    """Total requests recorded for ``day``; 0 when the ledger does not exist."""
    if not path.exists():
        return 0
    total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = LedgerEntry.model_validate_json(line)
        if entry.date == day:
            total += entry.requests
    return total
