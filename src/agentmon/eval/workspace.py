"""Repo-anchored path constants: dataset, cache, configs, run outputs.

The workspace layout in one place, so drivers stop importing dataset paths
from each other (the old ``scripts -> scripts`` import). Paths only — slice
resolution (and its seal refusals) deliberately stays in the dev adapter.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

LABELS_PATH = REPO / "datasets" / "synthetic" / "labels.jsonl"
SPLIT_PATH = REPO / "datasets" / "synthetic" / "split.json"
TRANSCRIPTS_DIR = REPO / "datasets" / "synthetic" / "transcripts"
PROMPTS_DIR = REPO / "src" / "agentmon" / "prompts"
CACHE_DIR = REPO / ".agentmon_cache"
RESULTS_DIR = REPO / "results"

OUT_PHASE3 = REPO / "out" / "phase3"
OUT_PHASE4 = REPO / "out" / "phase4"
PHASE4_LEDGER = OUT_PHASE4 / "requests.jsonl"

BANDS_DIR = REPO / "configs" / "bands"
FREEZE_MANIFEST = REPO / "configs" / "freeze" / "gate2.yaml"
