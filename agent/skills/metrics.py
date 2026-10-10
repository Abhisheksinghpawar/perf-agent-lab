"""Skill 5b - infrastructure metrics: CPU, memory, network and the Tomcat JVM, sampled once per second during the load test."""
from __future__ import annotations

import csv
import threading
import time
from pathlib import Path

import psutil

FIELDS = ["ts", "cpu_pct", "mem_pct", "net_sent_kbs", "net_recv_kbs", "java_cpu_pct", "java_rss_mb",
          "java_threads", "locust_cpu_pct"]


class Sampler(threading.Thread):
    def __init__(self, out_csv: Path, java_getter, interval: float = 1.0):
        super().__init__(daemon=True)
        self.out_csv = out_csv
        self.java_getter = java_getter
        self.interval = interval
        self._halt = threading.Event()
        self.rows: list[dict] = []

    def stop(self):
        self._halt.set()
        self.join(timeout=5)

    def run(self):
        java = self.java_getter()
        me = psutil.Process()
        psutil.cpu_percent(None)
        if java:
            java.cpu_percent(None)
        net0, t0 = psutil.net_io_counters(), time.time()
        locust_procs: dict[int, psutil.Process] = {}
        while not self._halt.wait(self.interval):
            now = time.time()
            net = psutil.net_io_counters()
            dt = max(now - t0, 1e-6)
            row = {
                "ts": round(now, 2),
                "cpu_pct": psutil.cpu_percent(None),
                "mem_pct": psutil.virtual_memory().percent,
                "net_sent_kbs": round((net.bytes_sent - net0.bytes_sent) / 1024 / dt, 1),
                "net_recv_kbs": round((net.bytes_recv - net0.bytes_recv) / 1024 / dt, 1),
                "java_cpu_pct": 0.0, "java_rss_mb": 0.0, "java_threads": 0, "locust_cpu_pct": 0.0,
            }
            net0, t0 = net, now
            try:
                if java and java.is_running():
                    row["java_cpu_pct"] = round(java.cpu_percent(None) / psutil.cpu_count(), 1)   # normalise to whole-machine %
                    row["java_rss_mb"] = round(java.memory_info().rss / 1024 / 1024, 1)
                    row["java_threads"] = java.num_threads()
                for child in me.children(recursive=True):          # the locust subprocess(es)
                    if child.pid not in locust_procs:
                        locust_procs[child.pid] = child
                        child.cpu_percent(None)
                total = 0.0
                for pid, p in list(locust_procs.items()):
                    try:
                        total += p.cpu_percent(None)
                    except psutil.Error:
                        locust_procs.pop(pid, None)
                row["locust_cpu_pct"] = round(total / psutil.cpu_count(), 1)
            except psutil.Error:
                pass
            self.rows.append(row)
        self._flush()

    def _flush(self):
        with self.out_csv.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(self.rows)


def read_metrics(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        rows = []
        for r in csv.DictReader(fh):
            rows.append({k: (float(v) if k != "java_threads" else int(float(v))) for k, v in r.items()})
        return rows
