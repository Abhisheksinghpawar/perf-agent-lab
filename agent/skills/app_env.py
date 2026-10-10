"""Skill 2 - build the system under test, start it, wait until healthy, and tear it down again."""
from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import psutil
import requests

# Tomcat/cargo needs these on modern JDKs; GC events go to stdout so they land in the same app.log we analyse later.
JVM_ARGS = ("--add-opens=java.base/java.lang=ALL-UNNAMED --add-opens=java.rmi/sun.rmi.transport=ALL-UNNAMED "
            "-Xlog:gc:stdout:time,uptime")


class App:
    def __init__(self, cfg: dict, workdir: str, out: Path, maven_repo: Path | None = None):
        self.cfg = cfg
        self.workdir = Path(workdir)
        self.out = out
        self.proc: subprocess.Popen | None = None
        self.log_path = out / "app.log"
        self.maven_repo = maven_repo
        self.mvn = str(self.workdir / ("mvnw.cmd" if os.name == "nt" else "mvnw"))

    def _mvn_args(self) -> list[str]:
        # skip source-mutating / slow code-quality plugins: we only need a WAR, and the checkout must stay pristine
        args = ["-B", "-ntp", "-Dformatter.skip=true", "-Dimpsort.skip=true", "-Drewrite.skip=true", "-Djacoco.skip=true",
                "-Dlicense.skip=true", "-Dcheckstyle.skip=true", "-Dmaven.javadoc.skip=true"]
        if self.maven_repo:
            args.append(f"-Dmaven.repo.local={self.maven_repo}")
        return args

    def build(self) -> float:
        t0 = time.time()
        if os.name != "nt":
            os.chmod(self.mvn, 0o755)
        log = (self.out / "build.log").open("w", encoding="utf-8")
        r = subprocess.run([self.mvn, *self._mvn_args(), "clean", "package", "-DskipTests"], cwd=self.workdir,
                           stdout=log, stderr=subprocess.STDOUT, timeout=int(self.cfg["target"]["build_timeout_s"]))
        log.close()
        if r.returncode != 0:
            tail = (self.out / "build.log").read_text(encoding="utf-8", errors="replace")[-1500:]
            raise RuntimeError(f"build failed (exit {r.returncode}):\n{tail}")
        return round(time.time() - t0, 1)

    def start(self) -> float:
        t0 = time.time()
        port = urlparse(self.cfg["target"]["base_url"]).port or 80
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"port {port} is already in use - stop the other process (an earlier run?) or change [target].base_url")
        logf = self.log_path.open("w", encoding="utf-8")
        cmd = [self.mvn, *self._mvn_args(), "cargo:run", "-P", "tomcat9", f"-Dcargo.jvmargs={JVM_ARGS}"]
        self.proc = subprocess.Popen(cmd, cwd=self.workdir, stdout=logf, stderr=subprocess.STDOUT)
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

    def java_process(self) -> psutil.Process | None:
        """The Tomcat JVM (a descendant of the maven process) - this is what we sample for CPU/memory."""
        if not self.proc:
            return None
        try:
            kids = psutil.Process(self.proc.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            return None
        javas = [p for p in kids if "java" in (p.name() or "").lower()]
        for p in javas:
            try:
                if "catalina" in " ".join(p.cmdline()).lower():
                    return p
            except psutil.Error:
                continue
        return max(javas, key=lambda p: p.memory_info().rss, default=None)

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
