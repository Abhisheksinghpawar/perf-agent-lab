"""Skill 3 - sanity (smoke) test: 1 user for ~20 s. Proves the Locust script still matches the app before we spend a minute on load."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .locust_runner import percentile, read_records, run_locust


def evaluate(records: list[dict], smoke_cfg: dict) -> dict:
    total = len(records)
    fails = [r for r in records if not r.get("ok")]
    rate = (len(fails) / total) if total else 1.0
    p95 = percentile([r["rt"] for r in records], 95)
    reasons = []
    if total < int(smoke_cfg["min_requests"]):
        reasons.append(f"only {total} requests completed (need >= {smoke_cfg['min_requests']})")
    if rate > float(smoke_cfg["max_error_rate"]):
        reasons.append(f"error rate {rate:.1%} (> {float(smoke_cfg['max_error_rate']):.1%})")
    if p95 > float(smoke_cfg["p95_ms_limit"]):
        reasons.append(f"p95 {p95:.0f} ms (> {smoke_cfg['p95_ms_limit']} ms)")
    kinds = Counter((r.get("name", "?"), str(r.get("status", "")), (r.get("err") or "")[:120]) for r in fails)
    top = [{"endpoint": k[0], "status": k[1], "error": k[2], "count": c} for k, c in kinds.most_common(6)]
    return {"passed": not reasons, "requests": total, "failures": len(fails), "error_rate": round(rate, 4),
            "p95_ms": round(p95, 1), "reasons": reasons, "top_failures": top}


def run_smoke(cfg: dict, locustfile: Path, out: Path, tag: str = "smoke") -> dict:
    s = cfg["smoke"]
    res = run_locust(locustfile, cfg["target"]["base_url"], int(s["users"]), max(1, int(s["users"])),
                     int(s["duration_s"]), out, tag)
    result = evaluate(read_records(res["jsonl"]), s)
    result["locustfile"] = str(locustfile)
    result["wall_s"] = res["wall_s"]
    return result
