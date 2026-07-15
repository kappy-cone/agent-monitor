"""Export agentmon run artifacts into the Elasticsearch lens (design 10, slice 1).

Reads committed run dirs (summary.json + verdicts.jsonl), joins ground truth via the
library's own build_rows, and bulk-indexes one doc per DRAW into `agentmon-draws`.

ADDITIVITY: this lives outside src/agentmon/**. The library never imports Elasticsearch;
this script imports the library. `uv run pytest` is green with the stack down because the
test path cannot reach ES, not because we mocked it.

DISPOSABLE: ES is a derived projection. Drop the index whenever; this rebuilds it in
seconds from artifacts that remain the source of truth.

NO NEW DEPENDENCY: httpx is already a dep (pyproject.toml). _bulk is an NDJSON POST, so
the `elasticsearch` client buys nothing here. "Keep it boring" satisfied by adding nothing.

Targets the ALREADY-RUNNING elk-forensics cluster (7.17.18) at localhost:9200 by default.
It writes only `agentmon-*` indices and never touches `malware-investigation`.

Run:
  env -u UV_PYTHON uv run --no-sync python scripts/es/export.py            # dry plan
  env -u UV_PYTHON uv run --no-sync python scripts/es/export.py --execute
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
import yaml  # noqa: E402
from docs import DRAWS_INDEX, build_run_docs, bulk_ndjson  # noqa: E402
from timeprov import resolve  # noqa: E402

from agentmon.eval.split import load_labels, parse_provenance  # noqa: E402
from agentmon.schemas import CalibratedVerdict  # noqa: E402

LABELS = REPO / "datasets" / "synthetic" / "labels.jsonl"
MAPPING = REPO / "ops" / "lens" / "mappings" / "agentmon-draws.json"
SUBSTRATES = Path(__file__).resolve().parent / "substrates.yaml"
SEARCH_ROOTS = ("results", "out")


def discover_runs() -> list[Path]:
    """Every run dir with both a summary.json and a verdicts.jsonl."""
    out: list[Path] = []
    for root in SEARCH_ROOTS:
        for verdicts in sorted((REPO / root).rglob("verdicts.jsonl")):
            if (verdicts.parent / "summary.json").is_file():
                out.append(verdicts.parent)
    return out


def run_key(run_dir: Path) -> str:
    return str(run_dir.relative_to(REPO)).replace("/", "__")


def load_run(run_dir: Path) -> tuple[dict[str, Any], list[CalibratedVerdict]]:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    verdicts = [
        CalibratedVerdict.model_validate_json(line)
        for line in (run_dir / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return summary, verdicts


def ensure_index(client: httpx.Client, base: str) -> None:
    body = json.loads(MAPPING.read_text(encoding="utf-8"))
    r = client.head(f"{base}/{DRAWS_INDEX}")
    if r.status_code == 200:
        return
    r = client.put(f"{base}/{DRAWS_INDEX}", json=body)
    if r.status_code >= 300:
        raise RuntimeError(f"create index failed: {r.status_code} {r.text[:400]}")
    print(f"  created index {DRAWS_INDEX}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="actually index (default: dry plan)")
    ap.add_argument("--es", default="http://localhost:9200", help="Elasticsearch base URL")
    args = ap.parse_args()

    substrates = yaml.safe_load(SUBSTRATES.read_text(encoding="utf-8"))
    provenances = {p.transcript_id: p for p in map(parse_provenance, load_labels(LABELS))}
    export_time = datetime.datetime.now(datetime.UTC).isoformat()

    runs = discover_runs()
    all_docs: list[tuple[str, dict[str, Any]]] = []
    time_census: dict[str, int] = {}
    skipped: list[str] = []

    for run_dir in runs:
        summary, verdicts = load_run(run_dir)
        if not verdicts:
            continue
        key = run_key(run_dir)
        stamp = resolve(summary, export_time=export_time)
        time_census[stamp.provenance] = time_census.get(stamp.provenance, 0) + 1
        meta = {
            "run_key": key,
            "committed": run_dir.is_relative_to(REPO / "results"),
            "slice": summary.get("slice"),
        }
        try:
            docs = build_run_docs(
                verdicts,
                provenances,
                run_meta=meta,
                time_fields=stamp.as_fields(),
                substrates=substrates,
            )
        except KeyError as exc:
            # Fail loud per run rather than silently dropping a substrate/label.
            skipped.append(f"{key}: {exc}")
            continue
        all_docs.extend(docs)

    print(f"PLAN: {len(runs)} run dirs -> {len(all_docs)} draw docs into {DRAWS_INDEX}")
    print(f"  time provenance census: {time_census}")
    if skipped:
        print(f"  SKIPPED {len(skipped)} runs (fail-closed):")
        for s in skipped[:5]:
            print(f"    - {s}")
    untrusted = time_census.get("none", 0)
    if untrusted:
        print(f"  WARNING: {untrusted} runs have NO declared time -> trusted=false")

    if not args.execute:
        print("\n(dry plan — pass --execute to index)")
        return

    with httpx.Client(timeout=60.0) as client:
        info = client.get(args.es)
        if info.status_code != 200:
            raise SystemExit(f"no Elasticsearch at {args.es}: {info.status_code}")
        version = info.json()["version"]["number"]
        print(f"\nelasticsearch {version} at {args.es}")
        ensure_index(client, args.es)

        CHUNK = 500
        indexed = errors = 0
        for i in range(0, len(all_docs), CHUNK):
            payload = bulk_ndjson(all_docs[i : i + CHUNK])
            r = client.post(
                f"{args.es}/_bulk",
                content=payload.encode(),
                headers={"Content-Type": "application/x-ndjson"},
            )
            if r.status_code >= 300:
                raise RuntimeError(f"bulk failed: {r.status_code} {r.text[:400]}")
            body = r.json()
            for item in body.get("items", []):
                if item.get("index", {}).get("error"):
                    errors += 1
                    if errors <= 3:
                        print(f"  ERROR: {item['index']['error']}")
                else:
                    indexed += 1
        client.post(f"{args.es}/{DRAWS_INDEX}/_refresh")
        print(f"indexed {indexed} docs, {errors} errors")


if __name__ == "__main__":
    main()
