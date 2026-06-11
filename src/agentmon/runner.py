"""Run monitors over transcripts with an optional on-disk response cache.

The cache stores raw :class:`~agentmon.llm.client.LLMResponse` payloads keyed
by (prompt template, transcript content, model, temperature), so re-runs cost
nothing and experiments stay reproducible. Verdicts are always rebuilt by
re-parsing the (cached or fresh) response through the monitor.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from agentmon.llm.client import LLMClient, LLMResponse
from agentmon.monitors.base import Monitor
from agentmon.schemas import MonitorVerdict, Transcript

if TYPE_CHECKING:
    from collections.abc import Sequence

_TEMPERATURE = 0.0


def cache_key(monitor: Monitor, transcript: Transcript) -> str:
    """Deterministic cache key for one (monitor, transcript) pair.

    Keyed on the *built* prompt (template + rendered transcript, so config
    changes like ``max_transcript_tokens`` invalidate it too), the transcript
    content, the model, and the temperature.
    """
    payload = json.dumps(
        {
            "prompt": monitor.build_prompt(transcript),
            "transcript": transcript.model_dump_json(),
            "model": monitor.config.model,
            "temperature": _TEMPERATURE,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_cached_response(path: Path) -> LLMResponse | None:
    """Load a cached response, treating any missing or corrupt file as a miss."""
    try:
        return LLMResponse.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return None


def run_monitors(
    monitors: Sequence[Monitor],
    transcripts: Sequence[Transcript],
    client: LLMClient,
    cache_dir: Path | None = None,
) -> list[MonitorVerdict]:
    """Run every monitor over every transcript, returning one verdict per pair.

    Calls are sequential (monitors outer, transcripts inner) at temperature 0.
    With ``cache_dir`` set, responses are cached on disk and cache hits skip
    the client entirely; ``cache_dir=None`` disables caching.
    """
    verdicts: list[MonitorVerdict] = []
    for monitor in monitors:
        for transcript in transcripts:
            prompt = monitor.build_prompt(transcript)
            cache_path = cache_dir / f"{cache_key(monitor, transcript)}.json" if cache_dir else None
            response = _read_cached_response(cache_path) if cache_path else None
            if response is None:
                response = client.complete(
                    prompt, model=monitor.config.model, temperature=_TEMPERATURE
                )
                if cache_path is not None:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(response.model_dump_json(), encoding="utf-8")
            verdicts.append(monitor.verdict_from_response(transcript.id, response))
    return verdicts
