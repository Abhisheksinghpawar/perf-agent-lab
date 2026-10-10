"""Shared helpers for running Locust headless and reading its per-request log."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (pct in 0..100); 0.0 for an empty list."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def run_locust(locustfile: Path, host: str, users: int, spawn_rate: float, duration_s: int, out_dir: Path,
               tag: str, env_extra: dict | None = None) -> dict:
    """Run Locust headless for `duration_s` seconds. Per-request records go to <tag>.jsonl (see locustfile)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = out_dir / f"{tag}.jsonl"
    jsonl.unlink(missing_ok=True)
    log = out_dir / f"{tag}.locust.log"
    env = {**os.environ, "PERF_JSONL": str(jsonl), "PYTHONUNBUFFERED": "1", **(env_extra or {})}
    cmd = [sys.executable, "-m", "locust", "-f", str(locustfile), "--headless", "--host", host,
           "-u", str(users), "-r", str(spawn_rate), "-t", f"{int(duration_s)}s",
           "--csv", str(out_dir / tag), "--exit-code-on-error", "0", "--only-summary"]
    t0 = time.time()
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, env=env, timeout=int(duration_s) + 120)
    return {"returncode": proc.returncode, "jsonl": str(jsonl), "log": str(log),
            "wall_s": round(time.time() - t0, 1), "started": t0}


def read_records(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    recs = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue                      # a half-written last line is expected when locust is killed
    return recs
