"""Skill 9b - publish: archive the run, update history, and build the static site (GitHub Pages) with a small latest.json
that a portfolio page can fetch."""
from __future__ import annotations

import gzip
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


def _top_finding(findings: list[dict]) -> dict | None:
    """The most important finding in a form small enough for latest.json (a portfolio page renders it)."""
    if not findings:
        return None
    f = findings[0]
    return {"title": f["title"], "severity": f.get("severity", "info"), "category": f.get("category", ""),
            "endpoint": f.get("endpoint"), "evidence": [str(x)[:170] for x in f.get("evidence", [])[:2]],
            "suspected_cause": str(f.get("suspected_cause", ""))[:240],
            "code_refs": [{"file": r["file"], "line": r.get("line", 1), "note": str(r.get("note", ""))[:90]} for r in f.get("code_refs", [])[:2] if r.get("file")],
            "recommendation": str(f.get("recommendation", ""))[:240]}


def summary_of(R: dict) -> dict:
    """The compact record used for latest.json and history.json."""
    an, lg = R.get("analysis") or {}, R.get("logs") or {}
    o, res, gc = an.get("overall", {}), an.get("resources", {}) or {}, lg.get("gc") or {}
    sm, heal = R["smoke"], R.get("heal") or {}
    return {
        "run_id": R["run_id"], "finished_at": R["finished_at"], "verdict": R["verdict"], "duration_s": R["duration_s"],
        "commit": {k: R["commit"][k] for k in ("sha", "short", "subject", "date", "repo")},
        "smoke": {"passed": sm["passed"], "requests": sm["requests"], "healed": bool(heal.get("healed")),
                  "heal_attempts": heal.get("attempts", 0)},
        "load": {"users": R["load"]["users"], "duration_s": R["load"]["duration_s"], "requests": o.get("requests", 0),
                 "rps": o.get("rps", 0), "error_rate": o.get("error_rate", 0), "p50": o.get("p50", 0), "p95": o.get("p95", 0),
                 "p99": o.get("p99", 0), "failures": o.get("failures", 0)},
        "analysis": {"spikes": len(an.get("spikes") or []), "error_clusters": len(an.get("error_clusters") or []),
                     "regressions": len(an.get("regressions") or [])},
        "logs": {"exception_types": len(lg.get("exceptions") or []), "gc_count": gc.get("count", 0),
                 "gc_max_pause_ms": gc.get("max_pause_ms", 0)},
        "resources": {k: res.get(k) for k in ("cpu_avg", "cpu_max", "java_cpu_avg", "java_rss_max_mb", "net_sent_mb", "net_recv_mb")},
        "findings": [{"title": f["title"], "severity": f.get("severity", "info")} for f in R["findings"][:5]],
        "top_finding": _top_finding(R["findings"]),
        "improvements": [{"priority": i.get("priority", n), "action": str(i.get("action", ""))[:240], "effort": i.get("effort", "")}
                         for n, i in enumerate(R["improvements"][:3], 1)],
        "top_improvement": (R["improvements"][0]["action"] if R["improvements"] else None),
        "summary": R["summary"],
        "tokens": {"total": R["tokens"]["total"], "skipped_llm_steps": [s["step"] for s in R["tokens"]["skipped"]]},
        "llm": R.get("llm_mode", "off"),
    }


def publish(root: Path, cfg: dict, R: dict, html: str, out: Path) -> dict:
    p = cfg["publish"]
    reports = root / p["reports_dir"]
    run_dir = reports / "runs" / R["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.html").write_text(html, encoding="utf-8")
    (run_dir / "report.json").write_text(json.dumps(R, indent=2, default=str), encoding="utf-8")
    for name in ("trace.json", "load.locust.log"):
        if (out / name).exists():
            shutil.copy(out / name, run_dir / name)
    for name in ("load.jsonl", "metrics.csv"):                       # raw data, gzipped
        src = out / name
        if src.exists():
            with src.open("rb") as fi, gzip.open(run_dir / (name + ".gz"), "wb") as fo:
                shutil.copyfileobj(fi, fo)
    lf = out / "locust" / "locustfile.py"
    if lf.exists():
        shutil.copy(lf, run_dir / "locustfile.used.py")

    summary = summary_of(R)
    hist_path = reports / "history.json"
    history = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else []
    history = [h for h in history if h["run_id"] != R["run_id"]] + [summary]
    history = history[-int(p["keep_runs"]):]
    hist_path.write_text(json.dumps(history, indent=2), encoding="utf-8")

    keep = {h["run_id"] for h in history}                              # prune old run folders
    for d in (reports / "runs").iterdir():
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)

    # ---- static site for GitHub Pages ---------------------------------------------------------------
    site = root / p["site_dir"]
    if site.exists():
        shutil.rmtree(site)
    (site / "runs").mkdir(parents=True)
    (site / "index.html").write_text(html, encoding="utf-8")
    (site / "latest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (site / "history.json").write_text(json.dumps(history, indent=2), encoding="utf-8")
    for h in history[-10:]:
        src = reports / "runs" / h["run_id"] / "report.html"
        if src.exists():
            (site / "runs" / f"{h['run_id']}.html").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")
    return summary


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
