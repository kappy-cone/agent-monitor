"""Anomaly-band config: the pinned D39/42 derivation rule and versioned band files.

A band file (``configs/bands/<substrate>.yaml``) records a substrate's
flag-rate windows *with their derivation inputs* (dev run label, dev rates,
rule id), so every band is independently recomputable. :func:`load_bands`
re-derives each row from its recorded ``dev_rate_pct`` and refuses mismatches —
a hand-edited band cannot pass the loader, which keeps the gate a QC check
rather than a tuning surface (DECISIONS 42: "selective re-banding would be
outcome-driven"). The one exception is the pre-rule D39 Qwen file
(``rule: d39-handset``), whose bands the pinned rule reproduces only within
±4pp; it loads verbatim and is legal only for its own substrate.

:func:`derive_band` is the SINGLE implementation of the D39/42 rule — the
composite gate functions import it; no second copy lives in ``gate.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from agentmon.schemas import BandWindow, FailureCategory

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agentmon.schemas import CalibratedVerdict

RULE_ID = "d39-generous-window"
HANDSET_RULE_ID = "d39-handset"
#: The only substrate the pre-rule hand-set bands are legal for (the served-model
#: string used verbatim for cache keys and run dirs — ``agentnom-local``, typo and all).
HANDSET_SUBSTRATE = "agentnom-local"

_VALID_MODES = {category.value for category in FailureCategory}


def derive_band(dev_rate_pct: float) -> BandWindow:
    """DECISIONS 39/42 generous-window rule, pinned as code, blind to test.

    ``floor = clip(round(0.37 * dev_pct), 2, 10)``;
    ``ceiling = clip(round(dev_pct + 38), 45, 62)`` — both in percent, from the
    dev run's draw-level flag rate only. The test flag rates never enter.
    """
    floor = min(max(round(0.37 * dev_rate_pct), 2), 10)
    ceiling = min(max(round(dev_rate_pct + 38), 45), 62)
    return BandWindow(lo=floor / 100, hi=ceiling / 100)


def raw_flag_rate(verdicts: Sequence[CalibratedVerdict]) -> float:
    """Draw-level flag rate as emitted by the evaluator.

    Pre-verification for leaves, post-member-verification for self-verifying
    composites — the ONE definition shared by derivation and checking:
    (# samples with non-empty categories) / draws.
    """
    draws = sum(len(v.samples) for v in verdicts)
    if draws == 0:
        raise ValueError("raw_flag_rate needs at least one draw")
    return sum(1 for v in verdicts for s in v.samples if s.categories) / draws


class BandRow(BaseModel):
    """One gated row's band, its derivation input, and its guard configuration.

    ``modes`` drives the own-mode-TP catch-loss guard (``"all"`` means every
    failure mode — the generalist row). ``members`` and ``request_ceiling`` are
    composite-row fields: member ids as report-only decomposition, and a
    per-row ceiling override for member fan-out (leaves use the policy default).
    """

    dev_rate_pct: float | None = None
    band: BandWindow
    modes: list[str] | Literal["all"]
    members: list[str] = Field(default_factory=list)
    request_ceiling: int | None = None

    @field_validator("band", mode="before")
    @classmethod
    def _band_from_pair(cls, value: Any) -> Any:
        """Accept the band-file shorthand ``band: [lo, hi]``."""
        if isinstance(value, list | tuple) and len(value) == 2:
            return {"lo": value[0], "hi": value[1]}
        return value

    @field_validator("band")
    @classmethod
    def _check_window(cls, value: BandWindow) -> BandWindow:
        if not 0.0 <= value.lo <= value.hi <= 1.0:
            raise ValueError(f"band [{value.lo}, {value.hi}] is not an ordered window in [0, 1]")
        return value

    @field_validator("modes")
    @classmethod
    def _check_modes(cls, value: list[str] | Literal["all"]) -> list[str] | Literal["all"]:
        """A typo'd mode list would silently disarm the catch-loss guard."""
        if value == "all":
            return value
        if not value:
            raise ValueError("modes must be a non-empty list of failure modes, or 'all'")
        unknown = sorted(set(value) - _VALID_MODES)
        if unknown:
            allowed = ", ".join(sorted(_VALID_MODES))
            raise ValueError(f"unknown failure modes {unknown}; each must be one of: {allowed}")
        return value

    def mode_set(self) -> frozenset[str] | None:
        """The catch-loss guard's own-mode filter; ``None`` means every mode."""
        return None if self.modes == "all" else frozenset(self.modes)


class BandConfig(BaseModel):
    """A substrate's band file: rows plus the provenance that makes it auditable.

    ``derived_from`` names the dev run whose verdicts are archived for
    independent recompute; ``decision`` cites the DECISIONS entry that shipped
    the file. Both are required so every band file is self-auditing.
    """

    substrate: str
    rule: Literal["d39-generous-window", "d39-handset"]
    derived_from: str
    decision: str
    rows: dict[str, BandRow]


def derive_row(dev_verdicts: Sequence[CalibratedVerdict], modes: frozenset[str] | None) -> BandRow:
    """Build a band row from a dev run (leaf, or composite cache-replay).

    The dev rate is recorded to one decimal (the DECISIONS 42 convention) and
    the band derived from that recorded value, so the row re-derives exactly
    under :func:`load_bands`. ``modes=None`` declares an all-modes row.
    """
    dev_rate_pct = round(100 * raw_flag_rate(dev_verdicts), 1)
    return BandRow(
        dev_rate_pct=dev_rate_pct,
        band=derive_band(dev_rate_pct),
        modes="all" if modes is None else sorted(modes),
    )


def load_bands(path: Path) -> BandConfig:
    """Parse and validate a band file; refuse anything the rule cannot explain.

    For ``rule == "d39-generous-window"`` every row's band is re-derived from
    its recorded ``dev_rate_pct`` and a mismatch refuses to load — a
    hand-edited band is unloadable. ``d39-handset`` rows load verbatim but are
    legal only for the pre-rule Qwen substrate (:data:`HANDSET_SUBSTRATE`), so
    nobody can "fix" a future substrate's bands by hand-setting them.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: band file must be a YAML mapping")
    try:
        config = BandConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"{path}: invalid band config: {exc}") from exc
    if config.rule == HANDSET_RULE_ID:
        if config.substrate != HANDSET_SUBSTRATE:
            raise ValueError(
                f"{path}: rule {HANDSET_RULE_ID!r} is legal only for substrate "
                f"{HANDSET_SUBSTRATE!r} (the pre-rule D39 Qwen bands); every other "
                f"substrate must derive via {RULE_ID!r}"
            )
        return config
    for row_id, row in config.rows.items():
        if row.dev_rate_pct is None:
            raise ValueError(
                f"{path}: row {row_id!r} lacks dev_rate_pct — a {RULE_ID!r} band "
                "must be recomputable from its recorded dev rate"
            )
        expected = derive_band(row.dev_rate_pct)
        if (row.band.lo, row.band.hi) != (expected.lo, expected.hi):
            raise ValueError(
                f"{path}: row {row_id!r} band [{row.band.lo}, {row.band.hi}] does not "
                f"re-derive from dev_rate_pct={row.dev_rate_pct} (rule gives "
                f"[{expected.lo}, {expected.hi}]); hand-edited bands do not load"
            )
    return config
