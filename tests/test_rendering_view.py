"""Property tests for the monitor view, the bracket overflow policy, and the
token-aware budget (architecture candidate 4, Phase R1; designs 01 + 05).

The four R1 properties:

(a) defaults => byte-identity: the head policy reproduces ``render_transcript``
    for every budget (``render_transcript`` delegates to ``build_view``, so the
    byte census in test_rendering.py guards the same path);
(b) under bracket at >=32k-char budgets every DEV-split ground-truth
    ``failure_event_indices`` entry survives — asserted on dev only. The
    test-split counterfactual (the measured 9->0 at the 8,192-token budget) is
    computed in the same test but REPORT-ONLY, never asserted: tuning against
    test-split evidence positions would not be blind-to-test.
(c) ``len(view.text) <= max_chars`` across the committed corpus and swept
    budgets (the pinned degenerate escape — a bare marker at budgets smaller
    than the marker itself — is exercised separately);
(d) elided ranges tile exactly the dropped indices and every in-view index is
    citable.

Mock-free and offline: rendering is pure, no client is ever constructed.
"""

from __future__ import annotations

import math
from pathlib import Path

from agentmon.eval.split import Provenance, load_labels, load_split, parse_provenance
from agentmon.rendering import (
    _CALIBRATED_CPT,
    RenderBudget,
    build_view,
    estimate_tokens,
    preflight_fits,
    render_event,
    render_transcript,
)
from agentmon.schemas import Transcript, UserMessage

REPO = Path(__file__).resolve().parents[1]
DATASET = REPO / "datasets" / "synthetic"
TRANSCRIPTS = DATASET / "transcripts"
TRANSCRIPT_PATHS = sorted(TRANSCRIPTS.glob("*.json"))

#: Budgets swept by the structural properties (c) and (d): small enough to force
#: both policies' overflow paths on real data, large enough that every marker fits.
SWEEP_BUDGETS = (300, 1000, 5000, 8192, 32000, 64000)

#: The (b) assertion domain: every >=32k-char budget named by design 05 R1,
#: including 32768 = the 8,192-token Gemma budget at the legacy 4 chars/token.
EVIDENCE_BUDGETS = (32000, 32768, 40000, 48000)


def chars_budget(max_chars: int) -> RenderBudget:
    """A RenderBudget whose max_chars is exactly ``max_chars``."""
    return RenderBudget(max_tokens=max_chars, chars_per_token=1.0)


def _load(path: Path) -> Transcript:
    return Transcript.model_validate_json(path.read_text(encoding="utf-8"))


def _load_id(transcript_id: str) -> Transcript:
    return _load(TRANSCRIPTS / f"{transcript_id}.json")


def _failure_provenances() -> list[Provenance]:
    labels = load_labels(DATASET / "labels.jsonl")
    return [p for p in map(parse_provenance, labels) if p.failure_event_indices]


def _uniform_transcript(n: int = 10) -> Transcript:
    """n events of exactly 120 rendered chars each — the census fixture shape."""
    events = [UserMessage(index=i, text=f"message {i} " + "y" * 100) for i in range(n)]
    return Transcript(id="t-uniform", source="synthetic", events=events)


# --- (a) defaults => byte-identity with the legacy renderer ---


def test_head_view_text_byte_equals_render_transcript() -> None:
    """For every committed transcript and every budget, head view == legacy render."""
    for path in TRANSCRIPT_PATHS:
        transcript = _load(path)
        for budget in (50, 150, 300, 64000):
            view = build_view(transcript, chars_budget(budget), "head")
            assert view.text == render_transcript(transcript, max_chars=budget), (
                path.stem,
                budget,
            )


def test_default_policy_is_head() -> None:
    transcript = _uniform_transcript()
    assert build_view(transcript, chars_budget(300)).text == render_transcript(
        transcript, max_chars=300
    )


def test_bracket_equals_head_when_nothing_overflows() -> None:
    """0/133 committed transcripts overflow the production budget: policies coincide."""
    for path in TRANSCRIPT_PATHS:
        transcript = _load(path)
        head = build_view(transcript, chars_budget(64000), "head")
        bracket = build_view(transcript, chars_budget(64000), "bracket")
        assert head.elided == () and bracket.elided == (), path.stem
        assert bracket.text == head.text, path.stem
        assert bracket.events == head.events, path.stem


# --- (b) evidence preservation: asserted on dev, reported on test ---


def test_dev_failure_evidence_survives_bracket_at_32k_budgets() -> None:
    """ASSERTED (dev only): bracket never elides a dev ground-truth failure event.

    The test-split counterfactual is computed below but only printed — never
    asserted — per the blind-to-test rule (design 05 R1, merge note M3).
    """
    split = load_split(DATASET / "split.json")
    failures = _failure_provenances()
    dev = [p for p in failures if split.side_of(p.transcript_id) == "dev"]
    assert dev, "no dev-split failure transcripts with ground-truth indices"

    overflowing = 0
    for prov in dev:
        transcript = _load_id(prov.transcript_id)
        for budget in EVIDENCE_BUDGETS:
            view = build_view(transcript, chars_budget(budget), "bracket")
            if view.elided:
                overflowing += 1
            for index in prov.failure_event_indices:
                assert view.event_text(index) is not None, (
                    prov.transcript_id,
                    budget,
                    index,
                )
    # Non-vacuity: dev failure renders reach ~32.5k chars, so the 32,000-char
    # budget forces at least one dev bracket view to actually elide.
    assert overflowing >= 1

    # REPORT-ONLY test-split counterfactual at the 8,192-token (32,768-char)
    # budget the Gemma incident first collided with. Expected from the design's
    # offline measurement: head drops ground-truth evidence in 9 overflowing
    # failure transcripts, bracket in 0. Printed, never asserted.
    test_side = [p for p in failures if split.side_of(p.transcript_id) == "test"]
    head_drops = 0
    bracket_drops = 0
    overflow_count = 0
    for prov in test_side:
        transcript = _load_id(prov.transcript_id)
        head = build_view(transcript, chars_budget(32768), "head")
        bracket = build_view(transcript, chars_budget(32768), "bracket")
        if head.elided or bracket.elided:
            overflow_count += 1
        if any(head.event_text(i) is None for i in prov.failure_event_indices):
            head_drops += 1
        if any(bracket.event_text(i) is None for i in prov.failure_event_indices):
            bracket_drops += 1
    print(
        f"[report-only] test-split counterfactual @32768 chars: "
        f"{overflow_count} failure transcripts overflow; head drops evidence in "
        f"{head_drops}, bracket in {bracket_drops}"
    )


# --- (c) budget respected and (d) elision tiles the dropped indices ---


def test_view_never_exceeds_budget_and_elision_tiles_dropped_indices() -> None:
    for path in TRANSCRIPT_PATHS:
        transcript = _load(path)
        all_indices = {event.index for event in transcript.events}
        for budget in SWEEP_BUDGETS:
            for policy in ("head", "bracket"):
                view = build_view(transcript, chars_budget(budget), policy)
                where = (path.stem, budget, policy)
                # (c) the view text never exceeds the budget.
                assert len(view.text) <= budget, where
                # (d) elided ranges are well-formed and tile the dropped indices.
                assert len(view.elided) <= 1, where
                in_view = [event.index for event in view.events]
                assert in_view == sorted(in_view), where
                elided: set[int] = set()
                for first, last in view.elided:
                    assert first <= last, where
                    elided |= set(range(first, last + 1))
                assert set(in_view) | elided == all_indices, where
                assert not set(in_view) & elided, where
                # Every in-view index is citable; every elided index is not.
                for event in view.events:
                    assert view.event_text(event.index) == event.text, where
                for index in elided:
                    assert view.event_text(index) is None, where


# --- bracket fixtures: marker format, tail preservation, degenerate escape ---


def test_bracket_marker_names_the_exact_elided_range() -> None:
    transcript = _uniform_transcript()
    view = build_view(transcript, chars_budget(400), "bracket")
    assert len(view.elided) == 1
    (first, last) = view.elided[0]
    assert 0 < first <= last < 9  # head and tail both kept something
    elided_chars = sum(
        len(render_event(event)) for event in transcript.events if first <= event.index <= last
    )
    count = last - first + 1
    marker = f"[events {first}..{last} elided: {count} events, {elided_chars} chars]"
    lines = view.text.split("\n")
    assert lines.count(marker) == 1
    kept = [event.text for event in view.events]
    marker_at = lines.index(marker)
    assert lines == [*kept[:marker_at], marker, *kept[marker_at:]]
    assert lines[0].startswith("[0] USER:")
    assert lines[-1].startswith("[9] USER:")


def test_bracket_preserves_late_evidence_where_head_drops_it() -> None:
    events = [
        UserMessage(index=i, text=(f"filler {i} " + "z" * 80) if i < 29 else "sekrit-evidence")
        for i in range(30)
    ]
    transcript = Transcript(id="t-late", source="synthetic", events=events)
    head = build_view(transcript, chars_budget(800), "head")
    bracket = build_view(transcript, chars_budget(800), "bracket")
    assert head.event_text(29) is None
    assert "sekrit-evidence" not in head.text
    assert bracket.event_text(29) is not None
    assert "sekrit-evidence" in bracket.text


def test_bracket_degenerate_bare_marker_when_nothing_fits() -> None:
    """Mirrors the head policy's pinned escape: the bare marker returns even
    when it overshoots an absurd budget — elision is accounted, never silent."""
    events = [UserMessage(index=i, text="y" * 100) for i in range(3)]
    transcript = Transcript(id="t-bare", source="synthetic", events=events)
    view = build_view(transcript, chars_budget(10), "bracket")
    total = sum(len(render_event(event)) for event in events)
    assert view.text == f"[events 0..2 elided: 3 events, {total} chars]"
    assert view.events == ()
    assert view.elided == ((0, 2),)


def test_head_view_elided_range_covers_the_dropped_tail() -> None:
    transcript = _uniform_transcript()
    view = build_view(transcript, chars_budget(300), "head")
    assert view.text == render_transcript(transcript, max_chars=300)
    assert view.elided == ((2, 9),)
    assert [event.index for event in view.events] == [0, 1]
    assert view.event_text(5) is None


# --- ViewEvent metadata and MonitorView lookups ---


def test_view_event_records_per_event_truncation() -> None:
    big = UserMessage(index=0, text="x" * 5000)
    small = UserMessage(index=1, text="short")
    transcript = Transcript(id="t-cap", source="synthetic", events=[big, small])
    view = build_view(transcript, chars_budget(64000))
    capped, uncapped = view.events
    assert capped.truncated
    assert capped.full_chars == len(render_event(big))
    assert capped.text.endswith("...[truncated]")
    assert len(capped.text) == 2000 + len("...[truncated]")
    assert not uncapped.truncated
    assert uncapped.full_chars == len(uncapped.text)


def test_event_text_for_absent_index_returns_none() -> None:
    view = build_view(_uniform_transcript(), chars_budget(64000))
    assert view.event_text(999) is None


def test_empty_transcript_views_are_empty_under_both_policies() -> None:
    transcript = Transcript(id="t-empty", source="synthetic", events=[])
    for policy in ("head", "bracket"):
        view = build_view(transcript, chars_budget(300), policy)
        assert view.text == ""
        assert view.events == ()
        assert view.elided == ()
        assert view.transcript_id == "t-empty"


# --- RenderBudget.calibrated, estimate_tokens, preflight_fits ---


def test_calibrated_cpt_pinned_from_the_calibration_script() -> None:
    """Pinned by scripts/calibrate_render_budget.py from the committed sample
    records (results/dev-render-calibration/): qwen p05 3.0429 -> 3.0, gemma
    p05 2.8400 -> the 3.0 floor binds; the "" fallback is the min pin."""
    assert _CALIBRATED_CPT == {
        "agentnom-local": 3.0,
        "agentmon-local-gemma": 3.0,
        "": 3.0,
    }


def test_calibrated_budget_fields_and_max_chars() -> None:
    budget = RenderBudget.calibrated(16000, "agentnom-local")
    assert budget.max_tokens == 16000
    assert budget.chars_per_token == 3.0
    assert budget.margin == 0.95
    assert budget.reserved_tokens == 0
    assert budget.max_chars == int(16000 * 3.0 * 0.95)
    assert budget.max_chars < RenderBudget.legacy(16000).max_chars


def test_calibrated_budget_reserves_template_overhead() -> None:
    budget = RenderBudget.calibrated(16000, "agentmon-local-gemma", template_chars=3200)
    assert budget.reserved_tokens == math.ceil(3200 / 3.0)
    assert budget.max_chars == int((16000 - budget.reserved_tokens) * 3.0 * 0.95)


def test_calibrated_budget_falls_back_for_unknown_models() -> None:
    assert RenderBudget.calibrated(1000, "some-new-substrate").chars_per_token == 3.0


def test_estimate_tokens_rounds_up() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("x" * 64000, chars_per_token=4.0) == 16000
    assert estimate_tokens("x" * 5, chars_per_token=4.0) == 2
    assert estimate_tokens("x" * 3200) == math.ceil(3200 / _CALIBRATED_CPT[""])


def test_preflight_fits_boundary() -> None:
    prompt = "x" * 8000
    tokens = estimate_tokens(prompt, chars_per_token=4.0)
    assert preflight_fits(prompt, tokens + 2048, chars_per_token=4.0)
    assert not preflight_fits(prompt, tokens + 2047, chars_per_token=4.0)
    assert preflight_fits(prompt, tokens, output_reserve=0, chars_per_token=4.0)


def test_preflight_catches_the_gemma_overflow_shape() -> None:
    """A 64k-char render (~19.8k server tokens, DECISIONS 41) must fail the
    8192/16384 contexts and pass the 32768 re-serve — detectable pre-spend."""
    prompt = "x" * 64000
    assert not preflight_fits(prompt, 16384)
    assert preflight_fits(prompt, 32768)
