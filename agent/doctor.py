"""`python -m agent doctor` - preflight checks with actionable fixes, so a fresh clone fails early and clearly, not 5 minutes in."""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import socket
import subprocess
import sys
from urllib.parse import urlparse

import argparse

from .config import ROOT, load_config, resolve

OK, FAIL, WARN = "OK  ", "FAIL", "WARN"


def _java_major() -> int | None:
    try:
        out = subprocess.run(["java", "-version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', out.stderr + out.stdout)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2)) if major == 1 and m.group(2) else major        # "1.8.0" -> 8


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _reachable(url: str) -> bool:
    try:
        import requests
        return requests.head(url, timeout=8, allow_redirects=True).status_code < 500
    except Exception:  # noqa: BLE001
        return False


def run_checks(cfg: dict) -> list[tuple[str, str, str, str]]:
    """[(status, name, detail, fix)]"""
    rows = []

    def add(ok, name, detail, fix="", level=FAIL):
        rows.append((OK if ok else level, name, detail, "" if ok else fix))

    add(sys.version_info >= (3, 11), "Python >= 3.11", sys.version.split()[0], "Install Python 3.11+ (the config loader uses tomllib).")
    add(bool(shutil.which("git")), "git", shutil.which("git") or "not found", "Install git: https://git-scm.com/downloads")
    t = cfg["target"]
    maven = t.get("runner") == "maven-cargo"
    if maven:
        jv = _java_major()
        add(jv is not None and jv >= 21, "Java >= 21 (JPetStore's build requires it)", f"java {jv}" if jv else "not found",
            "Install JDK 21 (Temurin: https://adoptium.net) and make sure `java` is on PATH. JPetStore's Maven enforcer rejects JDK 17.")
    else:
        add(bool(t.get("start_cmd")), "[target].start_cmd", t.get("start_cmd") or "empty",
            "Set the command that starts your app, e.g. start_cmd = \"{python} -u app.py --port {port}\" (see README, 'Use it on your own app').")
        if t.get("local_path"):
            lp = resolve(t["local_path"])
            add(lp.is_dir(), "[target].local_path", str(lp) if lp.is_dir() else f"{lp} is not a folder", "Point local_path at the folder that contains your app.")
        else:
            add(bool(t.get("repo_url")), "[target].repo_url", t.get("repo_url") or "empty",
                "Set repo_url (a git URL to clone) or local_path (a folder you already have).")
    lf = resolve(t["locustfile"])
    add(lf.exists(), "Locust script", str(lf) if lf.exists() else f"{lf} not found",
        "Set [target].locustfile to your Locust file (copy locust/locustfile.py or examples/python-shop/locustfile.py as a starting point).")
    for mod in ("locust", "psutil", "requests"):
        # find_spec, not import: importing locust monkey-patches the standard library (gevent), which breaks HTTPS afterwards
        found = importlib.util.find_spec(mod) is not None
        add(found, f"python package: {mod}", "installed" if found else "missing", "Run: pip install -r requirements.txt")
    port = urlparse(cfg["target"]["base_url"]).port or 80
    add(_port_free(port), f"port {port} is free", "free" if _port_free(port) else "in use",
        f"Something is already listening on {port}. Stop it, or change [target].base_url in config.toml.")
    if t.get("repo_url") and not t.get("local_path"):
        host = urlparse(t["repo_url"]).netloc or "github.com"
        add(_reachable(f"https://{host}"), f"network: {host}", "reachable", "The target repo is cloned from there; check your connection/proxy.")
    if maven:
        add(_reachable("https://repo.maven.apache.org/maven2/"), "network: Maven Central", "reachable", "Maven downloads Tomcat and dependencies on the first build.")
    free_gb = shutil.disk_usage(ROOT).free / 1024 ** 3
    add(free_gb >= 2, "disk space >= 2 GB", f"{free_gb:.1f} GB free", "Free up some disk space (Maven cache + Tomcat).", WARN)
    c = cfg["llm"]
    has_key = bool(os.environ.get(c["api_key_env"]) or os.environ.get("LLM_API_KEY"))
    add(has_key, "LLM API key", f"{c['api_key_env']} is set -> agents enabled" if has_key else "not set -> deterministic mode (still works)",
        f"Optional: export {c['api_key_env']}=... to enable the self-heal and correlation agents (free Groq key: console.groq.com). "
        f"Or try `python -m agent demo` for a key-less scripted run.", WARN)
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent doctor")
    ap.add_argument("--config", default=None, help="check this config instead of config.toml")
    cfg = load_config(ap.parse_args(argv).config)
    rows = run_checks(cfg)
    print("\nperf-agent-lab doctor\n" + "-" * 60)
    for status, name, detail, fix in rows:
        print(f"[{status}] {name:<34} {detail}")
        if fix:
            print(f"       -> {fix}")
    hard = [r for r in rows if r[0] == FAIL]
    print("-" * 60)
    if hard:
        print(f"{len(hard)} blocking problem(s). Fix them and run `python -m agent doctor` again.")
        return 1
    print("Ready. Next: `python -m agent demo` (no key needed) or `python -m agent run --force`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
