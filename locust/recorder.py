"""Writes one JSON line per request (timestamp, endpoint name, response time, status, error) to $PERF_JSONL.

The pipeline analyses this file (percentiles, spikes, error clusters). Locustfiles must `import recorder` - the
self-healing agent is required to keep that line.
"""
import json
import os
import threading
import time

from locust import events

_path = os.environ.get("PERF_JSONL")
_lock = threading.Lock()
_fh = open(_path, "a", encoding="utf-8", buffering=1) if _path else None


@events.request.add_listener
def _record(request_type=None, name=None, response_time=0, response_length=0, response=None, exception=None,
            start_time=None, **_kwargs):
    if _fh is None:
        return
    rec = {
        "ts": round(start_time if start_time else time.time() - response_time / 1000.0, 3),
        "name": name,
        "method": request_type,
        "rt": round(float(response_time), 1),
        "ok": exception is None,
        "status": getattr(response, "status_code", 0) or 0,
        "err": str(exception)[:160] if exception else "",
        "bytes": int(response_length or 0),
    }
    with _lock:
        _fh.write(json.dumps(rec) + "\n")


@events.quitting.add_listener
def _close(**_kwargs):
    if _fh:
        _fh.flush()
