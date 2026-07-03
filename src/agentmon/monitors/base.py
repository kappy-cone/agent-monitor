"""Monitor abstraction: prompt files, prompt building, and verdict parsing.

A :class:`Monitor` is a prompt template plus a :class:`MonitorConfig`, loaded
from a markdown file with YAML frontmatter. It renders a transcript into a
numbered plain-text view (via :mod:`agentmon.rendering`), asks an LLM for a
verdict, and defensively parses the response into a
:class:`~agentmon.schemas.MonitorVerdict`.

``render_event`` and ``render_transcript`` moved to :mod:`agentmon.rendering`
and are re-exported here so existing importers keep working unchanged.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

if TYPE_CHECKING:
    from agentmon.calibration import DrawContext, DrawResult

from agentmon.llm.client import LLMClient, LLMResponse
from agentmon.rendering import MonitorView, OverflowPolicy, RenderBudget, build_view
from agentmon.rendering import render_event as render_event  # redundant alias: re-export
from agentmon.rendering import render_transcript as render_transcript  # redundant alias: re-export
from agentmon.schemas import (
    Evidence,
    FailureCategory,
    MonitorVerdict,
    Transcript,
)


class MonitorConfig(BaseModel):
    """Monitor settings parsed from a prompt file's YAML frontmatter.

    ``chars_per_token`` and ``overflow_policy`` are additive fields with legacy
    defaults: the frozen prompt files carry neither, so their parsed config and
    every sample cache key are unchanged. The knobs are usable only by future
    dev-round monitors.
    """

    id: str
    description: str = ""
    categories: list[FailureCategory] = Field(default_factory=list)
    model: str
    max_transcript_tokens: int = Field(default=16000, gt=0)
    chars_per_token: float = Field(default=4.0, gt=0)
    overflow_policy: OverflowPolicy = "head"

    @model_validator(mode="after")
    def _warn_on_self_inflicted_truncation(self) -> MonitorConfig:
        """A tighter chars-per-token margin plus head truncation drops late evidence."""
        if self.chars_per_token < 4.0 and self.overflow_policy == "head":
            warnings.warn(
                f"monitor {self.id!r}: chars_per_token={self.chars_per_token} shrinks the "
                "render budget while overflow_policy='head' drops whole events from the "
                "tail — late evidence can be silently truncated; use "
                "overflow_policy='bracket'",
                stacklevel=2,
            )
        return self


def _parse_json_object(text: str) -> dict[str, Any]:
    """Extract the verdict JSON object from ``text``.

    Decodes a candidate object at every ``{`` in the response, tolerating
    prose (and stray braces) before or after the payload, markdown fences,
    and preamble objects. The last object carrying ``suspicion_score`` wins;
    otherwise the first object found.
    """
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    pos = text.find("{")
    while pos != -1:
        try:
            obj, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            pos = text.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            candidates.append(obj)
        pos = text.find("{", end)
    scored = [c for c in candidates if "suspicion_score" in c]
    if scored:
        return scored[-1]
    if candidates:
        return candidates[0]
    raise ValueError("no JSON object found in response")


def _clamp_score(value: Any) -> int:
    """Round a numeric (or numeric-string) score and clamp it to [0, 100]."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise ValueError(f"suspicion_score must be a number, got {value!r}") from None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"suspicion_score must be a number, got {value!r}")
    return max(0, min(100, round(value)))


def _parse_categories(value: Any) -> list[FailureCategory]:
    """Keep valid FailureCategory values; silently drop everything else."""
    if not isinstance(value, list):
        return []
    categories: list[FailureCategory] = []
    for item in value:
        try:
            categories.append(FailureCategory(item))
        except (ValueError, TypeError):
            continue
    return categories


def _parse_evidence(value: Any) -> list[Evidence]:
    """Keep evidence entries that validate; silently drop the rest."""
    if not isinstance(value, list):
        return []
    evidence: list[Evidence] = []
    for item in value:
        try:
            evidence.append(Evidence.model_validate(item))
        except ValidationError:
            continue
    return evidence


class Monitor:
    """A single LLM judge: a prompt template plus its config."""

    def __init__(self, config: MonitorConfig, template: str) -> None:
        self.config = config
        self.template = template

    @classmethod
    def from_file(cls, path: Path) -> Monitor:
        """Load a monitor from a markdown file with YAML frontmatter.

        The file must start with a ``---`` line, contain frontmatter mapping
        to :class:`MonitorConfig` fields, then a closing ``---`` line followed
        by the markdown prompt body.
        """
        text = path.read_text(encoding="utf-8")
        lines = text.split("\n")
        if not lines or lines[0].strip() != "---":
            raise ValueError(f"{path}: expected YAML frontmatter opening '---' on line 1")
        end = None
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end = i
                break
        if end is None:
            raise ValueError(f"{path}: missing closing '---' for YAML frontmatter")
        try:
            data = yaml.safe_load("\n".join(lines[1:end]))
        except yaml.YAMLError as exc:
            raise ValueError(f"{path}: invalid YAML frontmatter: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{path}: frontmatter must be a YAML mapping")
        try:
            config = MonitorConfig.model_validate(data)
        except ValidationError as exc:
            raise ValueError(f"{path}: invalid monitor config: {exc}") from exc
        return cls(config=config, template="\n".join(lines[end + 1 :]))

    def view(self, transcript: Transcript) -> MonitorView:
        """The exact transcript view this monitor's prompt embeds.

        Grounding must check against this view — the bytes the monitor saw —
        never an uncapped re-render.
        """
        budget = RenderBudget(
            max_tokens=self.config.max_transcript_tokens,
            chars_per_token=self.config.chars_per_token,
        )
        return build_view(transcript, budget, self.config.overflow_policy)

    def build_prompt(self, transcript: Transcript) -> str:
        """Substitute the monitor's transcript view into the prompt template."""
        # str.replace, not str.format: prompt bodies contain literal JSON braces.
        return self.template.replace("{{transcript}}", self.view(transcript).text)

    def verdict_from_response(self, transcript_id: str, response: LLMResponse) -> MonitorVerdict:
        """Defensively parse a model response into a verdict.

        Any parse failure yields a score-0 verdict with ``parse_error`` set;
        the raw response text and usage metadata are always preserved.
        """
        meta: dict[str, Any] = {
            "monitor_id": self.config.id,
            "transcript_id": transcript_id,
            "model": response.model,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "latency_ms": response.latency_ms,
            "raw_response": response.text,
        }
        try:
            data = _parse_json_object(response.text)
            score = _clamp_score(data.get("suspicion_score", 0))
            categories = _parse_categories(data.get("categories"))
            evidence = _parse_evidence(data.get("evidence"))
            reasoning = data.get("reasoning", "")
            if not isinstance(reasoning, str):
                reasoning = str(reasoning)
        except Exception as exc:
            return MonitorVerdict(suspicion_score=0, parse_error=str(exc), **meta)
        return MonitorVerdict(
            suspicion_score=score,
            categories=categories,
            evidence=evidence,
            reasoning=reasoning,
            **meta,
        )

    def run(self, transcript: Transcript, client: LLMClient) -> MonitorVerdict:
        """Build the prompt, query the client, and parse the verdict."""
        prompt = self.build_prompt(transcript)
        response = client.complete(prompt, model=self.config.model, temperature=0.0)
        return self.verdict_from_response(transcript.id, response)

    def evaluate_once(self, ctx: DrawContext) -> DrawResult:
        """Produce one calibrated scoring draw — the leaf of the composition seam.

        A leaf monitor and a composite both expose ``evaluate_once``, so the
        calibrated scoring loop drives either through the same call. The leaf
        runs one parse-hardened, cached completion; a composite combines member
        draws. The delegate is imported lazily because the parse-hardening logic
        lives in ``agentmon.calibration``, which sits above this module.
        """
        from agentmon.calibration import evaluate_leaf_draw

        return evaluate_leaf_draw(self, ctx)
