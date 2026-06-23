"""Monitor composition: a composite satisfies the Monitor interface.

Cascade (a cheap triage gates a deep member) and ensemble (combine member draws)
both live behind one seam — they implement ``evaluate_once`` the way a leaf
monitor does, so the calibrated scoring loop drives a composite exactly as it
drives a leaf (architecture-deepening candidate 3). This mirrors the
verification pipeline one layer up: a composite satisfies the draw-evaluator
interface the way a stage satisfies the pipeline interface.

A composite makes **no model call of its own**. It reads each member's
already-cached draws by passing the same per-draw :class:`DrawContext` down (the
sample index is preserved, never freshened), so it adds no cache key and
re-scores nothing. Member-level ``model_override`` is the only thing it changes
on the context, and only when set — the seam for cross-substrate ensembles.

Composites are opt-in: ``load_monitors`` discovers only leaf prompt files, so
nothing instantiates a composite unless a caller builds one explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agentmon.calibration import DrawContext, DrawResult
from agentmon.monitors.base import MonitorConfig
from agentmon.schemas import CompositeProvenance
from agentmon.verification import StageContext, VerificationPipeline, default_pipeline

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentmon.monitors.base import Monitor
    from agentmon.schemas import VerificationOutcome


@dataclass(frozen=True)
class Member:
    """A composite's member: a monitor, optionally pinned to a served model.

    ``model_override`` overrides the context's model for this member only, and
    only when set — the seam for cross-substrate ensembles, where the same
    monitor reads draws cached under two different served names. ``name``
    disambiguates those two in provenance and member-score maps.
    """

    monitor: Monitor
    model_override: str | None = None

    @property
    def name(self) -> str:
        if self.model_override is None:
            return self.monitor.config.id
        return f"{self.monitor.config.id}@{self.model_override}"

    def draw(self, ctx: DrawContext) -> DrawResult:
        """This member's draw for ``ctx``, with its model pin applied if set."""
        member_ctx = ctx if self.model_override is None else replace(ctx, model=self.model_override)
        return self.monitor.evaluate_once(member_ctx)


class CompositeMonitor:
    """Base for monitors that compose member draws behind the leaf seam.

    Exposes the ``config`` (id + model) and ``evaluate_once`` the calibrated
    scoring loop needs, so a composite is interchangeable with a leaf. The
    config model defaults to the first member's effective model — a standalone
    run with no runner override then reads members under a real served name —
    but a runner ``model_override`` or per-member pin supersedes it.
    """

    def __init__(self, monitor_id: str, members: Sequence[Member]) -> None:
        if not members:
            raise ValueError("a composite monitor needs at least one member")
        self.members: tuple[Member, ...] = tuple(members)
        model = members[0].model_override or members[0].monitor.config.model
        self.config = MonitorConfig(id=monitor_id, model=model)

    def evaluate_once(self, ctx: DrawContext) -> DrawResult:  # pragma: no cover - abstract
        raise NotImplementedError


def _select_driver(draws: list[tuple[Member, DrawResult]]) -> tuple[Member, DrawResult]:
    """The member whose draw an ensemble returns: highest-scoring flagged, else
    highest-scoring overall. Ties resolve to the earlier member, deterministically.
    """
    flagged = [pair for pair in draws if pair[1].verdict.categories]
    pool = flagged if flagged else draws
    return max(pool, key=lambda pair: pair[1].verdict.suspicion_score)


class EnsembleMonitor(CompositeMonitor):
    """Combine member draws: flag = OR, score = max, return the driver's draw.

    Every member scores the SAME context (each reads its own cached draws). The
    composite draw flags iff any member flagged — OR emerges because the driver
    is the highest-scoring *flagged* member when any flagged — and carries the
    maximum member suspicion score. Its reasoning, categories, and cited events
    are the driver's verbatim, so a downstream verification check still has real
    grounding to re-examine. Parse-hardening usage from every member is summed,
    and each non-driver member's draw usage is folded into the extras, so total
    cost counts every member call exactly once.

    Watch-out (failure mode #2 at the composition layer): the calibrated loop's
    verification pass re-checks only the single returned draw — the driver. If
    the driver is a loud false positive that verification refutes, a quieter
    member's true catch, which rode in only as the OR flag, is dropped with it.
    The ensemble cannot protect a quiet catch behind a loud one. Because each
    member's draw is cached under its own key, per-member verification (verify
    each, then OR the survivors) is recoverable as a live-box step; that is why
    the offline dev probe scores composites with verification OFF.
    """

    def evaluate_once(self, ctx: DrawContext) -> DrawResult:
        draws = [(member, member.draw(ctx)) for member in self.members]
        driver, driver_result = _select_driver(draws)
        max_score = max(result.verdict.suspicion_score for _, result in draws)
        member_scores = {member.name: result.verdict.suspicion_score for member, result in draws}
        verdict = driver_result.verdict.model_copy(
            update={
                "suspicion_score": max_score,
                # Defensive copies: never alias a member's (possibly reused) leaf lists.
                "categories": list(driver_result.verdict.categories),
                "evidence": list(driver_result.verdict.evidence),
                "provenance": CompositeProvenance(
                    composite=self.config.id,
                    driver=driver.name,
                    member_scores=member_scores,
                    escalated=False,
                ),
            }
        )
        # The driver's own draw usage rides on `verdict` (summed via samples);
        # fold every member's discarded-attempt extras plus the non-driver
        # members' draw usage in here, so the total counts each call once.
        extra_in = sum(result.extra_input_tokens for _, result in draws)
        extra_out = sum(result.extra_output_tokens for _, result in draws)
        for _, result in draws:
            if result is driver_result:
                continue
            extra_in += result.verdict.input_tokens
            extra_out += result.verdict.output_tokens
        return DrawResult(
            verdict=verdict,
            parse_retries=sum(result.parse_retries for _, result in draws),
            parse_repairs=sum(result.parse_repairs for _, result in draws),
            extra_input_tokens=extra_in,
            extra_output_tokens=extra_out,
        )


@dataclass(frozen=True)
class _MemberVerification:
    """One flagged member's verification outcome, for the survivor fold + audit."""

    name: str
    supported: bool
    outcome: VerificationOutcome


def _verify_then_or(
    draws: list[tuple[Member, DrawResult]],
    ctx: DrawContext,
    pipeline: VerificationPipeline,
) -> tuple[list[tuple[Member, DrawResult]], list[_MemberVerification], int, int]:
    """Verify each FLAGGED member's own draw, keyed at that member's served model.

    Returns ``(survivors, records, verif_in, verif_out)``. An unflagged member
    is never verified (no spend) and is not a survivor — mirroring
    ``run_calibrated``, which only verifies flagged samples. A flagged member
    whose verification refutes its flag (``combined.supported is False``) is
    dropped. The :class:`StageContext` model is the member's served model
    (``model_override`` or the context model), so the verification cache key
    namespaces under exactly the substrate the member's draw was read at — which
    is what makes a cross-substrate verified ensemble key correctly and replay
    from the per-substrate ``verify=True`` cache with zero completions.
    """
    survivors: list[tuple[Member, DrawResult]] = []
    records: list[_MemberVerification] = []
    verif_in = verif_out = 0
    for member, result in draws:
        if not result.verdict.categories:  # unflagged members are never verified
            continue
        member_model = member.model_override or ctx.model
        sctx = StageContext(
            transcript=ctx.transcript,
            verdict=result.verdict,
            client=ctx.client,
            cache_dir=ctx.cache_dir,
            model=member_model,
        )
        combined = pipeline.run(sctx).combined  # one call; reuse the result
        verif_in += combined.input_tokens
        verif_out += combined.output_tokens
        records.append(_MemberVerification(member.name, combined.supported, combined))
        if combined.supported:
            survivors.append((member, result))
    return survivors, records, verif_in, verif_out


class VerifiedEnsembleMonitor(EnsembleMonitor):
    """Per-member verify-then-OR — the verification-aware ensemble.

    Closes the :class:`EnsembleMonitor` watch-out. Each member draws as before,
    then every *flagged* member's own draw is re-checked by a
    :class:`~agentmon.verification.VerificationPipeline` (``default_pipeline()``
    by default, or an injected one), keyed at that member's served model. Members
    whose flag is refuted are dropped; the composite then ORs the *survivors*:
    flag iff a survivor flagged, score = the max over survivors, and the driver
    (reasoning/categories/cited events) is the highest-scoring survivor. A loud
    false positive that verification refutes no longer drags down a quieter
    member's true catch — the quiet survivor stands on its own.

    The verification is baked into the returned draw, so ``run_calibrated`` must
    not re-verify it. This monitor sets ``self_verifies = True``; the calibrated
    loop reads that marker and skips its flip-on-fail pass for this monitor
    (leaves and plain composites in the same batch still verify normally).
    Without the marker the loop would re-verify only the surviving driver, under
    the single composite-effective model (mis-keyed for cross-substrate members)
    and record it in ``verifications``, double-counting the cost — so the marker
    makes ``verify=True`` and ``verify=False`` both correct for this monitor.

    ``provenance.member_supported`` records each flagged member's uphold/refute,
    so a rescued quiet catch and a dropped loud false positive are both legible.
    """

    #: This monitor verifies each member internally, so run_calibrated skips its
    #: own flip-on-fail pass (which would re-verify a draw already verified).
    self_verifies = True

    def __init__(
        self,
        monitor_id: str,
        members: Sequence[Member],
        *,
        pipeline: VerificationPipeline | None = None,
    ) -> None:
        super().__init__(monitor_id, members)
        self._pipeline = pipeline

    def evaluate_once(self, ctx: DrawContext) -> DrawResult:
        pipeline = self._pipeline or default_pipeline()
        draws = [(member, member.draw(ctx)) for member in self.members]
        survivors, records, verif_in, verif_out = _verify_then_or(draws, ctx, pipeline)
        member_scores = {member.name: result.verdict.suspicion_score for member, result in draws}
        member_supported = {record.name: record.supported for record in records}

        if survivors:
            driver, driver_result = _select_driver(survivors)
            score = max(result.verdict.suspicion_score for _, result in survivors)
            # Defensive copies: never alias a member's (possibly reused) leaf lists.
            categories = list(driver_result.verdict.categories)
            evidence = list(driver_result.verdict.evidence)
        else:
            # Every flag refuted (or none flagged): return an honest unflagged
            # draw — the highest-scoring member *overall*, its own score, no
            # categories. Pick flag-blind with ``max`` (NOT ``_select_driver``,
            # which prefers flagged members — a refuted member keeps its leaf
            # categories, so it would wrongly win over a higher unflagged one).
            # Clear evidence too, so an unflagged draw surfaces no grounding for
            # a flag that was dropped.
            driver, driver_result = max(draws, key=lambda pair: pair[1].verdict.suspicion_score)
            score = driver_result.verdict.suspicion_score
            categories = []
            evidence = []

        verdict = driver_result.verdict.model_copy(
            update={
                "suspicion_score": score,
                "categories": categories,
                "evidence": evidence,
                "provenance": CompositeProvenance(
                    composite=self.config.id,
                    driver=driver.name,
                    member_scores=member_scores,
                    member_supported=member_supported,
                    escalated=False,
                ),
            }
        )
        # The driver's draw usage rides on `verdict`; fold in every member's
        # parse-hardening extras, the non-driver draws, and every member
        # verification call, so total cost counts each call exactly once.
        extra_in = sum(result.extra_input_tokens for _, result in draws) + verif_in
        extra_out = sum(result.extra_output_tokens for _, result in draws) + verif_out
        for _, result in draws:
            if result is driver_result:
                continue
            extra_in += result.verdict.input_tokens
            extra_out += result.verdict.output_tokens
        return DrawResult(
            verdict=verdict,
            parse_retries=sum(result.parse_retries for _, result in draws),
            parse_repairs=sum(result.parse_repairs for _, result in draws),
            extra_input_tokens=extra_in,
            extra_output_tokens=extra_out,
        )


class CascadeMonitor(CompositeMonitor):
    """A cheap triage gates a deep member: escalate only when triage flags.

    The triage member scores first. If it does not flag, the cascade
    short-circuits and returns the triage draw — the deep member's
    ``evaluate_once`` is never called, so the deep model call (and its cache
    read) is skipped. When triage flags, the deep member scores the same context
    and its draw is returned, with triage's call folded into the extras. Recall
    is bounded by triage: a failure triage misses can never reach the deep member.
    """

    def __init__(self, monitor_id: str, triage: Member, deep: Member) -> None:
        super().__init__(monitor_id, [triage, deep])
        self.triage = triage
        self.deep = deep

    def evaluate_once(self, ctx: DrawContext) -> DrawResult:
        triage_result = self.triage.draw(ctx)
        if not triage_result.verdict.categories:
            verdict = triage_result.verdict.model_copy(
                update={
                    "provenance": CompositeProvenance(
                        composite=self.config.id,
                        driver=self.triage.name,
                        member_scores={self.triage.name: triage_result.verdict.suspicion_score},
                        escalated=False,
                    )
                }
            )
            return replace(triage_result, verdict=verdict)

        deep_result = self.deep.draw(ctx)
        verdict = deep_result.verdict.model_copy(
            update={
                "provenance": CompositeProvenance(
                    composite=self.config.id,
                    driver=self.deep.name,
                    member_scores={
                        self.triage.name: triage_result.verdict.suspicion_score,
                        self.deep.name: deep_result.verdict.suspicion_score,
                    },
                    escalated=True,
                )
            }
        )
        return DrawResult(
            verdict=verdict,
            parse_retries=triage_result.parse_retries + deep_result.parse_retries,
            parse_repairs=triage_result.parse_repairs + deep_result.parse_repairs,
            extra_input_tokens=(
                triage_result.extra_input_tokens
                + deep_result.extra_input_tokens
                + triage_result.verdict.input_tokens
            ),
            extra_output_tokens=(
                triage_result.extra_output_tokens
                + deep_result.extra_output_tokens
                + triage_result.verdict.output_tokens
            ),
        )
