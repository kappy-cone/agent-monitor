"""Build the self-contained HTML run-results viewer for one eval run.

A thin adapter over :func:`agentmon.eval.report.write_html_report` (the same
pattern as ``dev_eval.py``): point it at a run directory written by
``scripts/dev_eval.py`` (``summary.json`` + ``verdicts.jsonl``) and it writes
``<run_dir>/report.html`` — a single offline file, openable from ``file://``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentmon.eval.report import write_html_report  # noqa: E402
from agentmon.eval.workspace import LABELS_PATH  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "run_dir", type=Path, help="run directory containing summary.json and verdicts.jsonl"
    )
    parser.add_argument(
        "--labels", type=Path, default=LABELS_PATH, help="ground-truth labels JSONL"
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="output HTML path (default: <run_dir>/report.html)"
    )
    args = parser.parse_args()
    out_path = write_html_report(args.run_dir, args.labels, args.out)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
