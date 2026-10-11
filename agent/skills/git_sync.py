"""Skill 1 - get the code of the system under test (clone a repo, or use a local folder) and decide whether anything changed."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

from ..config import resolve

# never descend into these when looking at source files
EXCLUDE_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "site-packages", "target", "build", "dist",
                ".gradle", ".idea", ".mypy_cache", ".pytest_cache", "vendor"}


def _git(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def _web_url(remote: str) -> str:
    """git@github.com:org/app.git / https://github.com/org/app.git -> https://github.com/org/app (for report links)."""
    remote = remote.strip()
    m = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", remote)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return remote.removesuffix(".git") if remote.startswith("http") else ""


def content_hash(folder: Path, limit: int = 20000) -> str:
    """Fingerprint of a folder that is not under git: names, sizes and modification times of its files."""
    h, n = hashlib.sha1(), 0
    for dirpath, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
        for f in sorted(files):
            p = Path(dirpath) / f
            try:
                st = p.stat()
            except OSError:
                continue
            h.update(f"{p.relative_to(folder).as_posix()}|{st.st_size}|{st.st_mtime_ns}\n".encode())
            n += 1
            if n >= limit:
                return h.hexdigest()
    return h.hexdigest()


def _info(sha: str, work: Path, **kw) -> dict:
    return {"sha": sha, "short": sha[:8], "subject": "", "author": "", "date": "", "repo": "", "branch": "", "subdir": "",
            "workdir": str(work), "previous_sha": None, "changed": True, "changed_files": [], "commits_since": [], **kw}


def _sync_local(t: dict, last_sha: str | None) -> dict:
    """Use a folder that already exists (a monorepo subfolder, a sibling checkout, ...). Nothing is cloned or modified."""
    work = resolve(t["local_path"]).resolve()
    if not work.is_dir():
        raise RuntimeError(f"[target].local_path '{t['local_path']}' is not a folder (resolved to {work})")
    top = _git(["rev-parse", "--show-toplevel"], work, check=False)
    if top:
        subdir = os.path.relpath(work, Path(top).resolve()).replace(os.sep, "/")
        subdir = "" if subdir == "." else subdir
        sha = _git(["log", "-1", "--format=%H", "--", "."], work, check=False)
        dirty = bool(_git(["status", "--porcelain", "--", "."], work, check=False))
        if not sha or dirty:                                   # untracked or edited: fingerprint the files so changes still re-run
            sha = (sha[:12] + "+" if sha else "local+") + content_hash(work)[:12]
        info = _info(sha, work, subdir=subdir, repo=_web_url(_git(["remote", "get-url", "origin"], work, check=False)),
                     branch=_git(["rev-parse", "--abbrev-ref", "HEAD"], work, check=False),
                     subject=_git(["log", "-1", "--pretty=%s", "--", "."], work, check=False) or "(uncommitted local changes)",
                     author=_git(["log", "-1", "--pretty=%an", "--", "."], work, check=False),
                     date=_git(["log", "-1", "--pretty=%cI", "--", "."], work, check=False))
        real = re.fullmatch(r"[0-9a-f]{40}", sha) is not None
        if last_sha and last_sha != sha and real and re.fullmatch(r"[0-9a-f]{40}", last_sha) \
                and _git(["cat-file", "-t", last_sha], work, check=False) == "commit":
            info["changed_files"] = _git(["diff", "--name-only", "--relative", last_sha, sha, "--", "."], work, check=False).splitlines()[:60]
            info["commits_since"] = _git(["log", "--pretty=%h %s", f"{last_sha}..{sha}", "--", "."], work, check=False).splitlines()[:15]
    else:
        info = _info("local+" + content_hash(work)[:12], work, subject="local folder (not under git)")
    info["previous_sha"], info["changed"] = last_sha, last_sha != info["sha"]
    return info


def sync(cfg: dict, root: Path, last_sha: str | None) -> dict:
    t = cfg["target"]
    if t.get("local_path"):
        return _sync_local(t, last_sha)
    if not t.get("repo_url"):
        raise RuntimeError("set [target].repo_url (a git URL to clone) or [target].local_path (a folder) in your config")
    work = (root / t["workdir"]).resolve()
    branch = t.get("branch") or "main"
    if (work / ".git").exists():
        _git(["fetch", "--depth", "200", "origin", branch], work)
        _git(["reset", "--hard", f"origin/{branch}"], work)
    else:
        work.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--depth", "200", "--branch", branch, t["repo_url"], str(work)])
    if t.get("ref"):                                         # reproducible run: test exactly this commit/tag
        _git(["fetch", "--unshallow", "--tags", "origin"], work, check=False)
        _git(["checkout", "--detach", t["ref"]], work)
    sha = _git(["rev-parse", "HEAD"], work)
    info = _info(sha, work, subject=_git(["log", "-1", "--pretty=%s"], work), author=_git(["log", "-1", "--pretty=%an"], work),
                 date=_git(["log", "-1", "--pretty=%cI"], work), repo=t["repo_url"].removesuffix(".git"), branch=branch,
                 previous_sha=last_sha, changed=last_sha != sha)
    if last_sha and last_sha != sha:
        # the shallow clone may not contain the previous commit; that is fine, we just lose the diff
        if _git(["cat-file", "-t", last_sha], work, check=False) == "commit":
            info["changed_files"] = _git(["diff", "--name-only", last_sha, sha], work).splitlines()[:60]
            info["commits_since"] = _git(["log", "--pretty=%h %s", f"{last_sha}..{sha}"], work).splitlines()[:15]
    return info


def git_log_for(workdir: str, path: str, n: int = 5) -> list[str]:
    """Recent commits touching `path` (used by the correlation agent)."""
    out = _git(["log", f"-{int(n)}", "--pretty=%h %ad %s", "--date=short", "--", path], Path(workdir), check=False)
    return out.splitlines()
