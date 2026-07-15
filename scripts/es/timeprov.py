"""Time provenance for the ES lens (design 10).

Verdicts carry NO timestamp. Time exists only in a run's ``summary.json``, and it
is not uniformly present. Kibana is a time-oriented tool, so an exporter is under
constant temptation to manufacture a time axis. This module is the ONE place a
fabrication could enter, so it lives alone, is pure, and is unit-tested.

The ruling (DECISIONS, design 10): ``@timestamp`` is the run's DECLARED time at the
run's TRUE precision, with a mandatory sibling provenance stamp. The run is the
temporal event. No backfill, no jitter, no inference.

Ladder:
  1. ``summary.json:timestamp``  -> verbatim,      precision=second, trusted=True
  2. ``summary.json:date``       -> T00:00:00Z,    precision=day,    trusted=True
  3. neither                     -> export time,   precision=none,   trusted=False

REJECTED, deliberately:

* **File mtime.** Not demoted — deleted. It is provably wrong here, not merely weak:
  all five ``results/gemma-qwen-test-matrix/qwen/*`` share mtime ``2026-06-18`` while
  every one of them declares ``"date": "2026-06-15"``. mtime is off by three days and
  records a *copy*, not the run. A field that is measurably wrong must not be indexed
  behind a boolean an analyst un-filters in one click.
* **Spreading draws within a run by cumulative ``latency_ms``.** Draw order is a write
  order, not a wall clock, and a cache hit's latency is not wall-clock time at all.
  It would manufacture a timeline with a plausible-looking derivation — worse than an
  obvious fabrication, because it survives review.

Consequence we do not paper over: **within a run, time is degenerate.** Every draw of
a run shares one instant. The date histogram is meaningful only ACROSS runs, where a
run legitimately reads as a burst. No panel may imply intra-run time evolution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Provenance = Literal["run_meta_timestamp", "run_meta_date", "none"]
Precision = Literal["second", "day", "none"]


@dataclass(frozen=True)
class TimeStamp:
    """A resolved ``@timestamp`` plus the provenance that makes it auditable."""

    timestamp: str
    provenance: Provenance
    precision: Precision
    trusted: bool
    source_field: str

    def as_fields(self) -> dict[str, Any]:
        """The ES document fields. ``agentmon.time.*`` is a sibling of @timestamp so a
        reader can always see how the instant was obtained."""
        return {
            "@timestamp": self.timestamp,
            "agentmon": {
                "time": {
                    "provenance": self.provenance,
                    "precision": self.precision,
                    "trusted": self.trusted,
                    "source_field": self.source_field,
                }
            },
        }


def resolve(summary: dict[str, Any], *, export_time: str) -> TimeStamp:
    """Resolve one run's ``@timestamp`` from its summary, honestly.

    ``export_time`` (ISO8601) is the caller's single clock read, used only for the
    untrusted fallback so every fallback doc in one export shares one instant.
    """
    declared = summary.get("timestamp")
    if isinstance(declared, str) and declared.strip():
        return TimeStamp(
            timestamp=declared,
            provenance="run_meta_timestamp",
            precision="second",
            trusted=True,
            source_field="summary.json:timestamp",
        )

    day = summary.get("date")
    if isinstance(day, str) and day.strip():
        # Floored to midnight, NEVER spread across the day. All docs in the run share
        # one instant, which honestly says "we know the day, not the time".
        return TimeStamp(
            timestamp=f"{day.strip()}T00:00:00Z",
            provenance="run_meta_date",
            precision="day",
            trusted=True,
            source_field="summary.json:date",
        )

    return TimeStamp(
        timestamp=export_time,
        provenance="none",
        precision="none",
        trusted=False,
        source_field="none",
    )
