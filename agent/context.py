"""Run context: output folders, the step trace shown in the report, and token accounting."""
from __future__ import annotations

import json
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class RunContext:
    def __init__(self, cfg: dict, root: Path, run_id: str | None = None):
        self.cfg = cfg
        self.root = Path(root)
        self.started = time.time()
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.out = self.root / "out" / self.run_id
        self.out.mkdir(parents=True, exist_ok=True)
        self.events: list[dict] = []
        self.tokens: dict[str, dict] = {}        # step -> {prompt, completion, calls}
        self.data: dict = {}                     # results shared between steps
        self.verdict = "healthy"                 # healthy | attention | failed

    # ---- tracing -------------------------------------------------------------------------
    @contextmanager
    def step(self, key: str, title: str, kind: str = "code"):
        """Record a pipeline step. kind: 'code' (deterministic) or 'agent' (LLM involved)."""
        ev = {"key": key, "title": title, "kind": kind, "status": "running", "start": time.time(), "detail": ""}
        self.events.append(ev)
        print(f"\n=== [{key}] {title}", flush=True)
        try:
            yield ev
            if ev["status"] == "running":
                ev["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            ev["status"] = "failed"
            ev["detail"] = f"{type(exc).__name__}: {exc}"
            ev["traceback"] = traceback.format_exc(limit=6)
            raise
        finally:
            ev["duration_s"] = round(time.time() - ev["start"], 2)
            print(f"    -> {ev['status']} in {ev['duration_s']}s {ev['detail']}".rstrip(), flush=True)

    def skip(self, key: str, title: str, why: str, kind: str = "code"):
        self.events.append({"key": key, "title": title, "kind": kind, "status": "skipped", "start": time.time(),
                            "duration_s": 0, "detail": why})
        print(f"\n=== [{key}] {title}\n    -> skipped: {why}", flush=True)

    # ---- tokens --------------------------------------------------------------------------
    def add_tokens(self, step: str, prompt: int, completion: int):
        t = self.tokens.setdefault(step, {"prompt": 0, "completion": 0, "calls": 0})
        t["prompt"] += int(prompt or 0)
        t["completion"] += int(completion or 0)
        t["calls"] += 1

    def total_tokens(self) -> int:
        return sum(t["prompt"] + t["completion"] for t in self.tokens.values())

    # ---- persistence -----------------------------------------------------------------------
    def trace(self) -> list[dict]:
        out = []
        for e in self.events:
            item = {k: e[k] for k in ("key", "title", "kind", "status", "detail", "duration_s") if k in e}
            out.append(item)
        return out

    def write_trace(self):
        (self.out / "trace.json").write_text(json.dumps(self.trace(), indent=2), encoding="utf-8")
