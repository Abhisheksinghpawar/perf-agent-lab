"""Skill 5 - the real load test (default: 250 users for 60 seconds, see config.toml) with infrastructure sampling running alongside."""
from __future__ import annotations

from pathlib import Path

import psutil

from .locust_runner import read_records, run_locust
from .metrics import Sampler, read_metrics


def run_load(cfg: dict, locustfile: Path, out: Path, java_getter) -> dict:
    l = cfg["load"]
    sampler = Sampler(out / "metrics.csv", java_getter)
    sampler.start()
    try:
        res = run_locust(locustfile, cfg["target"]["base_url"], int(l["users"]), float(l["spawn_rate"]),
                         int(l["duration_s"]), out, "load")
    finally:
        sampler.stop()
    records = read_records(res["jsonl"])
    metrics = read_metrics(out / "metrics.csv")
    return {"records": records, "metrics": metrics, "started": res["started"], "wall_s": res["wall_s"],
            "users": int(l["users"]), "duration_s": int(l["duration_s"]), "cpu_count": psutil.cpu_count(),
            "mem_total_gb": round(psutil.virtual_memory().total / 1024 ** 3, 1)}
