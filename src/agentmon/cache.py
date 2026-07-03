"""The one on-disk LLM response cache contract: read-or-call-and-write.

Every cached model call in the codebase goes through :func:`complete_cached`:
look up the fully-resolved entry path, treat any missing or corrupt file as a
miss, call the client on a miss, and write the raw
:class:`~agentmon.llm.client.LLMResponse` back so re-runs cost nothing.
Verdicts are always rebuilt by re-parsing the (cached or fresh) response, so
the cache stores responses, never interpretations.

Key composition lives with the callers, not here — the caller owns key
derivation and hands over a resolved path. The two key schemes are
:func:`agentmon.calibration.sample_cache_key` (scoring draws: prompt hash +
transcript hash + effective model + temperature + sample index + attempt) and
``agentmon.verification._llm_cache_key`` (verification stages: stage ``kind`` +
prompt hash + model + temperature, one namespace per LLM stage).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from agentmon.llm.client import LLMClient, LLMResponse


def load_cached_response(path: Path) -> LLMResponse | None:
    """Load a cached response, treating any missing or corrupt file as a miss."""
    try:
        return LLMResponse.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return None


def complete_cached(
    client: LLMClient,
    prompt: str,
    *,
    model: str,
    temperature: float,
    cache_path: Path | None,
) -> LLMResponse:
    """Return a cached response for ``prompt`` or call the client and cache it.

    ``cache_path`` is the fully-resolved entry path (the caller owns key
    derivation); ``None`` disables caching. The shared read-or-call-and-write
    helper behind both the sampling path and the verification stages.
    """
    response = load_cached_response(cache_path) if cache_path else None
    if response is None:
        response = client.complete(prompt, model=model, temperature=temperature)
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(response.model_dump_json(), encoding="utf-8")
    return response
