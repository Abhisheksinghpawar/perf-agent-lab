"""Skill 7 - read the application log and line it up with the load-test timeline.

Understands: Java (Tomcat/JUL and SimpleFormatter lines, `at pkg.Class.method(File.java:12)` stack traces, JVM GC events)
and anything that prints `2026-10-11 03:12:44,123 ERROR message` style lines, including Python tracebacks.
Lines need timestamps: that is how errors are matched to the load-test timeline."""
from __future__ import annotations

import calendar
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
JUL = re.compile(r"(\d{2})-([A-Z][a-z]{2})-(\d{4}) (\d{2}):(\d{2}):(\d{2})\.(\d{3}) (SEVERE|WARNING|INFO|CONFIG|FINE\w*)\s+\[([^\]]*)\]\s+(\S+)\s?(.*)")
GC = re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3})([+-]\d{4})\]\[([\d.]+)s\]\s+GC\((\d+)\)\s+(Pause .+?)\s+(\d+)M->(\d+)M\((\d+)M\)\s+([\d.]+)ms")
EXC = re.compile(r"^(?:[\w.$-]+\.)?([\w$]+(?:Exception|Error))(?::\s*(.*))?$")
CARGO_PREFIX = re.compile(r"^\[(?:INFO|WARN|WARNING|ERROR|DEBUG)\]\s*(?:\[talledLocalContainer\]\s*)?")
SIMPLE_DATE = re.compile(r"^([A-Z][a-z]{2}) (\d{1,2}), (\d{4}) (\d{1,2}):(\d{2}):(\d{2}) (AM|PM) (\S+) (\S+)\s*$")
SIMPLE_LEVEL = re.compile(r"^(SEVERE|WARNING|INFO|CONFIG|FINE|FINER|FINEST): ?(.*)$")
LEVEL_HINT = re.compile(r"\b(ERROR|SEVERE|WARN(?:ING)?|FATAL)\b")
# "2026-10-11 03:12:44,123 ERROR msg" / "[2026-10-11T03:12:44.123Z] WARN msg" (Python logging, Node/pino-pretty, Go zap console, ...)
ISO_LOG = re.compile(r"^\[?(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?(Z|[+-]\d{2}:?\d{2})?\]?\s+\[?"
                     r"(CRITICAL|FATAL|ERROR|WARNING|WARN|INFO|DEBUG|TRACE)\]?:?\s*(.*)$")
PYFRAME = re.compile(r'^File "([^"]+)", line (\d+), in (\S+)')
PYEXC = re.compile(r"^(?:[\w.]+\.)?(\w+)(?::\s*(.*))?$")


def _jul_ts(m: re.Match) -> float:
    d, mon, y, hh, mm, ss, ms = m.groups()[:7]
    return time.mktime((int(y), MONTHS[mon], int(d), int(hh), int(mm), int(ss), 0, 0, -1)) + int(ms) / 1000


def _simple_ts(m: re.Match) -> float:
    mon, d, y, hh, mm, ss, ap = m.groups()[:7]
    hour = int(hh) % 12 + (12 if ap == "PM" else 0)
    return time.mktime((int(y), MONTHS[mon], int(d), hour, int(mm), int(ss), 0, 0, -1))


def _iso_ts(m: re.Match) -> float:
    y, mo, d, hh, mm, ss, frac, tz = m.groups()[:8]
    parts = (int(y), int(mo), int(d), int(hh), int(mm), int(ss), 0, 0, -1)
    secs = int((frac or "0").ljust(6, "0")[:6]) / 1e6
    if not tz:
        return time.mktime(parts) + secs                      # no zone in the line: assume this machine's local time
    off = 0 if tz == "Z" else (1 if tz[0] == "+" else -1) * (int(tz[1:3]) * 3600 + int(tz[-2:]) * 60)
    return calendar.timegm(parts[:6]) - off + secs


def parse(path: Path) -> dict:
    events, exceptions, gc = [], [], []
    last_ts = None
    cur_exc = None
    ctx_lines: list[str] = []          # MyBatis prints "### SQL: ..." / "### The error may exist in X.xml" before the trace
    if not Path(path).exists():
        return {"events": [], "exceptions": [], "gc": [], "lines": 0}
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    pending_ts, pending_src = None, ""
    py_exc = None                       # a Python traceback being read: {"ts", "frames"}
    for raw in lines:
        line = CARGO_PREFIX.sub("", raw.rstrip())          # cargo tags every container line with "[INFO] "
        sd = SIMPLE_DATE.match(line.strip())
        if sd:                                              # line 1 of Java's SimpleFormatter: "Oct 10, 2026 5:51:04 PM class method"
            pending_ts, pending_src = _simple_ts(sd), f"{sd.group(8)}.{sd.group(9)}"
            continue
        sl = SIMPLE_LEVEL.match(line.strip())
        if sl and pending_ts is not None:                   # line 2: "SEVERE: message"
            last_ts = pending_ts
            events.append({"ts": last_ts, "level": sl.group(1), "source": pending_src, "msg": sl.group(2)[:240]})
            pending_ts, cur_exc, ctx_lines = None, None, []
            continue
        iso = ISO_LOG.match(line.strip())
        if iso:
            last_ts = _iso_ts(iso)
            events.append({"ts": last_ts, "level": iso.group(9), "source": "", "msg": iso.group(10)[:240]})
            cur_exc, py_exc = None, None
            continue
        m = JUL.search(line)
        if m:
            last_ts = _jul_ts(m)
            level, source, msg = m.group(8), m.group(10), m.group(11)
            events.append({"ts": last_ts, "level": level, "source": source, "msg": msg[:240]})
            cur_exc = None
            continue
        g = GC.search(line)
        if g:
            dt = datetime.strptime(g.group(1) + g.group(2), "%Y-%m-%dT%H:%M:%S.%f%z")
            gc.append({"ts": dt.timestamp(), "kind": g.group(5).strip(), "before_mb": int(g.group(6)),
                       "after_mb": int(g.group(7)), "heap_mb": int(g.group(8)), "pause_ms": float(g.group(9))})
            continue
        stripped = line.strip()
        if stripped.startswith("### ") and last_ts is not None:
            keep = re.match(r"(SQL:|The error may exist in|Error updating database|Error querying database)", stripped[4:])
            if keep and len(ctx_lines) < 6:                  # the useful MyBatis lines: failing SQL + mapper file
                ctx_lines.append(stripped[4:184])
            continue
        if stripped.startswith("Traceback (most recent call last)") and last_ts is not None:
            py_exc, cur_exc = {"ts": last_ts, "frames": []}, None
            continue
        if py_exc is not None:
            fm = PYFRAME.match(stripped)
            if fm:
                py_exc["frames"].append(stripped[:200])
                continue
            if line[:1] in (" ", "\t") or not stripped:             # the source line / caret markers under a frame
                continue
            pe = PYEXC.match(stripped)
            if pe:                                                  # first unindented line after the frames = the exception
                exceptions.append({"ts": py_exc["ts"], "type": pe.group(1), "msg": (pe.group(2) or "")[:200],
                                   "trace": list(reversed(py_exc["frames"][-80:])), "context": []})   # innermost frame first, like Java
                py_exc = None
                continue
            py_exc = None
        if stripped.startswith("at ") and cur_exc is not None:
            if len(cur_exc["trace"]) < 80:
                cur_exc["trace"].append(stripped)
            continue
        e = EXC.match(stripped)
        if e and last_ts is not None:
            cur_exc = {"ts": last_ts, "type": e.group(1), "msg": (e.group(2) or "")[:200], "trace": [], "context": ctx_lines[:4]}
            exceptions.append(cur_exc)
            continue
        if LEVEL_HINT.search(line) and last_ts is not None:
            events.append({"ts": last_ts, "level": LEVEL_HINT.search(line).group(1), "source": "", "msg": stripped[:240]})
        cur_exc = None
    return {"events": events, "exceptions": exceptions, "gc": gc, "lines": len(lines)}


def _in(rows, a, b):
    return [r for r in rows if a <= r["ts"] <= b]


FRAME = re.compile(r"at ([\w.$]+)\.([\w$<>]+)\((\w+\.java):(\d+)\)")
JDK_PREFIXES = ("java.", "javax.", "jdk.", "sun.", "org.apache.", "org.springframework.")


def _py_relpath(path: str, root: str | None) -> str | None:
    """A Python frame's file as a path inside the app folder, or None when it is library/stdlib code."""
    path = path.replace("\\", "/")
    if root:
        r = str(root).replace("\\", "/").rstrip("/")
        if path.lower().startswith(r.lower() + "/"):
            path = path[len(r) + 1:]
    if os.path.isabs(path) or re.match(r"^[A-Za-z]:/", path) or path.startswith("<"):
        return None                                             # outside the app folder (stdlib, site-packages, <frozen ...>)
    parts = set(path.split("/"))
    return None if parts & {"site-packages", "dist-packages", "node_modules"} else path


def app_frames(trace: list[str], pkg: str, limit: int = 3, root: str | None = None) -> list[dict]:
    """First `limit` stack frames that belong to the application (not the JDK/Spring/MyBatis/HSQLDB, nor Python libraries)."""
    out = []
    for line in trace:
        m = FRAME.search(line)
        if m:
            ours = m.group(1).startswith(pkg) if pkg else not m.group(1).startswith(JDK_PREFIXES)
            if ours:
                cls = m.group(1).split("$")[0]
                out.append({"file": "src/main/java/" + cls.replace(".", "/") + ".java", "line": int(m.group(4)),
                            "method": f"{cls.rsplit('.', 1)[-1]}.{m.group(2)}"})
        else:
            pm = PYFRAME.match(line.strip())
            rel = _py_relpath(pm.group(1), root) if pm else None
            if rel:
                out.append({"file": rel, "line": int(pm.group(2)), "method": f"{Path(rel).stem}.{pm.group(3)}"})
        if len(out) >= limit:
            break
    return out


def group_exceptions(exs: list[dict], pkg: str, limit: int = 5, root: str | None = None) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for x in exs:
        frames = app_frames(x["trace"], pkg, root=root)
        key = (x["type"], re.sub(r"\d+", "#", x["msg"][:80]), frames[0]["method"] if frames else "")   # ids/counters in the message must not split a group
        g = groups.setdefault(key, {"type": x["type"], "message": x["msg"][:160], "count": 0, "app_frames": frames,
                                    "where": (f"{frames[0]['method']} ({frames[0]['file'].rsplit('/', 1)[-1]}:{frames[0]['line']})" if frames else ""),
                                    "context": x.get("context", [])[:3], "first_ts": x["ts"], "last_ts": x["ts"]})
        g["count"] += 1
        g["first_ts"], g["last_ts"] = min(g["first_ts"], x["ts"]), max(g["last_ts"], x["ts"])
    return sorted(groups.values(), key=lambda g: g["count"], reverse=True)[:limit]


def summarize(parsed: dict, t_start: float, t_end: float, pad: float = 5.0, pkg: str = "org.mybatis.jpetstore",
              root: str | None = None) -> dict:
    """Compact, token-cheap summary of what the app logged while the load test ran."""
    a, b = t_start - pad, t_end + pad
    ev = _in(parsed["events"], a, b)
    ex = _in(parsed["exceptions"], a, b)
    gc = _in(parsed["gc"], a, b)
    levels = Counter(e["level"] for e in ev)
    top_ex = [{k: v for k, v in g.items() if k not in ("first_ts", "last_ts")} for g in group_exceptions(ex, pkg, root=root)]
    warn_counts = Counter(e["msg"][:140] for e in ev if e["level"] in ("WARNING", "WARN", "SEVERE", "ERROR"))
    pauses = [g["pause_ms"] for g in gc]
    return {
        "log_lines": parsed["lines"],
        "levels": dict(levels),
        "exceptions": top_ex,
        "warnings": [f"{m} (x{c})" if c > 1 else m for m, c in warn_counts.most_common(5)],
        "gc": {"count": len(gc), "total_pause_ms": round(sum(pauses), 1), "max_pause_ms": round(max(pauses), 1) if pauses else 0,
               "full_gc": sum(1 for g in gc if "Full" in g["kind"]),
               "heap_after_max_mb": max((g["after_mb"] for g in gc), default=0)},
    }


def window(parsed: dict, ts0: float, ts1: float, max_lines: int = 25) -> list[str]:
    """Raw-ish log lines between two epoch timestamps (for the correlation agent's get_log_window tool)."""
    rows = [(e["ts"], f"{e['level']} {e['source']} {e['msg']}".strip()) for e in _in(parsed["events"], ts0, ts1)
            if e["level"] != "INFO" or "Exception" in e["msg"]]
    rows += [(x["ts"], f"{x['type']}: {x['msg']} @ {x['trace'][0] if x['trace'] else '?'}") for x in _in(parsed["exceptions"], ts0, ts1)]
    rows += [(g["ts"], f"GC {g['kind']} {g['before_mb']}M->{g['after_mb']}M pause {g['pause_ms']}ms") for g in _in(parsed["gc"], ts0, ts1)
             if g["pause_ms"] >= 10 or "Full" in g["kind"]]
    rows.sort()
    return [f"+{ts - ts0:5.1f}s {text}" for ts, text in rows[:max_lines]]


def gc_in_window(parsed: dict, ts0: float, ts1: float) -> dict:
    gc = _in(parsed["gc"], ts0, ts1)
    return {"count": len(gc), "total_pause_ms": round(sum(g["pause_ms"] for g in gc), 1),
            "max_pause_ms": round(max((g["pause_ms"] for g in gc), default=0), 1)}
