"""Discovery of monitor prompt files on disk."""

from __future__ import annotations

from pathlib import Path

from agentmon.monitors.base import Monitor

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_monitors(prompts_dir: Path | None = None) -> dict[str, Monitor]:
    """Load every ``*.md`` prompt file in ``prompts_dir``, keyed by monitor id."""
    directory = prompts_dir if prompts_dir is not None else DEFAULT_PROMPTS_DIR
    monitors: dict[str, Monitor] = {}
    for path in sorted(directory.glob("*.md")):
        monitor = Monitor.from_file(path)
        monitor_id = monitor.config.id
        if monitor_id in monitors:
            raise ValueError(f"duplicate monitor id {monitor_id!r} in {path}")
        monitors[monitor_id] = monitor
    return monitors


def get_monitor(monitor_id: str, prompts_dir: Path | None = None) -> Monitor:
    """Return one monitor by id, or raise KeyError listing the available ids."""
    monitors = load_monitors(prompts_dir)
    if monitor_id not in monitors:
        available = ", ".join(sorted(monitors)) or "(none)"
        raise KeyError(f"unknown monitor {monitor_id!r}; available: {available}")
    return monitors[monitor_id]
