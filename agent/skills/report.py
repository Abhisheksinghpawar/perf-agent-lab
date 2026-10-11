"""Skill 9 - the final report: one self-contained HTML file (inline CSS + SVG charts, no JavaScript, no external requests)."""
from __future__ import annotations

from html import escape as e

COLORS = {"p50": "#22d3ee", "p95": "#a78bfa", "rps": "#34d399", "err": "#f87171", "cpu": "#fbbf24", "java": "#fb923c",
          "locust": "#94a3b8", "net": "#60a5fa", "rss": "#f472b6"}
VERDICT = {"healthy": ("Healthy", "#34d399"), "attention": ("Needs attention", "#fbbf24"), "failed": ("Run failed", "#f87171")}
SEV = {"high": "#f87171", "medium": "#fbbf24", "low": "#60a5fa", "info": "#94a3b8"}


def svg_chart(series: list[tuple[str, str, list[tuple[float, float]]]], bands: list[tuple[float, float]] | None = None,
              unit: str = "", w: int = 520, h: int = 190, y_max: float | None = None) -> str:
    """series = [(label, color, [(x, y), ...]), ...]; bands = x-ranges to shade (spikes)."""
    pad_l, pad_r, pad_t, pad_b = 46, 12, 14, 26
    xs = [x for _l, _c, pts in series for x, _y in pts] or [0, 1]
    ys = [y for _l, _c, pts in series for _x, y in pts] or [0, 1]
    x0, x1 = min(xs), max(xs) or 1
    top = y_max if y_max is not None else (max(ys) * 1.15 or 1)
    sx = lambda x: pad_l + (x - x0) / ((x1 - x0) or 1) * (w - pad_l - pad_r)  # noqa: E731
    sy = lambda y: h - pad_b - (y / top) * (h - pad_t - pad_b)               # noqa: E731
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" xmlns="http://www.w3.org/2000/svg">']
    for i in range(5):
        v = top * i / 4
        y = sy(v)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w - pad_r}" y2="{y:.1f}" stroke="#ffffff14"/>')
        out.append(f'<text x="{pad_l - 6}" y="{y + 3:.1f}" text-anchor="end" font-size="9" fill="#8b92b3">{v:.0f}</text>')
    for i in range(5):
        v = x0 + (x1 - x0) * i / 4
        out.append(f'<text x="{sx(v):.1f}" y="{h - 8}" text-anchor="middle" font-size="9" fill="#8b92b3">{v:.0f}s</text>')
    for a, b in bands or []:
        out.append(f'<rect x="{sx(a):.1f}" y="{pad_t}" width="{max(sx(b) - sx(a), 2):.1f}" height="{h - pad_t - pad_b}" fill="#f8717122"/>')
    for _label, color, pts in series:
        if len(pts) > 1:
            d = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in pts)
            out.append(f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="1.8" stroke-linejoin="round"/>')
    lx = pad_l
    for label, color, _pts in series:
        out.append(f'<rect x="{lx}" y="2" width="8" height="8" rx="2" fill="{color}"/><text x="{lx + 12}" y="10" font-size="9" fill="#c9cee8">{e(label)}</text>')
        lx += 18 + len(label) * 5.4
    if unit:
        out.append(f'<text x="{w - pad_r}" y="10" text-anchor="end" font-size="9" fill="#8b92b3">{e(unit)}</text>')
    out.append("</svg>")
    return "".join(out)


def _kpi(label: str, value: str, sub: str = "") -> str:
    return f'<div class="kpi"><div class="k">{e(label)}</div><div class="v">{e(value)}</div><div class="s">{e(sub)}</div></div>'


def _table(headers: list[str], rows: list[list[str]], cls: str = "") -> str:
    head = "".join(f"<th>{e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def build_html(R: dict) -> str:
    verdict_label, vcolor = VERDICT.get(R["verdict"], VERDICT["attention"])
    commit, an = R["commit"], R.get("analysis") or {}
    o = an.get("overall", {})
    res = an.get("resources") or {}
    tgt = R.get("target") or {}
    plabel = tgt.get("process_label") or "Tomcat JVM"
    has_repo = bool(commit.get("repo"))                      # a local folder without a git remote has nothing to link to
    repo_blob = f"{commit['repo']}/blob/{commit['sha']}/" + (commit["subdir"].strip("/") + "/" if commit.get("subdir") else "")
    commit_html = (f'<a href="{e(commit["repo"])}/commit/{e(commit["sha"])}" target="_blank" rel="noopener">{e(commit["short"])}</a>'
                   if has_repo else e(commit["short"]))

    # ---- pipeline trace ---------------------------------------------------------------------------
    steps = []
    for s in R["trace"]:
        badge = {"agent": "AGENT", "code": "CODE"}.get(s["kind"], "CODE")
        steps.append(f'<li class="st {e(s["status"])}"><span class="b {e(s["kind"])}">{badge}</span>'
                     f'<b>{e(s["title"])}</b><span class="d">{s.get("duration_s", 0)}s</span>'
                     f'<i>{e(s["status"])}{(" - " + e(s["detail"])) if s.get("detail") else ""}</i></li>')

    # ---- charts -------------------------------------------------------------------------------------
    charts = ""
    if an.get("timeline"):
        tl, rt = an["timeline"], an.get("resource_timeline", [])
        bands = [(s["start_rel"], s["end_rel"]) for s in an["spikes"]]
        charts = (
            '<div class="grid2">'
            f'<div class="card"><h4>Response time (ms)</h4>{svg_chart([("p50", COLORS["p50"], [(b["t"], b["p50"]) for b in tl if b["n"]]), ("p95", COLORS["p95"], [(b["t"], b["p95"]) for b in tl if b["n"]])], bands, "ms")}</div>'
            f'<div class="card"><h4>Throughput &amp; errors</h4>{svg_chart([("req/s", COLORS["rps"], [(b["t"], b["rps"]) for b in tl]), ("errors/bucket", COLORS["err"], [(b["t"], b["errors"]) for b in tl])], bands)}</div>'
            f'<div class="card"><h4>CPU (%)</h4>{svg_chart([("host", COLORS["cpu"], [(b["t"], b["cpu"]) for b in rt]), (plabel.lower(), COLORS["java"], [(b["t"], b["java_cpu"]) for b in rt]), ("locust", COLORS["locust"], [(b["t"], b["locust_cpu"]) for b in rt])], bands, "%", y_max=100)}</div>'
            f'<div class="card"><h4>Network (KB/s) &amp; {e(plabel)} memory (MB RSS)</h4>{svg_chart([("net KB/s", COLORS["net"], [(b["t"], b["net_kbs"]) for b in rt]), (f"{plabel.lower()} RSS MB", COLORS["rss"], [(b["t"], b["rss_mb"]) for b in rt])], bands)}</div>'
            "</div>")

    # ---- tables ---------------------------------------------------------------------------------------
    ep_rows = []
    for ep in an.get("endpoints", []):
        d = ep["delta_pct"]
        delta = "" if d is None else f'<span style="color:{"#f87171" if d > 25 else "#34d399" if d < -25 else "#8b92b3"}">{d:+.0f}%</span>'
        ep_rows.append([e(ep["name"]), str(ep["count"]), f'{ep["failures"]}', f'{ep["avg"]:.0f}', f'{ep["p50"]:.0f}', f'<b>{ep["p95"]:.0f}</b>',
                        f'{ep["p99"]:.0f}', f'{ep["max"]:.0f}', delta])
    endpoints = _table(["Endpoint", "Req", "Fail", "avg", "p50", "p95", "p99", "max", "vs baseline"], ep_rows, "num") if ep_rows else ""
    var = an.get("variance") or []
    var_note = ('<p class="m" style="margin-top:10px">Slower than baseline on the same commit, so treated as runner variance rather than a regression: '
                + ", ".join(f'{e(v["endpoint"])} ({v["delta_pct"]:+.0f}%)' for v in var) + ".</p>") if var else ""

    spikes = ""
    if an.get("spikes"):
        spikes = _table(["Window", "Peak p95", "Median p95", "Phase", "Slowest endpoint", "Errors", "CPU"],
                        [[f'{s["start_rel"]:.0f}-{s["end_rel"]:.0f}s', f'{s["peak_p95"]:.0f} ms', f'{s["median_p95"]:.0f} ms', e(s["phase"]),
                          e(s["top_endpoints"][0]["name"]) if s["top_endpoints"] else "-", str(s["errors"]), f'{s["cpu_avg"]:.0f}%']
                         for s in an["spikes"]])
    errors = ""
    if an.get("error_clusters"):
        errors = _table(["Endpoint", "Status", "Message", "Count", "First / last"],
                        [[e(c["endpoint"]), e(c["status"] or "-"), e(c["error"] or "-"), str(c["count"]), f'{c["first_rel"]:.0f}s / {c["last_rel"]:.0f}s']
                         for c in an["error_clusters"]])

    lg = R.get("logs") or {}
    gc = lg.get("gc", {})
    log_html = ""
    if lg:
        ex = "".join(f'<li><code>{e(x["type"])}</code> {e(x["message"])} <small>x{x["count"]} {e(x["where"])}</small></li>' for x in lg.get("exceptions", []))
        warn = "".join(f"<li>{e(w)}</li>" for w in lg.get("warnings", []))
        gc_card = ""                                       # only JVM apps log GC events
        if gc.get("count"):
            gc_card = (f'<div class="card"><h4>JVM garbage collection</h4><p class="m">{gc.get("count", 0)} events - total pause {gc.get("total_pause_ms", 0)} ms - '
                       f'max {gc.get("max_pause_ms", 0)} ms - full GCs {gc.get("full_gc", 0)} - heap after GC (max) {gc.get("heap_after_max_mb", 0)} MB</p></div>')
        log_html =(f'<div class="grid2"><div class="card"><h4>Application log</h4><p class="m">{lg.get("log_lines", 0)} lines scanned - levels: '
                    f'{e(", ".join(f"{k} {v}" for k, v in lg.get("levels", {}).items()) or "nothing logged during the test window (startup lines excluded)")}</p>'
                    f'{"<ul>" + ex + "</ul>" if ex else "<p class=m>No exceptions logged during the test.</p>"}'
                    f'{"<ul>" + warn + "</ul>" if warn else ""}</div>'
                    f'{gc_card}</div>')

    # ---- findings ------------------------------------------------------------------------------------------
    cards = []
    for f in R["findings"]:
        refs = "".join((f'<a href="{e(repo_blob + r["file"])}#L{int(r.get("line", 1))}" target="_blank" rel="noopener">' if has_repo else "<code>")
                       + f'{e(r["file"].split("/")[-1])}:{int(r.get("line", 1))}' + ("</a>" if has_repo else "</code>")
                       + f' {e(r.get("note", ""))}<br>'
                       for r in f.get("code_refs", []) if r.get("file"))
        ev = "".join(f"<li>{e(str(x))}</li>" for x in f.get("evidence", []))
        rec = " <span class='rec'>recurring</span>" if f.get("recurring") else ""
        cards.append(f'<div class="card f" style="border-left-color:{SEV.get(f.get("severity"), "#94a3b8")}">'
                     f'<h4>{e(f["title"])} <span class="sev" style="background:{SEV.get(f.get("severity"), "#94a3b8")}">{e(f.get("severity", "info"))}</span>{rec}</h4>'
                     f'<ul class="ev">{ev}</ul><p><b>Suspected cause:</b> {e(f.get("suspected_cause", "unknown"))}</p>'
                     f'{"<p class=m>" + refs + "</p>" if refs else ""}<p><b>Recommendation:</b> {e(f.get("recommendation", ""))}</p></div>')
    findings = "".join(cards) or '<div class="card"><p>No findings: no errors, regressions or steady-state spikes in this run.</p></div>'
    imps = ""
    if R["improvements"]:
        imps = _table(["#", "Improvement", "Expected impact", "Effort"],
                      [[str(i.get("priority", n)), e(i.get("action", "")), e(i.get("expected_impact", "")), e(i.get("effort", ""))]
                       for n, i in enumerate(R["improvements"], 1)])

    # ---- self-heal ----------------------------------------------------------------------------------------
    heal = ""
    h = R.get("heal")
    if h:
        rows = [[str(x["attempt"]), "pass" if x["passed"] else "fail", e(x.get("stage", "")), e("; ".join(x.get("reasons", [])) or "-")] for x in h["history"]]
        heal = ('<h2>Self-healing</h2><div class="card">'
                f'<p>The smoke test failed, so the repair agent rewrote the Locust script ({h["attempts"]} attempt(s)): '
                f'<b>{"healed" if h["healed"] else "not healed"}</b>. {e(h.get("summary", ""))}</p>'
                f'{_table(["Attempt", "Result", "Stage", "Notes"], rows) if rows else ""}'
                f'{"<pre>" + e(h["diff"][:5000]) + "</pre>" if h.get("diff") else ""}</div>')

    # ---- tokens ---------------------------------------------------------------------------------------------
    tok = R["tokens"]
    trows = [[e(k), str(v["calls"]), str(v["prompt"]), str(v["completion"])] for k, v in tok["by_step"].items()]
    skipped = "".join(f'<li><b>{e(s["step"])}</b> skipped - {e(s["reason"])}' + (f' (last time it used ~{s["est_tokens_saved"]} tokens)' if s.get("est_tokens_saved") else "") + "</li>"
                      for s in tok["skipped"])
    token_html = (f'<div class="card"><p class="m">LLM tokens this run: <b>{tok["total"]}</b>. Deterministic steps (git, build, smoke, load, metrics, '
                  f'statistics, log parsing, report) never call the model.</p>'
                  f'{_table(["LLM step", "Calls", "Prompt", "Completion"], trows) if trows else ""}'
                  f'{"<ul>" + skipped + "</ul>" if skipped else ""}</div>')

    sm = R["smoke"]
    kpis = "".join([
        _kpi("Requests", f'{o.get("requests", 0):,}', f'{R["load"]["users"]} users, {R["load"]["duration_s"]} s'),
        _kpi("Throughput", f'{o.get("rps", 0)} req/s'),
        _kpi("p95 latency", f'{o.get("p95", 0):.0f} ms', f'p50 {o.get("p50", 0):.0f} / p99 {o.get("p99", 0):.0f}'),
        _kpi("Error rate", f'{o.get("error_rate", 0):.2%}', f'{o.get("failures", 0)} failed'),
        _kpi("CPU (host)", f'{res.get("cpu_avg", 0)}%', f'max {res.get("cpu_max", 0)}%, {plabel} avg {res.get("java_cpu_avg", 0)}%'),
        _kpi(f"{plabel} memory", f'{res.get("java_rss_max_mb", 0):.0f} MB', "peak RSS"),
        _kpi("Smoke test", "passed" if sm["passed"] else "failed", f'{sm["requests"]} req' + (" - script self-healed" if (R.get("heal") or {}).get("healed") else "")),
        _kpi("LLM tokens", f'{tok["total"]}'),
    ])

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Performance run {e(R["run_id"])} - {e(verdict_label)}</title>
<style>
:root{{--bg:#05060f;--card:#ffffff0a;--b:#ffffff17;--t:#e8ebf7;--m:#8b92b3}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(1100px 500px at 50% -10%,#14123a,var(--bg) 60%);color:var(--t);font:14px/1.55 Inter,system-ui,Segoe UI,sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:36px 22px 70px}}h1{{font:600 28px/1.15 system-ui;margin:6px 0 4px}}h2{{font:600 18px system-ui;margin:34px 0 12px}}h4{{margin:0 0 8px;font-size:14px}}
.badge{{display:inline-block;padding:4px 12px;border-radius:99px;font-weight:600;font-size:12px;color:#05060f}}.m{{color:var(--m)}}small{{color:var(--m)}}
.card{{background:var(--card);border:1px solid var(--b);border-radius:14px;padding:16px 18px}}.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0}}.kpi{{background:var(--card);border:1px solid var(--b);border-radius:12px;padding:12px 14px}}
.kpi .k{{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--m)}}.kpi .v{{font:600 22px system-ui}}.kpi .s{{font-size:11px;color:var(--m)}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}th,td{{padding:7px 9px;border-bottom:1px solid var(--b);text-align:left}}th{{color:var(--m);font-weight:500}}table.num td:not(:first-child){{text-align:right;font-variant-numeric:tabular-nums}}table.num th:not(:first-child){{text-align:right}}
ol.trace{{list-style:none;padding:0;margin:0;display:grid;gap:6px}}.st{{display:flex;gap:10px;align-items:center;background:var(--card);border:1px solid var(--b);border-radius:10px;padding:8px 12px}}
.st i{{color:var(--m);font-style:normal;font-size:12px;flex:1}}.st .d{{color:var(--m);font-variant-numeric:tabular-nums;min-width:48px;text-align:right}}.st.failed{{border-color:#f87171}}.st.skipped{{opacity:.6}}
.b{{font-size:10px;font-weight:700;letter-spacing:.08em;padding:2px 7px;border-radius:6px}}.b.agent{{background:#a78bfa;color:#05060f}}.b.code{{background:#ffffff1a;color:#c9cee8}}
.f{{border-left:4px solid;margin-bottom:12px}}.sev{{font-size:10px;padding:2px 8px;border-radius:99px;color:#05060f;text-transform:uppercase;vertical-align:middle}}.rec{{font-size:10px;color:#fbbf24}}
ul.ev{{margin:6px 0 8px 18px;padding:0}}a{{color:#22d3ee}}pre{{background:#00000055;border:1px solid var(--b);border-radius:10px;padding:12px;overflow:auto;font-size:11.5px}}code{{background:#ffffff14;padding:1px 5px;border-radius:5px}}
svg{{width:100%;height:auto}}.foot{{margin-top:40px;color:var(--m);font-size:12px}}@media(max-width:760px){{.grid2{{grid-template-columns:1fr}}.kpis{{grid-template-columns:repeat(2,1fr)}}}}
</style></head><body><div class="wrap">
<div class="m">perf-agent-lab - automated performance run</div>
<h1>{e(tgt.get("name") or commit["repo"].split("github.com/")[-1] or "Performance run")} <span class="badge" style="background:{vcolor}">{e(verdict_label)}</span></h1>
<div class="m">commit {commit_html} - {e(commit["subject"])} - run {e(R["run_id"])} (UTC) - total {R["duration_s"]} s - LLM: {e(R.get("llm_mode", "off"))}</div>
<div class="card" style="margin-top:16px"><b>Summary.</b> {e(R["summary"])}</div>
<div class="kpis">{kpis}</div>
<h2>Pipeline</h2><ol class="trace">{"".join(steps)}</ol>
{heal}
<h2>Results</h2>{charts}
<div class="card" style="margin-top:14px">{endpoints or "<p class=m>No endpoint data.</p>"}{var_note}</div>
{"<h2>Latency spikes</h2><div class='card'>" + spikes + "</div>" if spikes else ""}
{"<h2>Errors</h2><div class='card'>" + errors + "</div>" if errors else ""}
<h2>Logs</h2>{log_html}
<h2>Findings</h2>{findings}
{"<h2>Improvement plan</h2><div class='card'>" + imps + "</div>" if imps else ""}
<h2>Token usage</h2>{token_html}
<p class="foot">Locust and the application ran on the same machine (a shared CI runner), so absolute numbers are indicative and CPU contention is expected;
use this report to compare runs and spot regressions. Load: {R["load"]["users"]} users for {R["load"]["duration_s"]} s after a {R["smoke"]["requests"]}-request smoke test.</p>
</div></body></html>"""
