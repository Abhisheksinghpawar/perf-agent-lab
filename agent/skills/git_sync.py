"""Skill 1 - pull the latest code of the system under test and decide whether anything changed."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def sync(cfg: dict, root: Path, last_sha: str | None) -> dict:
    t = cfg["target"]
    work = (root / t["workdir"]).resolve()
    if (work / ".git").exists():
        _git(["fetch", "--depth", "200", "origin", t["branch"]], work)
        _git(["reset", "--hard", f"origin/{t['branch']}"], work)
    else:
        work.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--depth", "200", "--branch", t["branch"], t["repo_url"], str(work)])
    if t.get("ref"):                                         # reproducible run: test exactly this commit/tag
        _git(["fetch", "--unshallow", "--tags", "origin"], work, check=False)
        _git(["checkout", "--detach", t["ref"]], work)
    sha = _git(["rev-parse", "HEAD"], work)
    info = {
        "sha": sha,
        "short": sha[:8],
        "subject": _git(["log", "-1", "--pretty=%s"], work),
        "author": _git(["log", "-1", "--pretty=%an"], work),
        "date": _git(["log", "-1", "--pretty=%cI"], work),
        "repo": t["repo_url"].removesuffix(".git"),
        "branch": t["branch"],
        "workdir": str(work),
        "previous_sha": last_sha,
        "changed": last_sha != sha,
        "changed_files": [],
        "commits_since": [],
    }
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
