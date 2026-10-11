"""Skill 6 - deterministic analysis of the load test. No LLM involved: percentiles, throughput, spikes, error clusters,
resource saturation and comparison with the stored baseline. Its compact output is what later steps (and the LLM) look at."""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict

from .locust_runner import percentile


def _stats(values: list[float]) -> dict:
    if not values:
        return {"avg": 0.0, "p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    return {"avg": round(sum(values) / len(values), 1), "p50": round(percentile(values, 50), 1),
            "p90": round(percentile(values, 90), 1), "p95": round(percentile(values, 95), 1),
            "p99": round(percentile(values, 99), 1), "max": round(max(values), 1)}


def analyze(records: list[dict], metrics: list[dict], cfg: dict, baseline: dict | None = None, same_code: bool = False) -> dict:
    """same_code: every baseline run used this exact commit, so an endpoint that got slower is environment variance
    (e.g. a busy shared CI runner), not a code regression. It is reported as `variance` and does not change the verdict."""
    a = cfg["analysis"]
    baseline = baseline or {}
    if not records:
        return {"overall": {"requests": 0}, "endpoints": [], "timeline": [], "spikes": [], "error_clusters": [],
                "resources": {}, "resource_timeline": [], "regressions": [], "variance": [], "improvements": [], "verdict": "attention",
                "reasons": ["no requests were recorded"]}

    t0 = min(r["ts"] for r in records)
    t1 = max(r["ts"] + r["rt"] / 1000 for r in records)
    dur = max(t1 - t0, 1e-6)
    rts = [r["rt"] for r in records]
    fails = [r for r in records if not r.get("ok")]
    overall = {"requests": len(records), "failures": len(fails), "error_rate": round(len(fails) / len(records), 4),
               "rps": round(len(records) / dur, 2), "duration_s": round(dur, 1), "t_start": t0, "t_end": t1, **_stats(rts)}

    # ---- per endpoint ---------------------------------------------------------------------------
    by_ep: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_ep[r["name"]].append(r)
    endpoints, regressions, variance, improvements = [], [], [], []
    for name, rs in by_ep.items():
        st = _stats([x["rt"] for x in rs])
        f = sum(1 for x in rs if not x.get("ok"))
        base = baseline.get(name, {}).get("p95")
        delta = round((st["p95"] - base) / base * 100, 1) if base else None
        ep = {"name": name, "count": len(rs), "failures": f, "error_rate": round(f / len(rs), 4),
              "rps": round(len(rs) / dur, 2), **st, "baseline_p95": base, "delta_pct": delta}
        endpoints.append(ep)
        if base:
            diff = st["p95"] - base
            if delta is not None and delta > float(a["regression_pct"]) and diff > float(a["regression_min_ms"]):
                (variance if same_code else regressions).append({"endpoint": name, "p95": st["p95"], "baseline_p95": base, "delta_pct": delta})
            elif delta is not None and delta < -float(a["regression_pct"]) and -diff > float(a["regression_min_ms"]):
                improvements.append({"endpoint": name, "p95": st["p95"], "baseline_p95": base, "delta_pct": delta})
    endpoints.sort(key=lambda e: e["p95"], reverse=True)

    # ---- time buckets -----------------------------------------------------------------------------
    w = float(a["window_s"])
    nb = int(dur // w) + 1
    buckets: list[list[dict]] = [[] for _ in range(nb)]
    for r in records:
        buckets[min(int((r["ts"] - t0) // w), nb - 1)].append(r)
    mbuckets: list[list[dict]] = [[] for _ in range(nb)]
    for m in metrics:
        i = int((m["ts"] - t0) // w)
        if 0 <= i < nb:
            mbuckets[i].append(m)

    def mavg(rows, key):
        return round(sum(x[key] for x in rows) / len(rows), 1) if rows else 0.0

    timeline, res_timeline = [], []
    for i in range(nb):
        rs = buckets[i]
        vals = [x["rt"] for x in rs]
        timeline.append({"t": round(i * w, 1), "rps": round(len(rs) / w, 2), "p50": round(percentile(vals, 50), 1),
                         "p95": round(percentile(vals, 95), 1), "errors": sum(1 for x in rs if not x.get("ok")),
                         "n": len(rs)})
        rows = mbuckets[i]
        res_timeline.append({"t": round(i * w, 1), "cpu": mavg(rows, "cpu_pct"), "java_cpu": mavg(rows, "java_cpu_pct"),
                             "rss_mb": mavg(rows, "java_rss_mb"), "locust_cpu": mavg(rows, "locust_cpu_pct"),
                             "net_kbs": round(mavg(rows, "net_sent_kbs") + mavg(rows, "net_recv_kbs"), 1)})

    # ---- spikes --------------------------------------------------------------------------------------
    valid = [(i, b["p95"]) for i, b in enumerate(timeline) if b["n"] >= 5]
    spikes = []
    if len(valid) >= 4:
        med = statistics.median(p for _i, p in valid)
        thr = max(float(a["spike_factor"]) * med, float(a["spike_min_ms"]))
        hot = [i for i, p in valid if p > thr]
        groups, cur = [], []
        for i in hot:
            if cur and i == cur[-1] + 1:
                cur.append(i)
            else:
                if cur:
                    groups.append(cur)
                cur = [i]
        if cur:
            groups.append(cur)
        for g in groups:
            s_rel, e_rel = g[0] * w, (g[-1] + 1) * w
            win = [r for r in records if s_rel <= r["ts"] - t0 < e_rel]
            per_ep: dict[str, list[float]] = defaultdict(list)
            for r in win:
                per_ep[r["name"]].append(r["rt"])
            top = sorted(({"name": n, "p95": round(percentile(v, 95), 1), "count": len(v)} for n, v in per_ep.items()),
                         key=lambda x: x["p95"], reverse=True)[:3]
            mrows = [m for m in metrics if s_rel <= m["ts"] - t0 < e_rel]
            spikes.append({"start_rel": round(s_rel, 1), "end_rel": round(e_rel, 1),
                           "start_ts": round(t0 + s_rel, 2), "end_ts": round(t0 + e_rel, 2),
                           "peak_p95": round(max(timeline[i]["p95"] for i in g), 1), "median_p95": round(med, 1),
                           "phase": "warm-up" if g[0] * w < 2 * w + 1e-9 else "steady-state",
                           "top_endpoints": top, "errors": sum(1 for r in win if not r.get("ok")),
                           "cpu_avg": mavg(mrows, "cpu_pct"), "java_cpu_avg": mavg(mrows, "java_cpu_pct")})

    # ---- error clusters -----------------------------------------------------------------------------------
    clusters: dict[tuple, dict] = {}
    for r in fails:
        key = (r["name"], str(r.get("status", "")), (r.get("err") or "")[:100])
        c = clusters.setdefault(key, {"endpoint": key[0], "status": key[1], "error": key[2], "count": 0,
                                      "first_rel": r["ts"] - t0, "last_rel": r["ts"] - t0})
        c["count"] += 1
        c["first_rel"] = min(c["first_rel"], r["ts"] - t0)
        c["last_rel"] = max(c["last_rel"], r["ts"] - t0)
    error_clusters = sorted(({**c, "first_rel": round(c["first_rel"], 1), "last_rel": round(c["last_rel"], 1)}
                             for c in clusters.values()), key=lambda c: c["count"], reverse=True)[:8]

    # ---- resources ------------------------------------------------------------------------------------------
    res: dict = {}
    if metrics:
        cpu = [m["cpu_pct"] for m in metrics]
        rss = [m["java_rss_mb"] for m in metrics if m["java_rss_mb"] > 0]
        mins = max((metrics[-1]["ts"] - metrics[0]["ts"]) / 60, 1e-6)
        res = {"cpu_avg": round(sum(cpu) / len(cpu), 1), "cpu_max": round(max(cpu), 1),
               "cpu_saturated_s": sum(1 for c in cpu if c >= float(a["cpu_saturation_pct"])),
               "java_cpu_avg": round(sum(m["java_cpu_pct"] for m in metrics) / len(metrics), 1),
               "java_cpu_max": round(max(m["java_cpu_pct"] for m in metrics), 1),
               "locust_cpu_avg": round(sum(m["locust_cpu_pct"] for m in metrics) / len(metrics), 1),
               "mem_pct_max": round(max(m["mem_pct"] for m in metrics), 1),
               "java_rss_start_mb": round(rss[0], 1) if rss else 0, "java_rss_end_mb": round(rss[-1], 1) if rss else 0,
               "java_rss_max_mb": round(max(rss), 1) if rss else 0,
               "java_rss_growth_mb_per_min": round(((rss[-1] - rss[0]) / mins), 1) if len(rss) > 1 else 0,
               "net_sent_mb": round(sum(m["net_sent_kbs"] for m in metrics) / 1024, 2),
               "net_recv_mb": round(sum(m["net_recv_kbs"] for m in metrics) / 1024, 2),
               "cpu_count": None}
        res["load_generator_shares_host"] = True        # Locust runs on the same machine as the app (by design here)

    # ---- verdict ------------------------------------------------------------------------------------------------
    reasons = []
    if overall["error_rate"] > float(a["error_rate_alert"]):
        reasons.append(f"error rate {overall['error_rate']:.1%} above {float(a['error_rate_alert']):.1%}")
    steady = [s for s in spikes if s["phase"] != "warm-up"]
    if steady:
        reasons.append(f"{len(steady)} latency spike(s) in steady state (peak p95 {max(s['peak_p95'] for s in steady):.0f} ms)")
    if regressions:
        reasons.append(f"{len(regressions)} endpoint(s) slower than baseline")
    if res and res["cpu_saturated_s"] >= 10:
        reasons.append(f"CPU above {a['cpu_saturation_pct']}% for {res['cpu_saturated_s']} s")
    return {"overall": overall, "endpoints": endpoints, "timeline": timeline, "spikes": spikes,
            "error_clusters": error_clusters, "resources": res, "resource_timeline": res_timeline,
            "regressions": regressions, "variance": variance, "improvements": improvements,
            "verdict": "attention" if reasons else "healthy", "reasons": reasons}


def needs_investigation(analysis: dict) -> bool:
    """True when there is something worth spending LLM tokens on."""
    return analysis["verdict"] != "healthy" or bool(analysis["error_clusters"])


def evidence_bundle(analysis: dict, git_info: dict, log_summary: dict, known: dict) -> dict:
    """The only thing the correlation agent sees up-front: small, structured, no raw samples."""
    o = analysis["overall"]
    return {
        "commit": {"sha": git_info["short"], "subject": git_info["subject"], "changed_files": git_info["changed_files"][:15],
                   "commits_since_last_run": git_info["commits_since"][:8]},
        "load": {k: o[k] for k in ("requests", "rps", "error_rate", "p50", "p95", "p99", "max", "duration_s")},
        "slowest_endpoints": [{k: e[k] for k in ("name", "count", "p50", "p95", "p99", "error_rate", "baseline_p95")}
                              for e in analysis["endpoints"][:5]],
        "spikes": [{k: s[k] for k in ("start_rel", "end_rel", "peak_p95", "median_p95", "phase", "top_endpoints", "errors",
                                      "cpu_avg", "java_cpu_avg")} for s in analysis["spikes"][:4]],
        "error_clusters": analysis["error_clusters"][:5],
        "regressions": analysis["regressions"][:5],
        "slower_on_same_commit": analysis.get("variance", [])[:5],
        "resources": {k: analysis["resources"].get(k) for k in ("cpu_avg", "cpu_max", "cpu_saturated_s", "java_cpu_avg",
                                                                 "java_rss_max_mb", "java_rss_growth_mb_per_min",
                                                                 "net_sent_mb", "net_recv_mb", "locust_cpu_avg")},
        "logs": log_summary,
        "already_known_issues": [v["title"] for v in list(known.values())[:8]],
    }
