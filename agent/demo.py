"""`python -m agent demo` - the whole pipeline, including BOTH agent steps, with no API key.

It starts a scripted OpenAI-compatible server (agent/demo_llm.py) and runs with --demo-heal, so you can watch a stale
Locust script fail the sanity test, get repaired by the agent loop, and the correlation agent trace a real error to code.
The run is labelled "scripted demo" in the report (it is the agent *plumbing* that is demonstrated, not a model's skill).
State goes to ./.demo so your real memory/ and reports/ stay clean.
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

from . import demo_llm, run
from .config import ROOT


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv: list[str] | None = None) -> int:
    port = _free_port()
    server = demo_llm.start_in_thread(port, str(ROOT / "locust" / "locustfile.py"))
    os.environ.update({"LLM_BASE_URL": f"http://127.0.0.1:{port}/v1", "LLM_MODEL": "scripted-demo", "GROQ_API_KEY": "demo-not-a-real-key"})
    os.environ.setdefault("PERF_HOME", str(ROOT / ".demo"))
    print(f"scripted demo LLM on port {port}; state in {os.environ['PERF_HOME']}\n")
    try:
        code = run.main(["--demo-heal", "--force", "--set", "load.users=250", "--set", "load.spawn_rate=50", *(argv or [])])
    finally:
        server.shutdown()
    home = Path(os.environ["PERF_HOME"])
    reports = sorted((home / "reports" / "runs").glob("*/report.html"))
    if reports:
        print(f"\nOpen the report: {reports[-1]}")
    return code


if __name__ == "__main__":
    sys.exit(main())
