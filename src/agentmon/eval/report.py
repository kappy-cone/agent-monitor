"""Render eval outputs to disk.

Two independent renderers live here:

- :func:`write_report` — an :class:`EvalReport` to ``results.json`` + ``report.md``.
- :func:`write_html_report` — one calibrated run directory (``summary.json`` +
  ``verdicts.jsonl``) joined against ground-truth labels, rendered as a single
  self-contained HTML dashboard for error analysis. No server, no network, no
  templating dep: the page is string-built here, with all CSS/JS inlined and
  the row data embedded as a JSON blob a small vanilla script renders.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from agentmon.eval.split import load_labels, parse_provenance
from agentmon.schemas import (
    CalibratedVerdict,
    EvalReport,
    FailureCategory,
    MonitorEvalResult,
)

_INTRO = (
    "Recall figures use a false-alarm-budget framing: pick the score threshold that "
    "flags at most the budgeted fraction of benign transcripts (e.g. 1% FPR), then "
    "report the fraction of true failures caught at that threshold. This mirrors "
    "deployment, where the capacity to review false alarms is the binding constraint."
)


def _fmt(value: float | None) -> str:
    """Format a float to 3 decimals, with an em-dash for None."""
    return "—" if value is None else f"{value:.3f}"


def _monitor_section(result: MonitorEvalResult) -> str:
    tm = result.threshold_metrics
    rows = [
        (
            "Transcripts (n / positive / benign)",
            f"{result.n_transcripts} / {result.n_positive} / {result.n_benign}",
        ),
        ("Verdict parse failures (scored 0)", str(result.n_parse_failures)),
        ("AUROC", _fmt(result.auroc)),
        ("Recall @ 1% FPR", _fmt(result.recall_at_1pct_fpr)),
        ("Recall @ 5% FPR", _fmt(result.recall_at_5pct_fpr)),
        (f"Precision @ threshold {tm.threshold}", _fmt(tm.precision)),
        (f"Recall @ threshold {tm.threshold}", _fmt(tm.recall)),
        (f"F1 @ threshold {tm.threshold}", _fmt(tm.f1)),
        ("Mean input tokens", _fmt(result.mean_input_tokens)),
        ("Mean output tokens", _fmt(result.mean_output_tokens)),
        ("Mean latency (ms)", _fmt(result.mean_latency_ms)),
        ("Mean cost (USD)", _fmt(result.mean_cost_usd)),
    ]
    lines = [f"## {result.monitor_id}", "", "| Metric | Value |", "| --- | --- |"]
    lines += [f"| {name} | {value} |" for name, value in rows]
    return "\n".join(lines)


def write_report(report: EvalReport, out_dir: Path) -> tuple[Path, Path]:
    """Write results.json and report.md into out_dir; return (json_path, md_path)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "results.json"
    md_path = out_dir / "report.md"
    json_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    sections = [_monitor_section(result) for result in report.results]
    md = "\n\n".join(["# Monitor evaluation report", _INTRO, *sections]) + "\n"
    md_path.write_text(md, encoding="utf-8")
    return json_path, md_path


# --- HTML run-results viewer -------------------------------------------------

_MODE_ORDER = [category.value for category in FailureCategory]
_SPECIALIST_IDS = frozenset(_MODE_ORDER)


def confusion_tag(is_failure: bool, flagged: bool) -> str:
    """The four-way verdict-vs-label outcome for one (monitor, transcript)."""
    if is_failure:
        return "caught" if flagged else "missed"
    return "fp" if flagged else "tn"


def _sample_payload(verdict: CalibratedVerdict) -> list[dict[str, Any]]:
    """Per-sample drill-down detail: score, categories, evidence, verification.

    ``raw_response`` is deliberately excluded — it duplicates the parsed fields
    and would double the page weight for no analysis value.
    """
    payload: list[dict[str, Any]] = []
    for i, sample in enumerate(verdict.samples):
        verification = verdict.verifications[i] if i < len(verdict.verifications) else None
        payload.append(
            {
                "suspicion_score": sample.suspicion_score,
                "flagged": verdict.sample_flagged[i],
                "categories": [category.value for category in sample.categories],
                "evidence": [
                    {"event_index": ev.event_index, "quote": ev.quote} for ev in sample.evidence
                ],
                "reasoning": sample.reasoning,
                "parse_error": sample.parse_error,
                "verification": None
                if verification is None
                else {
                    "supported": verification.supported,
                    "quote_match": verification.quote_match,
                    "reasoning": verification.reasoning,
                },
            }
        )
    return payload


def build_rows(
    verdicts: list[CalibratedVerdict], provenances: dict[str, Any]
) -> list[dict[str, Any]]:
    """The verdict x ground-truth join: one drill-down row per CalibratedVerdict.

    "Flagged" is ``fraction_flagged > 0`` — the post-verification,
    decision-bearing quantity — so a flag that verification flipped never
    counts as a false positive here.
    """
    rows: list[dict[str, Any]] = []
    for verdict in sorted(verdicts, key=lambda v: (v.monitor_id, v.transcript_id)):
        prov = provenances.get(verdict.transcript_id)
        if prov is None:
            raise ValueError(
                f"verdict for {verdict.transcript_id!r} ({verdict.monitor_id}) has no "
                "ground-truth label in the labels file — cannot join"
            )
        flagged = verdict.fraction_flagged > 0
        flag_categories = sorted(
            {
                category.value
                for sample, sample_flagged in zip(
                    verdict.samples, verdict.sample_flagged, strict=True
                )
                if sample_flagged
                for category in sample.categories
            }
        )
        ran = [v for v in verdict.verifications if v is not None]
        flips = [v for v in ran if not v.supported]
        specialist = verdict.monitor_id in _SPECIALIST_IDS
        rows.append(
            {
                "monitor": verdict.monitor_id,
                "transcript": verdict.transcript_id,
                "label": prov.label,
                "stratum": prov.stratum,
                "fail_idx": list(prov.failure_event_indices),
                "flagged": flagged,
                "fraction_flagged": verdict.fraction_flagged,
                "mean_suspicion": verdict.mean_suspicion,
                "calibrated_score": verdict.calibrated_score,
                "tag": confusion_tag(prov.is_failure, flagged),
                "off_mode": specialist and prov.is_failure and prov.label != verdict.monitor_id,
                "cats": flag_categories,
                "verified_n": len(ran),
                "flips": len(flips),
                "mech_flips": sum(1 for v in flips if v.quote_match is False),
                "samples": _sample_payload(verdict),
            }
        )
    return rows


# The verdict x ground-truth join is the single source of truth for what counts as a
# catch / FP. It is public so out-of-library consumers (scripts/es, design 10) project
# the SAME join instead of forking it and drifting. Private aliases kept for callers
# that predate the promotion.
_build_rows = build_rows
_confusion_tag = confusion_tag


def _monitor_payload(summary_monitors: dict[str, Any]) -> dict[str, Any]:
    """Headline KPI numbers per monitor, lifted from summary.json (never recomputed).

    ``own_missed_n`` for an all-modes monitor (the generalist) is the full
    missed count — every category is its own mode.
    """
    monitors: dict[str, Any] = {}
    for monitor_id, entry in summary_monitors.items():
        specialist = monitor_id in _SPECIALIST_IDS
        missed_key = "own_failures_missed" if specialist else "failures_missed"
        try:
            monitors[monitor_id] = {
                "n": entry["n"],
                "auroc_all": entry["auroc_all"],
                "auroc_own": entry["auroc_own"],
                "recall_at_5pct": entry["recall_at_5pct"],
                "recall_at_0fp": entry["recall_at_0fp"],
                "own_missed_n": len(entry[missed_key]),
                "benign_fp_n": len(entry["benign_flagged"]),
                "hn_fp_n": len(entry["hard_negatives_flagged"]),
                "flips": entry["verification_flips"],
                "mech_flips": entry["mechanical_flips"],
                "specialist": specialist,
            }
        except KeyError as exc:
            raise ValueError(
                f"summary.json monitors[{monitor_id!r}] lacks key {exc.args[0]!r} — not a "
                "calibrated-run summary (expected the CalibratedRowSummary shape)"
            ) from exc
    return monitors


def _json_for_script(data: dict[str, Any]) -> str:
    """Serialize the data blob safely for embedding inside a ``<script>`` element.

    ``</`` and ``<!--`` would end (or comment-state) the script element if they
    appeared verbatim in a quote or reasoning string; both rewrites below are
    valid JSON escapes, so the parsed value is unchanged.
    """
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return blob.replace("</", "<\\/").replace("<!--", "<\\u0021--")


def write_html_report(run_dir: Path, labels_path: Path, out_html: Path | None = None) -> Path:
    """Render one calibrated run as a self-contained HTML error-analysis dashboard.

    Reads ``<run_dir>/summary.json`` and ``<run_dir>/verdicts.jsonl``, joins
    every verdict to its ground-truth provenance from ``labels_path``, and
    writes a single offline HTML file (default ``<run_dir>/report.html``).
    Deterministic: identical inputs produce identical bytes.
    """
    summary_path = run_dir / "summary.json"
    verdicts_path = run_dir / "verdicts.jsonl"
    for path in (summary_path, verdicts_path, labels_path):
        if not path.is_file():
            raise FileNotFoundError(f"run-report input missing: {path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(summary.get("monitors"), dict):
        raise ValueError(
            f"{summary_path} has no 'monitors' block — this viewer reads calibrated run "
            "directories as written by scripts/dev_eval.py (gated/composite summaries "
            "have a different shape)"
        )
    verdicts = [
        CalibratedVerdict.model_validate_json(line)
        for line in verdicts_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    provenances = {
        prov.transcript_id: prov for prov in map(parse_provenance, load_labels(labels_path))
    }
    rows = build_rows(verdicts, provenances)

    seen_modes = {row["label"] for row in rows} | {c for row in rows for c in row["cats"]}
    data = {
        "meta": {
            "label": summary.get("label", run_dir.name),
            "slice": summary.get("slice"),
            "model": summary.get("model_override"),
            "k": summary.get("k"),
            "verify": summary.get("verify"),
            "mock": summary.get("mock", False),
            "local": summary.get("local", False),
            "n_transcripts": summary.get("n_transcripts"),
            "calls": summary.get("calls"),
            "cost_usd": summary.get("cost_usd"),
            "timestamp": summary.get("timestamp"),
            "total_flags": sum(1 for row in rows if row["flagged"]),
        },
        "monitors": _monitor_payload(summary["monitors"]),
        "modes": [mode for mode in _MODE_ORDER if mode in seen_modes],
        "rows": rows,
    }

    page = _html_page(data)
    out_path = out_html if out_html is not None else run_dir / "report.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    return out_path


def _header_meta_html(meta: dict[str, Any]) -> str:
    """The static run-meta strip under the title, rendered server-side."""
    cost = meta.get("cost_usd")
    verify = meta.get("verify")
    items = [
        ("slice", meta.get("slice")),
        ("model", meta.get("model")),
        ("k", meta.get("k")),
        # A summary without the key drops the chip — never fabricate "off".
        ("verify", None if verify is None else ("on" if verify else "off")),
        ("transcripts", meta.get("n_transcripts")),
        ("total flags", meta.get("total_flags")),
        ("calls", meta.get("calls")),
        ("cost", None if cost is None else f"${cost:.2f}"),
        ("timestamp", meta.get("timestamp")),
    ]
    parts = [
        f"<span>{html.escape(str(name))} <b>{html.escape(str(value))}</b></span>"
        for name, value in items
        if value is not None
    ]
    return '<div class="meta">' + "".join(parts) + "</div>"


_HTML_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@</title>
<style>
:root{
  --bg:#f4f4f2; --surface:#ffffff; --line:#e3e1dc; --line2:#eeece8;
  --ink:#20201d; --muted:#71706a; --faint:#a3a29b;
  --catch:#2f7d4f; --catch-bg:#e7f2ea;
  --fp:#b45309; --fp-bg:#f9efe2;
  --miss:#b3403a; --miss-bg:#f8eae9;
  --tn:#6d6c66; --tn-bg:#efeeeb;
  --focus:#4c6ef5;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
}
*{box-sizing:border-box}
body{
  margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
.wrap{max-width:1180px;margin:0 auto;padding:0 24px}
.masthead{background:var(--surface);border-bottom:1px solid var(--line);padding:18px 0 14px}
.eyebrow{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
h1{font-size:22px;font-weight:650;margin:2px 0 8px;text-wrap:balance}
.titlerow{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.badge{
  font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;
  border:1px solid var(--line);border-radius:4px;padding:1px 6px;color:var(--muted);
}
.meta{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:12.5px;color:var(--muted)}
.meta b{color:var(--ink);font-weight:600;font-family:var(--mono);font-size:12px}
.mono{font-family:var(--mono);font-size:12.5px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0 14px}
.chip{
  display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);
  background:var(--surface);border-radius:999px;padding:5px 12px;font-size:13px;
  font-family:var(--mono);cursor:pointer;color:var(--ink);
}
.chip:hover{border-color:var(--faint)}
.chip.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.chip .n{
  font-size:10.5px;font-weight:700;border-radius:999px;padding:0 6px;
  background:var(--tn-bg);color:var(--tn);font-variant-numeric:tabular-nums;
}
.chip .n.bad{background:var(--miss-bg);color:var(--miss)}
.chip.on .n{background:rgba(255,255,255,.22);color:#fff}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;
  margin:0 0 16px}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:10px;
  padding:13px 15px 12px}
.kpi .v{font-size:26px;font-weight:650;font-family:var(--mono);
  font-variant-numeric:tabular-nums;line-height:1.15}
.kpi .l{font-size:12.5px;font-weight:600;margin-top:3px}
.kpi .s{font-size:11.5px;color:var(--muted);margin-top:1px}
.kpi.bad .v{color:var(--miss)}
.kpi.warn .v{color:var(--fp)}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;
  padding:16px 18px;margin-bottom:16px}
.panel h2{font-size:15px;font-weight:650;margin:0}
.panel .sub{font-size:12px;color:var(--muted);margin:2px 0 0}
.panel-head{margin-bottom:12px}
.barrow{
  display:grid;grid-template-columns:150px 1fr 140px;gap:12px;align-items:center;
  width:100%;border:0;background:none;padding:6px;border-radius:6px;cursor:pointer;
  text-align:left;font:inherit;color:inherit;
}
.barrow:hover{background:var(--bg)}
.barrow.on{background:var(--line2);box-shadow:inset 0 0 0 1.5px var(--ink)}
.barrow .bl{font-family:var(--mono);font-size:12.5px}
.track{display:flex;height:16px;border-radius:4px;overflow:hidden;background:var(--bg)}
.seg-c{background:var(--catch)}
.seg-f{background:var(--fp)}
.barrow .bn{font-family:var(--mono);font-size:12px;font-variant-numeric:tabular-nums;
  text-align:right;color:var(--muted);white-space:nowrap}
.barrow .bn b{font-weight:650}
.bn .c{color:var(--catch)} .bn .f{color:var(--fp)}
.legend{display:flex;gap:18px;font-size:12px;color:var(--muted);margin-top:10px;
  flex-wrap:wrap;align-items:center}
.sw{width:10px;height:10px;border-radius:2px;display:inline-block;margin-right:5px;
  vertical-align:-1px}
.toolbar{display:flex;gap:8px;align-items:center;margin:0 0 10px;flex-wrap:wrap}
.fbtn{
  border:1px solid var(--line);background:var(--surface);border-radius:6px;
  padding:4px 10px;font-size:12.5px;cursor:pointer;font:inherit;color:inherit;
}
.fbtn:hover{border-color:var(--faint)}
.fbtn.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.fbtn .n{font-variant-numeric:tabular-nums;font-family:var(--mono);font-size:11.5px}
.modeflag{
  display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);
  border:1px dashed var(--line);border-radius:6px;padding:3px 9px;
  font-family:var(--mono);cursor:pointer;background:none;
}
.rowcount{margin-left:auto;font-size:12px;color:var(--muted);
  font-variant-numeric:tabular-nums}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th{
  font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
  text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);
  white-space:nowrap;font-weight:600;
}
th.num,td.num{text-align:right}
td{padding:7px 10px;border-bottom:1px solid var(--line2);vertical-align:top}
tr.main{cursor:pointer}
tr.main:hover td{background:#faf9f7}
td.num{font-family:var(--mono);font-size:12.5px;font-variant-numeric:tabular-nums;
  white-space:nowrap}
td.id{font-family:var(--mono);font-size:12.5px;white-space:nowrap}
.caret{display:inline-block;width:14px;color:var(--faint)}
.gt{color:var(--muted);font-size:12px;font-family:var(--mono);white-space:nowrap}
.pill{
  display:inline-block;border-radius:999px;padding:1px 8px;font-size:11.5px;
  font-weight:600;white-space:nowrap;
}
.pill.caught{background:var(--catch-bg);color:var(--catch)}
.pill.missed{background:var(--miss-bg);color:var(--miss)}
.pill.fp{background:var(--fp-bg);color:var(--fp)}
.pill.tn{background:var(--tn-bg);color:var(--tn)}
.offmode{
  font-size:10px;color:var(--faint);border:1px solid var(--line);border-radius:4px;
  padding:0 4px;margin-left:6px;letter-spacing:.03em;white-space:nowrap;
}
.verif{font-size:12px;color:var(--muted);white-space:nowrap}
.verif .flip{color:var(--fp);font-weight:600}
.yes{font-weight:650}
.no{color:var(--faint)}
tr.detail td{background:#fbfaf8;padding:12px 14px 14px}
.dhead{font-size:12px;color:var(--muted);font-family:var(--mono);margin-bottom:8px}
.scard{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:6px 0;
  background:var(--surface)}
.shead{font-size:12px;font-family:var(--mono);color:var(--muted)}
.shead .flagged{color:var(--fp);font-weight:700}
.shead .clean{color:var(--catch)}
.ev{
  font-family:var(--mono);font-size:12px;background:var(--bg);border-radius:6px;
  padding:6px 9px;margin:6px 0;white-space:pre-wrap;word-break:break-word;
}
.ev .ei{color:var(--muted)}
.reason{font-size:12.5px;margin:7px 0 0;white-space:pre-wrap;word-break:break-word}
.vblock{
  margin-top:9px;font-size:12.5px;border-top:1px dashed var(--line);padding-top:8px;
  white-space:pre-wrap;word-break:break-word;
}
.vblock .ok{color:var(--catch);font-weight:650}
.vblock .refuted{color:var(--miss);font-weight:650}
.parseerr{color:var(--miss);font-size:12px;margin-top:6px;font-family:var(--mono)}
.empty{padding:22px 10px;color:var(--muted);font-size:13px;text-align:center}
footer{color:var(--faint);font-size:12px;padding:10px 0 36px}
button:focus-visible,tr.main:focus-visible{outline:2px solid var(--focus);
  outline-offset:2px}
@media (prefers-reduced-motion: reduce){*{transition:none!important}}
</style>
</head>
<body>
<header class="masthead">
  <div class="wrap">
    <div class="eyebrow">agentmon &middot; eval run report</div>
    <div class="titlerow"><h1>@@LABEL@@</h1>@@BADGES@@</div>
    @@META@@
  </div>
</header>
<main class="wrap">
  <nav class="chips" id="chips" aria-label="Monitor filter"></nav>
  <section class="kpis" id="kpis"></section>
  <section class="panel">
    <div class="panel-head">
      <h2>Flags by failure category</h2>
      <p class="sub">Bars count flags by monitor-emitted category; a flag on a labeled
        failure is a true catch, on a benign transcript a false positive.
        Click a bar to filter the table below.</p>
    </div>
    <div id="bars"></div>
    <div class="legend">
      <span><span class="sw" style="background:var(--catch)"></span>true catches</span>
      <span><span class="sw" style="background:var(--fp)"></span>false positives</span>
    </div>
  </section>
  <section class="panel">
    <div class="panel-head">
      <h2>Monitor &times; transcript drill-down</h2>
      <p class="sub">One row per verdict, joined to ground truth. Click a row for
        per-sample evidence, reasoning, and verification.</p>
    </div>
    <div class="toolbar" id="toolbar"></div>
    <div class="tablewrap">
      <table>
        <thead><tr>
          <th>transcript</th><th>ground truth</th><th>monitor</th><th>flagged</th>
          <th class="num">fraction flagged</th><th class="num">mean suspicion</th>
          <th class="num">calibrated score</th><th>outcome</th><th>verification</th>
        </tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
  </section>
</main>
<footer class="wrap">agentmon run-results viewer &middot; verdict vs ground truth
  &middot; flagged = fraction_flagged &gt; 0 (post-verification)</footer>
<script>const DATA=@@DATA@@;</script>
<script>
(function(){
"use strict";
var M=DATA.monitors, ROWS=DATA.rows, MODES=DATA.modes;
var state={monitor:"all", mode:null, err:"all"};
var expanded={};
var TAG_TEXT={caught:"caught", missed:"missed", fp:"false positive", tn:"true negative"};
function $(id){return document.getElementById(id);}
function esc(s){
  return String(s).replace(/[&<>"']/g,function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}[c];
  });
}
function fmt(v,d){return v==null?"\\u2014":Number(v).toFixed(d==null?3:d);}
function ids(){return Object.keys(M);}
function rowsForMonitor(){
  return ROWS.filter(function(r){
    return state.monitor==="all"||r.monitor===state.monitor;
  });
}
function modeMatch(r){
  return !state.mode||r.label===state.mode||r.cats.indexOf(state.mode)>=0;
}
function errMatch(r){return state.err==="all"||r.tag===state.err;}
function ownFailTotal(id){
  var spec=M[id].specialist;
  return ROWS.filter(function(r){
    return r.monitor===id&&r.label!=="benign"&&(!spec||r.label===id);
  }).length;
}
function benignTotal(id){
  return ROWS.filter(function(r){return r.monitor===id&&r.label==="benign";}).length;
}
function sumOver(f){
  return ids().reduce(function(a,id){return a+f(id);},0);
}
function renderChips(){
  var parts=[chipHtml("all","All monitors",null)];
  ids().forEach(function(id){
    parts.push(chipHtml(id,id,M[id].own_missed_n+M[id].benign_fp_n));
  });
  $("chips").innerHTML=parts.join("");
}
function chipHtml(value,text,errs){
  var on=state.monitor===value?" on":"";
  var n=errs==null?"":"<span class=\\"n"+(errs>0?" bad":"")+"\\">"+errs+"</span>";
  return "<button class=\\"chip"+on+"\\" data-monitor=\\""+esc(value)+"\\" "+
    "aria-pressed=\\""+(on?"true":"false")+"\\">"+esc(text)+n+"</button>";
}
function kpiCard(cls,value,label,sub){
  return "<div class=\\"kpi "+cls+"\\"><div class=\\"v\\">"+value+"</div>"+
    "<div class=\\"l\\">"+label+"</div><div class=\\"s\\">"+sub+"</div></div>";
}
function renderKpis(){
  var all=state.monitor==="all";
  var m=all?null:M[state.monitor];
  var ownMissed=all?sumOver(function(id){return M[id].own_missed_n;}):m.own_missed_n;
  var benignFp=all?sumOver(function(id){return M[id].benign_fp_n;}):m.benign_fp_n;
  var hnFp=all?sumOver(function(id){return M[id].hn_fp_n;}):m.hn_fp_n;
  var flips=all?sumOver(function(id){return M[id].flips;}):m.flips;
  var mech=all?sumOver(function(id){return M[id].mech_flips;}):m.mech_flips;
  var ownTotal=all?sumOver(ownFailTotal):ownFailTotal(state.monitor);
  var benTotal=all?sumOver(benignTotal):benignTotal(state.monitor);
  var auroc=all?null:(m.specialist?m.auroc_own:m.auroc_all);
  var aurocSub=all?"per-monitor metric \\u2014 select a monitor":
    (m.specialist?"own failures vs all benigns":"all-modes monitor: all failures");
  var recallSub=all?"per-monitor metric \\u2014 select a monitor":
    "all-category failures at a 5% false-alarm budget";
  $("kpis").innerHTML=
    kpiCard("",fmt(auroc),"AUROC (own-mode)",aurocSub)+
    kpiCard("",fmt(all?null:m.recall_at_5pct),"Recall @ 5% FPR",recallSub)+
    kpiCard(ownMissed>0?"bad":"",String(ownMissed),"Own failures missed",
      "of "+ownTotal+" own-mode failure verdicts")+
    kpiCard(benignFp>0?"warn":"",String(benignFp),"Benign false positives",
      "of "+benTotal+" benign verdicts \\u00b7 "+hnFp+" hard negatives")+
    kpiCard("",String(flips),"Verification flips",
      mech+" mechanical (quote mismatch)");
}
function renderBars(){
  var buckets={};
  MODES.forEach(function(md){buckets[md]={c:0,f:0};});
  rowsForMonitor().forEach(function(r){
    if(!r.flagged)return;
    r.cats.forEach(function(cat){
      if(!buckets[cat])buckets[cat]={c:0,f:0};
      if(r.label==="benign")buckets[cat].f++;else buckets[cat].c++;
    });
  });
  var max=1;
  MODES.forEach(function(md){max=Math.max(max,buckets[md].c+buckets[md].f);});
  $("bars").innerHTML=MODES.map(function(md){
    var b=buckets[md];
    var on=state.mode===md?" on":"";
    var wc=(b.c/max*100).toFixed(2),wf=(b.f/max*100).toFixed(2);
    return "<button class=\\"barrow"+on+"\\" data-mode=\\""+esc(md)+"\\" "+
      "aria-pressed=\\""+(on?"true":"false")+"\\">"+
      "<span class=\\"bl\\">"+esc(md)+"</span>"+
      "<span class=\\"track\\"><span class=\\"seg-c\\" style=\\"width:"+wc+"%\\"></span>"+
      "<span class=\\"seg-f\\" style=\\"width:"+wf+"%\\"></span></span>"+
      "<span class=\\"bn\\"><b class=\\"c\\">"+b.c+"</b> caught \\u00b7 "+
      "<b class=\\"f\\">"+b.f+"</b> FP</span></button>";
  }).join("");
}
function errCounts(){
  var rs=rowsForMonitor().filter(modeMatch);
  var c={all:rs.length,missed:0,fp:0};
  rs.forEach(function(r){if(c[r.tag]!=null)c[r.tag]++;});
  return c;
}
function renderToolbar(visibleN){
  var c=errCounts();
  var btn=function(value,text,n){
    var on=state.err===value?" on":"";
    return "<button class=\\"fbtn"+on+"\\" data-err=\\""+value+"\\" "+
      "aria-pressed=\\""+(on?"true":"false")+"\\">"+text+
      " <span class=\\"n\\">"+n+"</span></button>";
  };
  var modeFlag=state.mode?
    "<button class=\\"modeflag\\" id=\\"clearmode\\" title=\\"clear category filter\\">"+
    "category: "+esc(state.mode)+" \\u2715</button>":"";
  $("toolbar").innerHTML=
    btn("all","All rows",c.all)+
    btn("missed","Misses only",c.missed)+
    btn("fp","FPs only",c.fp)+
    modeFlag+
    "<span class=\\"rowcount\\">"+visibleN+" of "+ROWS.length+" rows</span>";
}
function sortRows(rows){
  function rank(r){return (r.tag==="missed"||r.tag==="fp")?(r.off_mode?1:0):2;}
  return rows.slice().sort(function(a,b){
    return rank(a)-rank(b)||b.calibrated_score-a.calibrated_score||
      (a.transcript<b.transcript?-1:a.transcript>b.transcript?1:0)||
      (a.monitor<b.monitor?-1:a.monitor>b.monitor?1:0);
  });
}
function key(r){return r.monitor+"|"+r.transcript;}
function verifCell(r){
  if(r.verified_n===0)return "\\u2014";
  var s=r.verified_n+" run";
  if(r.flips>0){
    s+=" \\u00b7 <span class=\\"flip\\">"+r.flips+" flip"+(r.flips>1?"s":"")+"</span>";
    if(r.mech_flips>0)s+=" ("+r.mech_flips+" mech)";
  }else{
    s+=" \\u00b7 upheld";
  }
  return s;
}
function rowHtml(r){
  var k=key(r),open=!!expanded[k];
  var off=r.off_mode?"<span class=\\"offmode\\">off-mode</span>":"";
  return "<tr class=\\"main\\" tabindex=\\"0\\" data-key=\\""+esc(k)+"\\" "+
    "aria-expanded=\\""+(open?"true":"false")+"\\">"+
    "<td class=\\"id\\"><span class=\\"caret\\">"+(open?"\\u25be":"\\u25b8")+"</span>"+
    esc(r.transcript)+"</td>"+
    "<td><span class=\\"gt\\">"+esc(r.stratum)+"</span>"+off+"</td>"+
    "<td class=\\"id\\">"+esc(r.monitor)+"</td>"+
    "<td>"+(r.flagged?"<span class=\\"yes\\">yes</span>":"<span class=\\"no\\">no</span>")+
    "</td>"+
    "<td class=\\"num\\">"+fmt(r.fraction_flagged,2)+"</td>"+
    "<td class=\\"num\\">"+fmt(r.mean_suspicion,1)+"</td>"+
    "<td class=\\"num\\">"+fmt(r.calibrated_score,3)+"</td>"+
    "<td><span class=\\"pill "+r.tag+"\\">"+TAG_TEXT[r.tag]+"</span></td>"+
    "<td class=\\"verif\\">"+verifCell(r)+"</td></tr>"+
    "<tr class=\\"detail\\""+(open?"":" hidden")+" data-detail=\\""+esc(k)+"\\">"+
    "<td colspan=\\"9\\">"+(open?detailHtml(r):"")+"</td></tr>";
}
function detailHtml(r){
  var head="ground truth: "+esc(r.stratum);
  if(r.fail_idx.length>0)head+=" \\u00b7 failure at event(s) ["+r.fail_idx.join(", ")+"]";
  if(r.cats.length>0)head+=" \\u00b7 flag categories: "+esc(r.cats.join(", "));
  var cards=r.samples.map(function(s,i){
    var st=s.flagged?"<span class=\\"flagged\\">flagged</span>":
      "<span class=\\"clean\\">no flag</span>";
    var h="<div class=\\"shead\\">sample "+(i+1)+" \\u00b7 suspicion "+s.suspicion_score+
      " \\u00b7 "+st;
    if(s.categories.length>0)h+=" \\u00b7 "+esc(s.categories.join(", "));
    h+="</div>";
    var ev=s.evidence.map(function(e){
      return "<div class=\\"ev\\"><span class=\\"ei\\">event #"+e.event_index+
        "</span>  "+esc(e.quote)+"</div>";
    }).join("");
    var reason=s.reasoning?"<p class=\\"reason\\">"+esc(s.reasoning)+"</p>":"";
    var perr=s.parse_error?
      "<div class=\\"parseerr\\">parse error: "+esc(s.parse_error)+"</div>":"";
    var v="";
    if(s.verification){
      var o=s.verification;
      var head2=o.supported?"<span class=\\"ok\\">\\u2713 upheld</span>":
        "<span class=\\"refuted\\">\\u2717 refuted \\u2014 flag flipped</span>";
      var qm=o.quote_match==null?"\\u2014":
        (o.quote_match?"yes":(o.supported?"no":"no (mechanical flip)"));
      v="<div class=\\"vblock\\">verification: "+head2+
        " \\u00b7 quote match: "+qm+
        (o.reasoning?"\\n"+esc(o.reasoning):"")+"</div>";
    }
    return "<div class=\\"scard\\">"+h+ev+reason+perr+v+"</div>";
  }).join("");
  return "<div class=\\"dhead\\">"+head+"</div>"+cards;
}
function renderTable(){
  var rows=sortRows(rowsForMonitor().filter(function(r){
    return modeMatch(r)&&errMatch(r);
  }));
  renderToolbar(rows.length);
  $("tbody").innerHTML=rows.length===0?
    "<tr><td colspan=\\"9\\"><div class=\\"empty\\">"+
    "No rows match the current filters.</div></td></tr>":
    rows.map(rowHtml).join("");
}
function renderAll(){renderChips();renderKpis();renderBars();renderTable();}
function toggleRow(k){
  expanded[k]=!expanded[k];
  var main=document.querySelector("tr.main[data-key=\\""+CSS.escape(k)+"\\"]");
  var det=document.querySelector("tr.detail[data-detail=\\""+CSS.escape(k)+"\\"]");
  if(!main||!det)return;
  if(expanded[k]){
    if(!det.firstElementChild.innerHTML){
      var r=ROWS.filter(function(x){return key(x)===k;})[0];
      if(r)det.firstElementChild.innerHTML=detailHtml(r);
    }
    det.hidden=false;
    main.setAttribute("aria-expanded","true");
    main.querySelector(".caret").textContent="\\u25be";
  }else{
    det.hidden=true;
    main.setAttribute("aria-expanded","false");
    main.querySelector(".caret").textContent="\\u25b8";
  }
}
document.addEventListener("click",function(e){
  var chip=e.target.closest(".chip");
  if(chip){
    state.monitor=chip.getAttribute("data-monitor");
    renderAll();return;
  }
  var bar=e.target.closest(".barrow");
  if(bar){
    var md=bar.getAttribute("data-mode");
    state.mode=state.mode===md?null:md;
    renderBars();renderTable();return;
  }
  var fbtn=e.target.closest(".fbtn");
  if(fbtn){state.err=fbtn.getAttribute("data-err");renderTable();return;}
  if(e.target.closest("#clearmode")){
    state.mode=null;renderBars();renderTable();return;
  }
  var main=e.target.closest("tr.main");
  if(main)toggleRow(main.getAttribute("data-key"));
});
document.addEventListener("keydown",function(e){
  if(e.key!=="Enter"&&e.key!==" ")return;
  var main=e.target.closest&&e.target.closest("tr.main");
  if(main){e.preventDefault();toggleRow(main.getAttribute("data-key"));}
});
renderAll();
})();
</script>
</body>
</html>
"""


def _html_page(data: dict[str, Any]) -> str:
    """Assemble the full document: static shell + data blob + inline app script."""
    meta = data["meta"]
    label = html.escape(str(meta["label"]))
    badges = "".join(
        f'<span class="badge">{name}</span>'
        for name, on in (("local", meta["mock"] is False and meta["local"]), ("mock", meta["mock"]))
        if on
    )
    return (
        _HTML_SHELL.replace("@@TITLE@@", f"agentmon report — {label}")
        .replace("@@LABEL@@", label)
        .replace("@@BADGES@@", badges)
        .replace("@@META@@", _header_meta_html(meta))
        .replace("@@DATA@@", _json_for_script(data))
    )
