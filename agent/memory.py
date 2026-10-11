"""Persistent memory between runs: this is what makes later runs cheap.

memory/state.json          last tested commit  -> unchanged code = whole run skipped (zero compute, zero tokens)
memory/baseline.json       per-endpoint p95 from recent healthy runs (+ commit) -> later runs only analyse the *delta*
memory/known_issues.json   fingerprints of past findings -> recurring issues are labelled, not re-investigated
memory/locustfile.good.py  last locustfile that passed the smoke test -> self-healing only runs when it stops working
memory/token_stats.json    tokens each LLM step used last time -> lets the report estimate what a skipped step saved
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path


class Memory:
    def __init__(self, root: Path, sub: str = "memory"):
        self.dir = Path(root) / sub
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- generic json helpers ---------------------------------------------------------------
    def _read(self, name: str, default):
        p = self.dir / name
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return default
        return default

    def _write(self, name: str, data):
        (self.dir / name).write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    # ---- last tested commit -----------------------------------------------------------------
    def last_sha(self) -> str | None:
        return self._read("state.json", {}).get("last_tested_sha")

    def save_state(self, sha: str, run_id: str, verdict: str):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._write("state.json", {"last_tested_sha": sha, "last_run_id": run_id, "verdict": verdict, "updated": now,
                                   "last_checked": now})

    def touch_checked(self):
        """Heartbeat for nights when the target repo is unchanged and the run is skipped: proves the schedule is alive."""
        s = self._read("state.json", {})
        s["last_checked"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._write("state.json", s)

    # ---- baseline -----------------------------------------------------------------------------
    def baseline(self, users: int | None = None) -> dict:
        """{endpoint: {"p95": ms, "n": samples}} - median of recent runs *at the same load level* (latency at 25 users says
        nothing about latency at 250)."""
        hist = [r for r in self._read("baseline.json", {}).get("runs", []) if users is None or r.get("users") == users]
        by_ep: dict[str, list[float]] = {}
        for run in hist:
            for ep, p95 in run.get("p95", {}).items():
                by_ep.setdefault(ep, []).append(p95)
        return {ep: {"p95": round(statistics.median(v), 1), "n": len(v)} for ep, v in by_ep.items()}

    def baseline_commits(self, users: int | None = None) -> set:
        """Commits the baseline at this load level was recorded on (None = recorded before commits were tracked)."""
        return {r.get("sha") for r in self._read("baseline.json", {}).get("runs", []) if users is None or r.get("users") == users}

    def add_baseline_run(self, run_id: str, endpoint_p95: dict[str, float], keep: int = 10, users: int | None = None,
                         sha: str | None = None):
        data = self._read("baseline.json", {"runs": []})
        data["runs"] = (data.get("runs", []) + [{"run_id": run_id, "users": users, "sha": sha, "p95": endpoint_p95}])[-keep:]
        self._write("baseline.json", data)

    # ---- known issues -------------------------------------------------------------------------
    @staticmethod
    def fingerprint(finding: dict) -> str:
        # numbers in titles change every run ("126 failed requests", "5421% slower"): normalise them away so recurrence is detected
        title = re.sub(r"\d+(?:[.,]\d+)?%?", "#", str(finding.get("title", "")).lower())
        key = "|".join([str(finding.get("endpoint", "")).lower().strip(), str(finding.get("category", "")).lower().strip(), title.strip()])
        return hashlib.sha1(key.encode()).hexdigest()[:10]

    def known_issues(self) -> dict:
        return self._read("known_issues.json", {})

    def record_findings(self, findings: list[dict], run_id: str) -> list[dict]:
        """Annotate findings with recurrence info and persist them."""
        issues = self.known_issues()
        for f in findings:
            fp = self.fingerprint(f)
            prev = issues.get(fp)
            f["fingerprint"] = fp
            f["recurring"] = bool(prev)
            f["seen_count"] = (prev["seen_count"] if prev else 0) + 1
            issues[fp] = {"title": f.get("title"), "endpoint": f.get("endpoint"), "category": f.get("category"),
                          "first_seen": prev["first_seen"] if prev else run_id, "last_seen": run_id,
                          "seen_count": f["seen_count"]}
        self._write("known_issues.json", issues)
        return findings

    # ---- known-good locustfile ------------------------------------------------------------------
    def good_locustfile(self) -> str | None:
        p = self.dir / "locustfile.good.py"
        return p.read_text(encoding="utf-8") if p.exists() else None

    def save_good_locustfile(self, source: str):
        (self.dir / "locustfile.good.py").write_text(source, encoding="utf-8")

    # ---- token stats ----------------------------------------------------------------------------
    def token_stats(self) -> dict:
        return self._read("token_stats.json", {})

    def update_token_stats(self, tokens: dict[str, dict]):
        stats = self.token_stats()
        for step, t in tokens.items():
            stats[step] = t["prompt"] + t["completion"]
        self._write("token_stats.json", stats)
