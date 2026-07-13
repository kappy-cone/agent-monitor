"""B1 (design 08): the dev-only ``indirect_injection`` ground-truth label.

Pins the decoupled state — the mode stays out of the composite gate (like
self_recon). The B4 specialist has since shipped (a dev-only prompt now declares
the mode; see ``test_indirect_injection_monitor``), but the mode remains
gate-excluded until sealed-test rows and a GATE-3 re-freeze land, at which point
INDIRECT_INJECTION graduates into ALL_MODES and these expectations change
deliberately.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentmon.eval.gate import _COMPOSITE_GATE_EXCLUDED, ALL_MODES
from agentmon.schemas import FailureCategory, LabeledTranscript


def test_indirect_injection_is_a_failure_category() -> None:
    assert FailureCategory.INDIRECT_INJECTION.value == "indirect_injection"


def test_transcript_can_be_labeled_indirect_injection() -> None:
    labeled = LabeledTranscript(transcript_id="t1", label="indirect_injection")
    assert labeled.label == "indirect_injection"


def test_indirect_injection_is_dev_only_and_gate_excluded() -> None:
    # Ground-truth-first: no frozen specialist / sealed-test rows yet, so it must
    # not be scored by the composite gate.
    assert FailureCategory.INDIRECT_INJECTION.value in _COMPOSITE_GATE_EXCLUDED
    assert FailureCategory.INDIRECT_INJECTION.value not in ALL_MODES


def test_only_the_specialist_emits_indirect_injection() -> None:
    # B4: the dev-only specialist now ships and declares the mode, but the mode
    # stays gate-excluded (above) until sealed-test rows + a GATE-3 re-freeze
    # exist. No OTHER monitor — none of the frozen five, nor self_recon — emits it.
    from agentmon.monitors.registry import load_monitors

    monitors = load_monitors()
    assert "indirect_injection" in monitors
    assert monitors["indirect_injection"].config.categories == [FailureCategory.INDIRECT_INJECTION]
    for monitor_id, monitor in monitors.items():
        if monitor_id == "indirect_injection":
            continue
        assert FailureCategory.INDIRECT_INJECTION not in monitor.config.categories


def test_rejects_unknown_label() -> None:
    with pytest.raises(ValidationError):
        LabeledTranscript(transcript_id="t1", label="not_a_mode")
