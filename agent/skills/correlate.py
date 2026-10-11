"""Skill 8 - correlation: connect spikes and errors with logs, source code and recent commits, then recommend fixes.

Two implementations with the same output schema:
  * correlate_agent  - an LLM agent with read-only tools (spike details, log window, resource window, code search,
                       file reader, git history, endpoint->handler lookup). Used only when there is something to investigate.
  * correlate_heuristic - deterministic rules + the same lookup tools. Always available; zero tokens.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from ..llm import LLM, LLMUnavailable, extract_json
from . import logs as logs_skill
from .git_sync import EXCLUDE_DIRS, git_log_for

SRC_GLOBS = ("*.java", "*.xml", "*.jsp", "*.properties")      # defaults; the real values come from [target] source_globs / source_dirs
HTTP_METHODS = r"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)"
ROUTE_HINT = re.compile(r"route|router|app\.|@|path|get\(|post\(|put\(|delete\(|case |handle|mapping|if .*==", re.I)
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


class Investigator:
    """Read-only access to the code base, logs and metrics of one run. Every method is safe to expose to an agent."""

    def __init__(self, workdir: str, analysis: dict, parsed_logs: dict, metrics: list[dict], t0: float, pkg: str = "org.mybatis.jpetstore",
                 dirs: tuple | list = ("src",), globs: tuple | list = SRC_GLOBS):
        self.pkg = pkg
        self.dirs, self.globs = tuple(dirs), tuple(globs)
        self.root = Path(workdir).resolve()
        self.analysis = analysis
        self.logs = parsed_logs
        self.metrics = metrics
        self.t0 = t0

    # -- helpers ------------------------------------------------------------------------------------
    def _safe(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root not in p.parents and p != self.root:
            raise ValueError("path outside the repository")
        return p

    def _files(self):
        """Source files of the app (configured folders and patterns), skipping dependencies and build output."""
        seen = set()
        for d in self.dirs:
            base = (self.root / d).resolve()
            if self.root not in base.parents and base != self.root:
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = sorted(x for x in dirnames if x not in EXCLUDE_DIRS)
                for name in sorted(filenames):
                    if any(Path(name).match(g) for g in self.globs):
                        p = Path(dirpath) / name
                        if p not in seen:
                            seen.add(p)
                            yield p

    # -- tools --------------------------------------------------------------------------------------
    def get_spike(self, index: int) -> dict:
        sp = self.analysis["spikes"]
        return sp[index] if 0 <= index < len(sp) else {"error": f"no spike {index} (have {len(sp)})"}

    def get_log_window(self, start_rel: float, end_rel: float, max_lines: int = 20) -> list[str]:
        return logs_skill.window(self.logs, self.t0 + start_rel, self.t0 + end_rel, min(int(max_lines), 40)) or ["(no notable log lines)"]

    def get_exceptions(self, start_rel: float, end_rel: float) -> list[dict]:
        """Exceptions logged in the window, grouped, each with the first application stack frames (file:line) and any SQL/mapper context."""
        win = [x for x in self.logs["exceptions"] if self.t0 + start_rel <= x["ts"] <= self.t0 + end_rel]
        return [{k: v for k, v in g.items() if k not in ("first_ts", "last_ts")} for g in logs_skill.group_exceptions(win, self.pkg, root=str(self.root))] or [{"info": "no exceptions in this window"}]

    def get_resource_window(self, start_rel: float, end_rel: float) -> dict:
        rows = [m for m in self.metrics if start_rel <= m["ts"] - self.t0 < end_rel]
        if not rows:
            return {"error": "no samples"}
        avg = lambda k: round(sum(r[k] for r in rows) / len(rows), 1)  # noqa: E731
        return {"samples": len(rows), "cpu_avg": avg("cpu_pct"), "cpu_max": max(r["cpu_pct"] for r in rows),
                "java_cpu_avg": avg("java_cpu_pct"), "locust_cpu_avg": avg("locust_cpu_pct"), "java_rss_mb": rows[-1]["java_rss_mb"],
                "net_kbs": round(avg("net_sent_kbs") + avg("net_recv_kbs"), 1),
                "gc": logs_skill.gc_in_window(self.logs, self.t0 + start_rel, self.t0 + end_rel)}

    def grep_code(self, pattern: str, max_results: int = 12) -> list[str]:
        try:
            rx = re.compile(pattern, re.I)
        except re.error as exc:
            return [f"bad regex: {exc}"]
        out = []
        for f in self._files():
            try:
                for n, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        out.append(f"{f.relative_to(self.root).as_posix()}:{n}: {line.strip()[:140]}")
                        if len(out) >= min(int(max_results), 25):
                            return out
            except OSError:
                continue
        return out or ["(no matches)"]

    def read_file(self, path: str, start: int = 1, end: int = 60) -> str:
        p = self._safe(path)
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        start = max(int(start), 1)
        end = min(int(end), start + 79, len(lines))
        return "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))

    def git_log(self, path: str) -> list[str]:
        self._safe(path)
        return git_log_for(str(self.root), path, 5)

    def find_handler(self, endpoint: str) -> dict:
        """Map a Locust endpoint name like 'GET /catalog/viewProduct' to the code that serves it: Spring MVC controllers are
        resolved precisely; for any other stack the route string is searched in the source."""
        r = self._find_spring_handler(endpoint)
        if "file" in r:
            return r
        return self._find_route(endpoint) or r

    def _find_route(self, endpoint: str) -> dict | None:
        """Generic lookup: the first source line that mentions the route as a string literal (best one first)."""
        m = re.match(rf"^\s*(?:{HTTP_METHODS}\s+)?(/[^\s?(#]*)", endpoint)
        if not m:
            return None
        path = m.group(1).rstrip("/") or "/"
        rx = re.compile(r"""["'`]""" + re.escape(path) + r"""/?(?:[?#][^"'`]*)?["'`]""")
        best = None
        for f in self._files():
            try:
                lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            for n, text in enumerate(lines, 1):
                if rx.search(text):
                    hit = (f.relative_to(self.root).as_posix(), n)
                    if ROUTE_HINT.search(text):
                        return {"file": hit[0], "line": hit[1], "action": path, "recent_commits": self.git_log(hit[0])}
                    best = best or hit
        return {"file": best[0], "line": best[1], "action": path, "recent_commits": self.git_log(best[0])} if best else None

    def _find_spring_handler(self, endpoint: str) -> dict:
        m = re.search(r"/([A-Za-z]+)(?:/([A-Za-z]+))?", endpoint)
        if not m:
            return {"error": "endpoint has no /path part"}
        base, action = m.group(1), m.group(2)
        ctrl = None
        for f in self._files():
            if f.suffix != ".java" or "controller" not in f.name.lower():
                continue
            if re.search(rf'@RequestMapping\(\s*"/{re.escape(base)}"', f.read_text(encoding="utf-8", errors="ignore")):
                ctrl = f
                break
        if not ctrl:
            return {"error": f"no controller mapped to /{base}"}
        rel = ctrl.relative_to(self.root).as_posix()
        line = 1
        lines = ctrl.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not action:                                           # "/catalog" itself: @GetMapping({ "", "/" }) or @GetMapping("")
            for n, text in enumerate(lines, 1):
                if re.search(r'@(?:Get|Post)Mapping\(\s*(?:\{\s*)?"/?"', text):
                    line = n
                    break
        if action:
            rx = re.compile(rf'@(?:Get|Post|Request)Mapping\(\s*(?:value\s*=\s*)?[{{\s]*"/{re.escape(action)}"')
            for n, text in enumerate(lines, 1):
                if rx.search(text):
                    line = n
                    break
        return {"file": rel, "line": line, "action": action, "recent_commits": self.git_log(rel)}


# ======================================================================================================
def _tools(inv: Investigator) -> dict:
    def schema(desc, **props):
        return {"description": desc, "type": "object", "properties": props, "required": list(props)}

    num, string = {"type": "number"}, {"type": "string"}
    return {
        "get_spike": (schema("Details of latency spike #index.", index={"type": "integer"}), inv.get_spike),
        "get_log_window": (schema("App/GC log lines between two run-relative seconds.", start_rel=num, end_rel=num), inv.get_log_window),
        "get_exceptions": (schema("Exceptions in a window with application stack frames (file:line) and SQL/mapper context.", start_rel=num, end_rel=num), inv.get_exceptions),
        "get_resource_window": (schema("CPU/memory/network/GC summary between two run-relative seconds.", start_rel=num, end_rel=num), inv.get_resource_window),
        "find_handler": (schema("Source file+line of the handler for an endpoint name, with recent commits.", endpoint=string), inv.find_handler),
        "grep_code": (schema("Regex search in the app's source files (returns file:line: text).", pattern=string), inv.grep_code),
        "read_file": (schema("Read lines start..end (max 80) of a repo file.", path=string, start={"type": "integer"}, end={"type": "integer"}), inv.read_file),
        "git_log": (schema("Recent commits for a file path.", path=string), inv.git_log),
    }


SYSTEM = """You are a senior performance engineer reviewing one short load test of a web app ({app}).
You get a compact evidence bundle. Investigate only what matters (max 3 issues): use the tools to look at the log
window and the exceptions (get_exceptions) around each spike/error, the resource window, and the code that handles the slow endpoint (find_handler first,
then read_file around the returned line). Stay evidence-based: say 'unknown' when the evidence is thin; note that the
load generator shares the machine with the app, so CPU contention is a possible cause. Use at most 5 tool calls in total, then reply. Issues already listed in
already_known_issues should be marked recurring rather than re-investigated in depth. Endpoints in slower_on_same_commit
got slower although the code did not change: that is environment variance, not a regression - do not blame commits for it.

Reply with ONLY one JSON object, no prose:
{"summary": "2-3 sentence executive summary",
 "findings": [{"title": "...", "severity": "high|medium|low|info", "category": "latency|errors|resource|regression|gc",
               "endpoint": "... or null", "evidence": ["short facts with numbers"], "suspected_cause": "...",
               "code_refs": [{"file": "path", "line": 123, "note": "..."}], "recommendation": "concrete fix"}],
 "improvements": [{"priority": 1, "action": "...", "expected_impact": "...", "effort": "low|medium|high"}]}"""


def correlate_agent(llm: LLM, inv: Investigator, bundle: dict, app_name: str = "the app under test") -> dict | None:
    import json
    nudges = {"n": 0}

    def on_text(reply: str):
        """Small models answer in prose or run out of turns: ask twice for the JSON object before giving up."""
        d = extract_json(reply)
        if (isinstance(d, dict) and "findings" in d) or nudges["n"] >= 2:
            return None
        nudges["n"] += 1
        return "Your reply was not the required JSON object. Reply now with ONLY that JSON object (no prose, no code fences), based on what you have learned."

    llm.last_error = ""
    try:
        text = llm.run_agent("correlate", SYSTEM.replace("{app}", app_name), "Evidence bundle:\n" + json.dumps(bundle, default=str), _tools(inv),
                             on_text=on_text, max_turns=8, max_tokens=3000)
    except LLMUnavailable as exc:
        llm.last_error = str(exc)
        return None
    data = extract_json(text)
    if not isinstance(data, dict) or "findings" not in data:
        llm.last_error = "reply was not the required JSON: " + (text[:120].replace("\n", " ") or "(empty)")
        return None
    data["findings"] = [f for f in data.get("findings", []) if isinstance(f, dict) and f.get("title")][:6]
    data.setdefault("improvements", [])
    data.setdefault("summary", "")
    return data


# ======================================================================================================
def _explain_exception(top: dict, inv: Investigator, code_refs: list[dict]) -> tuple[str, str]:
    """Rule-based reading of an exception message. Wording stays hedged: these are leads to verify, not verdicts."""
    text = f"{top['type']} {top['message']} {' '.join(top.get('context', []))}".lower()
    if re.search(r"unique constraint|duplicate ?key|pk_|primary key", text):
        for f in top["app_frames"][:1]:                      # look for id generation near the failing call
            try:
                src = inv.read_file(f["file"], max(f["line"] - 20, 1), f["line"] + 3)
            except (OSError, ValueError):
                break
            for ln in src.splitlines():
                if re.search(r"getNextId|nextId|next_?\w*id|sequence", ln, re.I):
                    num = int(ln.split(":", 1)[0])
                    code_refs.insert(1, {"file": f["file"], "line": num, "note": "primary key is generated here - check that it is atomic"})
                    break
        return ("concurrent requests inserted the same primary key: id generation is probably a non-atomic read-then-increment (race condition)",
                "Allocate ids atomically (database sequence / identity column, or a single UPDATE ... RETURNING) and retry once on a key collision.")
    if re.search(r"already exists|duplicate|conflict", text):             # not a database key, but the same kind of race
        for f in top["app_frames"][:1]:
            try:
                src = inv.read_file(f["file"], max(f["line"] - 20, 1), f["line"] + 3)
            except (OSError, ValueError):
                break
            hits = [int(ln.split(":", 1)[0]) for ln in src.splitlines()
                    if re.search(r"next_?\w*id|nextId|sequence|counter|increment", ln, re.I) and int(ln.split(":", 1)[0]) <= f["line"]]
            if hits:                                       # the nearest one above the failure is the call that produced the id
                code_refs.insert(1, {"file": f["file"], "line": hits[-1], "note": "id/counter is generated here - check that it is atomic"})
        return ("a uniqueness check failed under concurrent load: ids are probably allocated with a non-atomic read-then-increment (race condition)",
                "Allocate ids atomically (a database sequence or identity column, an atomic counter, or a lock) and retry once on a collision.")
    if re.search(r"timeout|timed out|lock wait", text):
        return "a database or downstream call timed out under load", "Add timeouts and indexes for the slow call, then size the connection pool for the target concurrency."
    if "nullpointer" in text:
        return "an unhandled null in the request path", "Guard the dereference and add a regression test for the failing input."
    if "outofmemory" in text:
        return "the JVM heap was exhausted", "Raise the heap and look for unbounded collections or caches."
    if re.search(r"connection|pool", text):
        return "the connection pool was exhausted", "Increase pool size or shorten transactions."
    return f"{top['type']} raised in {top['where'] or 'framework code'}", "Inspect the stack trace around the first application frame."


def correlate_heuristic(inv: Investigator, bundle: dict, analysis: dict, log_summary: dict) -> dict:
    """Rule-based fallback: same schema as the agent, no LLM."""
    findings, improvements = [], []

    def refs(endpoint: str | None) -> list[dict]:
        if not endpoint:
            return []
        h = inv.find_handler(endpoint)
        return [{"file": h["file"], "line": h["line"], "note": f"handler for {endpoint}"}] if "file" in h else []

    for i, sp in enumerate(analysis["spikes"][:3]):
        if sp["phase"] == "warm-up":
            continue
        top = sp["top_endpoints"][0] if sp["top_endpoints"] else {}
        win_logs = inv.get_log_window(sp["start_rel"], sp["end_rel"], 6)
        gc = logs_skill.gc_in_window(inv.logs, sp["start_ts"], sp["end_ts"])
        cause = "unknown - no matching log evidence"
        if gc["max_pause_ms"] >= 50:
            cause = f"GC pauses (max {gc['max_pause_ms']} ms) overlap the spike"
        elif sp["cpu_avg"] >= 85:
            cause = "host CPU saturated during the spike (load generator shares the machine)"
        findings.append({"title": f"Latency spike {sp['start_rel']:.0f}-{sp['end_rel']:.0f}s (p95 {sp['peak_p95']:.0f} ms)",
                         "severity": "high" if sp["peak_p95"] > 1000 else "medium", "category": "latency",
                         "endpoint": top.get("name"), "evidence": [f"p95 {sp['peak_p95']} ms vs median {sp['median_p95']} ms",
                                                                    f"CPU avg {sp['cpu_avg']}% (java {sp['java_cpu_avg']}%)",
                                                                    *win_logs[:2]],
                         "suspected_cause": cause, "code_refs": refs(top.get("name")),
                         "recommendation": "Profile the slowest endpoint during the spike window and check GC/CPU." })
    for c in analysis["error_clusters"][:3]:
        exs = inv.get_exceptions(max(c["first_rel"] - 2, 0), c["last_rel"] + 2)
        top = exs[0] if exs and "type" in exs[0] else None
        evidence = [f"HTTP {c['status'] or 'n/a'} x{c['count']} between {c['first_rel']}s and {c['last_rel']}s"]
        code_refs = refs(c["endpoint"])
        cause, rec = "no server-side exception was logged", "Reproduce with a single request and inspect the server log for the failing call."
        if top:
            evidence.append(f"{top['type']}: {top['message']} (x{top['count']})")
            evidence += [f"context: {x}" for x in top.get("context", [])[:2]]
            code_refs = [{"file": f["file"], "line": f["line"], "note": f"{f['method']} (throws {top['type']})"} for f in top["app_frames"][:2]] + code_refs
            cause, rec = _explain_exception(top, inv, code_refs)
        findings.append({"title": f"{c['count']} failed request(s) on {c['endpoint']}", "severity": "high" if c["count"] > 10 else "medium",
                         "category": "errors", "endpoint": c["endpoint"], "evidence": evidence, "suspected_cause": cause,
                         "code_refs": code_refs, "recommendation": rec})
    for r in analysis["regressions"][:2]:
        findings.append({"title": f"{r['endpoint']} is {r['delta_pct']:.0f}% slower than baseline", "severity": "medium", "category": "regression",
                         "endpoint": r["endpoint"], "evidence": [f"p95 {r['p95']} ms vs baseline {r['baseline_p95']} ms"],
                         "suspected_cause": "the code changed since the baseline was recorded" + (": " + ", ".join(bundle["commit"]["changed_files"][:3]) if bundle["commit"]["changed_files"] else ""),
                         "code_refs": refs(r["endpoint"]), "recommendation": "Review the recent commits that touch this handler, then re-run to rule out runner noise."})
    g = log_summary["gc"]
    if g["max_pause_ms"] >= 100 or g["full_gc"]:
        findings.append({"title": f"GC pauses up to {g['max_pause_ms']} ms ({g['full_gc']} full GC)", "severity": "medium", "category": "gc",
                         "endpoint": None, "evidence": [f"{g['count']} GC events, {g['total_pause_ms']} ms total pause"],
                         "suspected_cause": "heap too small or allocation churn", "code_refs": [],
                         "recommendation": "Increase heap / try G1 tuning and review allocation hot spots."})
    res = analysis["resources"]
    if res and res.get("cpu_saturated_s", 0) >= 10:
        findings.append({"title": f"CPU saturated for {res['cpu_saturated_s']} s", "severity": "medium", "category": "resource",
                         "endpoint": None, "evidence": [f"CPU avg {res['cpu_avg']}% max {res['cpu_max']}%, java avg {res['java_cpu_avg']}%, locust avg {res['locust_cpu_avg']}%"],
                         "suspected_cause": "app and load generator compete for the same cores", "code_refs": [],
                         "recommendation": "Run the generator on a separate host to separate app limits from tool limits."})
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    if findings:
        improvements = [{"priority": n, "action": f["recommendation"], "expected_impact": f["title"], "effort": "medium"}
                        for n, f in enumerate(findings[:4], 1)]
        summary = f"{len(findings)} item(s) need attention; the most important is: {findings[0]['title']}."
    else:
        summary = "No errors, regressions or steady-state spikes detected in this run."
    if analysis.get("variance"):
        summary += (f" {len(analysis['variance'])} endpoint(s) were slower than baseline on unchanged code;"
                    " treated as runner variance, not a regression.")
    return {"summary": summary, "findings": findings, "improvements": improvements}
