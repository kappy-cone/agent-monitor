"""B1 (design 08): the dev-only ``indirect_injection`` ground-truth label.

Pins the decoupled state — the label exists so data can be labeled with it before
its specialist ships (B4), while staying out of the composite gate (like
self_recon). When the specialist and sealed-test rows land, INDIRECT_INJECTION
graduates into ALL_MODES and these expectations change deliberately.
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


def test_no_monitor_emits_indirect_injection_yet() -> None:
    # The label exists before its specialist (B4). No prompt file declares it, so
    # the monitor library carries no indirect_injection specialist today.
    from agentmon.monitors.registry import load_monitors

    monitors = load_monitors()
    assert "indirect_injection" not in monitors
    for monitor in monitors.values():
        assert FailureCategory.INDIRECT_INJECTION not in monitor.config.categories


def test_rejects_unknown_label() -> None:
    with pytest.raises(ValidationError):
        LabeledTranscript(transcript_id="t1", label="not_a_mode")
