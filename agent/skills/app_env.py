"""Skill 2 - build the system under test, start it, wait until healthy, and tear it down again.

Two runners: "maven-cargo" (built-in: Maven build + Tomcat, used for JPetStore) and "command" (anything: your own
build_cmd / start_cmd from the config, run in the app folder through the shell).
"""
from __future__ import annotations

import os
import re
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import psutil
import requests

# Tomcat/cargo needs these on modern JDKs; GC events go to stdout so they land in the same app.log we analyse later.
JVM_ARGS = ("--add-opens=java.base/java.lang=ALL-UNNAMED --add-opens=java.rmi/sun.rmi.transport=ALL-UNNAMED "
            "-Xlog:gc:stdout:time,uptime")


def build_hint(build_log_tail: str) -> str:
    """One-line, human explanation for the build failures people actually hit."""
    if "RequireJavaVersion" in build_log_tail:
        m = re.search(r"is version (\S+) which is not in the allowed range (.+)", build_log_tail)
        return (f"the installed JDK ({m.group(1)}) is not accepted by the target project (allowed: {m.group(2).strip()}). Install a matching JDK."
                if m else "the installed JDK is not accepted by the target project. Install a matching JDK.")
    if "UnknownHostException" in build_log_tail or "Could not transfer artifact" in build_log_tail:
        return "Maven could not download dependencies - check your network/proxy."
    if "AccessDeniedException" in build_log_tail:
        return "Maven could not write to its cache - another Maven run is active, or set PERF_MAVEN_REPO to a private directory."
    return "see the build output below."


SHELLS = {"sh", "bash", "dash", "zsh", "cmd.exe", "powershell.exe", "pwsh.exe", "pwsh", "powershell"}


class App:
    def __init__(self, cfg: dict, workdir: str, out: Path, maven_repo: Path | None = None):
        self.cfg = cfg
        self.workdir = Path(workdir)
        self.out = out
        self.proc: subprocess.Popen | None = None
        self.log_path = out / "app.log"
        self.maven_repo = maven_repo
        self.mvn = str(self.workdir / ("mvnw.cmd" if os.name == "nt" else "mvnw"))
        self.t = cfg["target"]
        self.runner = self.t.get("runner", "command")
        if self.runner not in ("maven-cargo", "command"):
            raise RuntimeError(f"unknown [target].runner '{self.runner}' (use 'command' or 'maven-cargo')")

    def _port(self) -> int:
        return urlparse(self.t["base_url"]).port or 80

    def _expand(self, cmd: str) -> str:
        """{python} = the interpreter running the pipeline, {port} = the port of base_url."""
        py = f'"{sys.executable}"' if " " in sys.executable else sys.executable
        return cmd.replace("{python}", py).replace("{port}", str(self._port()))

    def _mvn_args(self) -> list[str]:
        # skip source-mutating / slow code-quality plugins: we only need a WAR, and the checkout must stay pristine
        args = ["-B", "-ntp", "-Dformatter.skip=true", "-Dimpsort.skip=true", "-Drewrite.skip=true", "-Djacoco.skip=true",
                "-Dlicense.skip=true", "-Dcheckstyle.skip=true", "-Dmaven.javadoc.skip=true"]
        if self.maven_repo:
            args.append(f"-Dmaven.repo.local={self.maven_repo}")
        return args

    def build(self) -> float:
        t0 = time.time()
        if self.runner == "command" and not self.t.get("build_cmd"):
            return 0.0                                   # nothing to build (interpreted app, prebuilt image, ...)
        log = (self.out / "build.log").open("w", encoding="utf-8")
        timeout = int(self.t["build_timeout_s"])
        try:
            if self.runner == "maven-cargo":
                if os.name != "nt":
                    os.chmod(self.mvn, 0o755)
                r = subprocess.run([self.mvn, *self._mvn_args(), "clean", "package", "-DskipTests"], cwd=self.workdir,
                                   stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
            else:
                r = subprocess.run(self._expand(self.t["build_cmd"]), shell=True, cwd=self.workdir,
                                   stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        finally:
            log.close()
        if r.returncode != 0:
            tail = (self.out / "build.log").read_text(encoding="utf-8", errors="replace")[-1500:]
            raise RuntimeError(f"build failed (exit {r.returncode}): {build_hint(tail)}\n{tail}")
        return round(time.time() - t0, 1)

    def start(self) -> float:
        t0 = time.time()
        port = self._port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"port {port} is already in use - stop the other process (an earlier run?) or change [target].base_url")
        if self.runner == "command" and not self.t.get("start_cmd"):
            raise RuntimeError("[target].start_cmd is empty - set the command that starts your app (see README, 'Use it on your own app')")
        logf = self.log_path.open("w", encoding="utf-8")
        if self.runner == "maven-cargo":
            cmd = [self.mvn, *self._mvn_args(), "cargo:run", "-P", "tomcat9", f"-Dcargo.jvmargs={JVM_ARGS}"]
            self.proc = subprocess.Popen(cmd, cwd=self.workdir, stdout=logf, stderr=subprocess.STDOUT)
        else:
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            self.proc = subprocess.Popen(self._expand(self.t["start_cmd"]), shell=True, cwd=self.workdir, env=env,
                                         stdout=logf, stderr=subprocess.STDOUT)
        logf.close()                                      # the child keeps its own copy of the handle
        self.wait_healthy()
        return round(time.time() - t0, 1)

    def wait_healthy(self):
        t = self.cfg["target"]
        url = t["base_url"] + t["health_path"]
        deadline = time.time() + int(t["startup_timeout_s"])
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                tail = self.log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
                raise RuntimeError(f"app exited during startup (code {self.proc.returncode}):\n{tail}")
            try:
                if requests.get(url, timeout=3).status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(2)
        raise TimeoutError(f"app not healthy at {url} after {t['startup_timeout_s']}s")

    def app_process(self) -> psutil.Process | None:
        """The process whose CPU/memory we sample: the Tomcat JVM (maven-cargo) or, for a command, the process matching
        [target].process_match (default: the largest child of the shell we started)."""
        if not self.proc:
            return None
        try:
            root = psutil.Process(self.proc.pid)
            kids = root.children(recursive=True)
        except psutil.NoSuchProcess:
            return None
        if self.runner == "command":
            cands = [p for p in [root, *kids] if (p.name() or "").lower() not in SHELLS]
            match = (self.t.get("process_match") or "").lower()
            if match:
                cands = [p for p in cands if match in " ".join(p.cmdline()).lower()]
            return max(cands, key=lambda p: p.memory_info().rss, default=None)
        javas = [p for p in kids if "java" in (p.name() or "").lower()]
        for p in javas:
            try:
                if "catalina" in " ".join(p.cmdline()).lower():
                    return p
            except psutil.Error:
                continue
        return max(javas, key=lambda p: p.memory_info().rss, default=None)

    java_process = app_process                           # old name

    def stop(self):
        if not self.proc:
            return
        try:
            parent = psutil.Process(self.proc.pid)
            procs = parent.children(recursive=True) + [parent]
        except psutil.NoSuchProcess:
            procs = []
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _gone, alive = psutil.wait_procs(procs, timeout=15)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
        self.proc = None
        lf = self.t.get("log_file")
        if lf and (self.workdir / lf).exists():          # app logs to a file: make it part of the log we analyse
            with self.log_path.open("a", encoding="utf-8") as out:
                out.write("\n" + (self.workdir / lf).read_text(encoding="utf-8", errors="replace"))
