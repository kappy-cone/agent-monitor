"""Provision the agentmon Kibana objects (design 10, slice 1) — as code, not clicks.

Index Management lists raw ES indices; Discover/Visualize need Kibana *saved objects*.
This creates them idempotently via the API so the lens is reproducible and reviewable
rather than a click-built artifact nobody can rebuild.

Targets the already-running elk-forensics Kibana (7.17.18). NOTE: 7.17 calls these
"index patterns"; "data views" is the 8.0+ rename. It creates only `agentmon-*`
objects and never touches the malware-investigation pattern.

THE GRAIN FOOTGUN this guards: agentmon-draws holds one doc per DRAW (k=3 per
verdict). Counting FPs across all docs returns 3x. The saved search pins
agentmon.draw.sample_index:0 — the decision grain, where each doc carries every
calibrated field exactly once.

THE TIME TRAP: within a run, time is DEGENERATE (all draws share one instant) and the
archive is historical (2026-06-12 .. 2026-07-07). Discover defaults to "Last 15
minutes" and will look EMPTY. Set the range wide. The date histogram is meaningful
only ACROSS runs, where a run legitimately reads as a burst — never within one.

Run: env -u UV_PYTHON uv run --no-sync python ops/lens/bootstrap_kibana.py
"""

from __future__ import annotations

import argparse
import json

import httpx

KIBANA = "http://localhost:5601"
INDEX = "agentmon-draws"
PATTERN_ID = "agentmon-draws-pattern"
H = {"kbn-xsrf": "true", "Content-Type": "application/json"}


def put_object(client: httpx.Client, base: str, otype: str, oid: str, attrs: dict, refs=None):
    """Idempotent create-or-overwrite of one saved object."""
    body: dict = {"attributes": attrs}
    if refs:
        body["references"] = refs
    r = client.post(f"{base}/api/saved_objects/{otype}/{oid}?overwrite=true", headers=H, json=body)
    if r.status_code >= 300:
        raise RuntimeError(f"{otype}/{oid} failed: {r.status_code} {r.text[:300]}")
    print(f"  ok  {otype}/{oid}")
    return r.json()


def search_source(query: str, filters: list | None = None) -> str:
    """For objects that REFERENCE an index pattern (search, visualization).

    ``indexRefName`` must have a matching entry in the object's ``references``, or
    Kibana refuses to load it with "Could not find reference for ...index".
    """
    return json.dumps(
        {
            "query": {"query": query, "language": "kuery"},
            "filter": filters or [],
            "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
        }
    )


def fetch_fields(client: httpx.Client, base: str, pattern: str) -> list:
    """The field list Kibana 7.17 stores on the index-pattern object.

    Without this the object still saves (200) and then every visualization renders a
    bare "Error" — the failure is silent at write time and only visible in the UI.
    """
    meta = ["_source", "_id", "_index", "_score"]
    q = "&".join(f"meta_fields={m}" for m in meta)
    r = client.get(f"{base}/api/index_patterns/_fields_for_wildcard?pattern={pattern}&{q}")
    if r.status_code >= 300:
        raise RuntimeError(f"fields_for_wildcard failed: {r.status_code} {r.text[:200]}")
    return r.json()["fields"]


def dashboard_search_source() -> str:
    """For a DASHBOARD, which has no index of its own — its panels carry the refs.

    Emitting indexRefName here (with only panel_N refs present) is what broke the
    first bootstrap: the object saved fine and then failed to render.
    """
    return json.dumps({"query": {"query": "", "language": "kuery"}, "filter": []})


REF = [
    {
        "id": PATTERN_ID,
        "name": "kibanaSavedObjectMeta.searchSourceJSON.index",
        "type": "index-pattern",
    }
]


def viz(title: str, vis_state: dict, query: str = "") -> dict:
    return {
        "title": title,
        "visState": json.dumps(vis_state),
        "uiStateJSON": "{}",
        "description": "",
        "kibanaSavedObjectMeta": {"searchSourceJSON": search_source(query)},
    }


def terms_bar(field: str, size: int, split: str | None = None) -> dict:
    aggs: list = [
        {"id": "1", "enabled": True, "type": "count", "schema": "metric", "params": {}},
        {
            "id": "2",
            "enabled": True,
            "type": "terms",
            "schema": "segment",
            "params": {"field": field, "size": size, "order": "desc", "orderBy": "1"},
        },
    ]
    if split:
        aggs.append(
            {
                "id": "3",
                "enabled": True,
                "type": "terms",
                "schema": "group",
                "params": {"field": split, "size": 10, "order": "desc", "orderBy": "1"},
            }
        )
    return {
        "title": "",
        "type": "histogram",
        "aggs": aggs,
        "params": {
            "type": "histogram",
            "grid": {"categoryLines": False},
            "categoryAxes": [
                {
                    "id": "CategoryAxis-1",
                    "type": "category",
                    "position": "bottom",
                    "show": True,
                    "scale": {"type": "linear"},
                    "labels": {"show": True, "filter": True, "truncate": 100},
                    "title": {},
                }
            ],
            "valueAxes": [
                {
                    "id": "ValueAxis-1",
                    "name": "LeftAxis-1",
                    "type": "value",
                    "position": "left",
                    "show": True,
                    "scale": {"type": "linear", "mode": "normal"},
                    "labels": {"show": True, "rotate": 0, "filter": False, "truncate": 100},
                    "title": {"text": "Count"},
                }
            ],
            "seriesParams": [
                {
                    "show": True,
                    "type": "histogram",
                    "mode": "stacked",
                    "data": {"label": "Count", "id": "1"},
                    "valueAxis": "ValueAxis-1",
                    "drawLinesBetweenPoints": True,
                    "showCircles": True,
                }
            ],
            "addTooltip": True,
            "addLegend": True,
            "legendPosition": "right",
            "times": [],
            "addTimeMarker": False,
            "labels": {"show": False},
            # `color` is REQUIRED in 7.17 even when show=False. Omitting it renders the
            # panel as a bare "Error" and the editor says "thresholdline requires an
            # argument" — the saved object writes 200 either way.
            "thresholdLine": {
                "show": False,
                "value": 10,
                "width": 1,
                "style": "full",
                "color": "#E7664C",
            },
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kibana", default=KIBANA)
    args = ap.parse_args()

    with httpx.Client(timeout=60.0) as client:
        st = client.get(f"{args.kibana}/api/status")
        if st.status_code != 200:
            raise SystemExit(f"no Kibana at {args.kibana}: {st.status_code}")
        print(f"kibana {st.json()['version']['number']} at {args.kibana}")

        # 1. The index pattern. @timestamp IS the time field: a run is a temporal
        #    event and the cross-run histogram is legitimate. See the TIME TRAP above.
        #
        #    7.17 needs the field list written INTO the saved object. There is no
        #    fields/refresh endpoint here (that is the 8.x data-views API and 404s),
        #    and an index pattern with no `fields` saves happily and then renders
        #    every visualization as a bare "Error".
        fields = fetch_fields(client, args.kibana, INDEX)
        print(f"  fields resolved: {len(fields)}")
        put_object(
            client,
            args.kibana,
            "index-pattern",
            PATTERN_ID,
            {
                "title": INDEX,
                "timeFieldName": "@timestamp",
                "fields": json.dumps(fields),
            },
        )

        # 2. Saved search at the DECISION GRAIN. This is the footgun guard: without
        #    sample_index:0 every count is 3x (k=3 draws per verdict).
        put_object(
            client,
            args.kibana,
            "search",
            "agentmon-decisions",
            {
                "title": "agentmon — decisions (sample_index:0, the decision grain)",
                "description": (
                    "One row per CalibratedVerdict. Counting FPs WITHOUT this filter "
                    "returns 3x (k=3 draws per verdict)."
                ),
                "columns": [
                    "agentmon.transcript.id",
                    "rule.name",
                    "agentmon.substrate.family",
                    "agentmon.eval.tag",
                    "agentmon.eval.label",
                    "agentmon.verdict.fraction_flagged",
                ],
                "sort": [["@timestamp", "desc"]],
                "kibanaSavedObjectMeta": {
                    "searchSourceJSON": search_source("agentmon.draw.sample_index:0")
                },
            },
            REF,
        )

        # 3. Visualizations. Each is a faceting question ES answers well — and
        #    deliberately NOT a headline metric: AUROC/recall@FPR need a sorted global
        #    sweep, are not ES aggs, and stay in `agentmon report`. Kibana never
        #    recomputes a headline metric.
        vizzes = [
            (
                "agentmon-outcome-by-substrate",
                "agentmon — outcome by substrate (decision grain)",
                terms_bar("agentmon.eval.tag", 5, split="agentmon.substrate.family"),
                "agentmon.draw.sample_index:0",
            ),
            (
                "agentmon-flags-by-monitor",
                "agentmon — alerts by monitor (rule.name)",
                terms_bar("rule.name", 12),
                "event.kind:alert and agentmon.draw.sample_index:0",
            ),
            (
                "agentmon-categories",
                "agentmon — emitted failure categories",
                terms_bar("agentmon.draw.categories", 10),
                "event.kind:alert",
            ),
        ]
        panels = []
        for i, (vid, title, state, q) in enumerate(vizzes):
            put_object(client, args.kibana, "visualization", vid, viz(title, state, q), REF)
            panels.append(
                {
                    "version": "7.17.18",
                    "type": "visualization",
                    "gridData": {
                        "x": (i % 2) * 24,
                        "y": (i // 2) * 15,
                        "w": 24,
                        "h": 15,
                        "i": str(i),
                    },
                    "panelIndex": str(i),
                    "embeddableConfig": {},
                    "panelRefName": f"panel_{i}",
                }
            )

        # 4. The dashboard.
        put_object(
            client,
            args.kibana,
            "dashboard",
            "agentmon-overview",
            {
                "title": "agentmon — monitor detections overview",
                "description": (
                    "Derived, disposable lens over committed artifacts. Headline metrics "
                    "(AUROC, recall@FPR) are NOT here by design — they need a sorted global "
                    "sweep, are not ES aggregations, and live in `agentmon report`. "
                    "Time is degenerate WITHIN a run; the histogram reads across runs only."
                ),
                "panelsJSON": json.dumps(panels),
                "optionsJSON": json.dumps({"hidePanelTitles": False, "useMargins": True}),
                "version": 1,
                "timeRestore": True,
                "timeFrom": "2026-06-01T00:00:00.000Z",  # the archive is historical:
                "timeTo": "now",  # "Last 15 minutes" would look EMPTY
                "kibanaSavedObjectMeta": {"searchSourceJSON": dashboard_search_source()},
            },
            [
                {"id": v[0], "name": f"panel_{i}", "type": "visualization"}
                for i, v in enumerate(vizzes)
            ],
        )

    print("\nOpen:")
    print(f"  Dashboard: {args.kibana}/app/dashboards#/view/agentmon-overview")
    print(f"  Discover:  {args.kibana}/app/discover#/view/agentmon-decisions")
    print("\nTIME RANGE: the archive is 2026-06-12..2026-07-07. The dashboard pins")
    print("2026-06-01 -> now. In Discover set the range wide or it looks empty.")


if __name__ == "__main__":
    main()
