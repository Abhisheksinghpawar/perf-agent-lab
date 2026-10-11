"""Skill 4 - self-healing: when the smoke test fails, an LLM agent repairs the *Locust script* (never the app).

Guard rails (see validate_locustfile): the agent can only submit a new locustfile; it is parsed with `ast` and
rejected if it imports anything outside a small allow-list, touches the filesystem/network/process APIs, or targets
a host other than the --host Locust is given. Every proposal is verified by re-running the smoke test, and the
number of attempts is capped.
"""
from __future__ import annotations

import ast
import difflib
import json
import re
import shutil
from pathlib import Path

import requests

from ..llm import LLM, LLMUnavailable
from .smoke import run_smoke

ALLOWED_IMPORTS = {"locust", "random", "re", "time", "json", "logging", "recorder", "urllib", "itertools", "collections"}
FORBIDDEN_NAMES = {"eval", "exec", "compile", "open", "__import__", "input", "globals", "locals", "vars", "getattr",
                   "setattr", "delattr", "os", "sys", "subprocess", "socket", "shutil", "pathlib", "requests", "ctypes"}
MAX_BYTES = 14_000


def validate_locustfile(source: str) -> list[str]:
    """Return a list of rule violations (empty = acceptable)."""
    problems: list[str] = []
    if len(source.encode()) > MAX_BYTES:
        return [f"file too large (> {MAX_BYTES} bytes)"]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error line {exc.lineno}: {exc.msg}"]
    imported: set[str] = set()
    has_user = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                imported.add(root)
                if root not in ALLOWED_IMPORTS:
                    problems.append(f"import '{a.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            imported.add(root)
            if root not in ALLOWED_IMPORTS:
                problems.append(f"import from '{node.module}' is not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            problems.append(f"access to '{node.attr}' is not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.lower().startswith(("http://", "https://")):
            problems.append("absolute URLs are not allowed - use paths relative to the Locust --host")
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                if name in ("HttpUser", "FastHttpUser"):
                    has_user = True
    if not has_user:
        problems.append("must define a class deriving from HttpUser")
    if "recorder" not in imported:
        problems.append("must keep 'import recorder' (it writes the per-request log the pipeline analyses)")
    return sorted(set(problems))


def _page_tool(base_url: str):
    def fetch_page(path: str) -> dict:
        """GET a page of the app under test so you can inspect real URLs, forms and field names."""
        if not path.startswith("/"):
            return {"error": "path must start with '/'"}
        r = requests.get(base_url + path, timeout=15)
        body = r.text
        import re
        links = sorted(set(re.findall(r'href="([^"#]+)"', body)))[:25]
        forms = re.findall(r"<form[^>]*>", body)[:5]
        inputs = re.findall(r"<input[^>]*name=\"([^\"]+)\"[^>]*>", body)[:25]
        return {"status": r.status_code, "content_type": r.headers.get("content-type", ""), "links": links,
                "forms": forms, "input_names": inputs, "body_start": body[:900]}
    return fetch_page


SYSTEM = """You repair a Locust load-test script (Python) that stopped passing its smoke test.
Rules:
- Only change the locustfile. The application under test is correct; the script is out of date.
- Keep `import recorder` and use paths relative to the host (never absolute URLs).
- Use fetch_page() to look at the real pages (links, form field names) and fix URLs, parameters and response checks.
- Submit your fix with propose_locustfile(content). It validates the file and re-runs the smoke test for you and
  returns the result. Iterate until it passes. You have a small attempt budget; make each attempt count.
- You MUST submit a script: either call propose_locustfile(content), or reply with the COMPLETE script in one ```python block
  (it is then submitted for you). Describing a fix without submitting it counts as failure.
- Be economical: you get a few page fetches (the landing page is already included below), then submit your first attempt.
- When it passes, reply with one short sentence describing what you changed. No code in the final reply."""


def _landing(cfg: dict) -> str:
    """The app's landing page (links, forms) up front: saves the model several exploratory fetches (= tokens and minutes)."""
    t = cfg["target"]
    try:
        page = _page_tool(t["base_url"])(t.get("health_path") or "/")
        return f"Landing page {t.get('health_path') or '/'} -> " + json.dumps({k: page[k] for k in ("status", "links", "forms", "input_names")}, default=str)[:2500]
    except Exception:  # noqa: BLE001 - optional context only
        return "(landing page could not be fetched; use fetch_page)"


def heal(cfg: dict, llm: LLM, ctx, broken: Path, smoke_result: dict, out: Path) -> dict:
    """Try to repair `broken`. Returns {healed, attempts, history, diff, source}."""
    max_attempts = int(cfg["smoke"]["max_heal_attempts"])
    heal_dir = out / "heal"
    heal_dir.mkdir(exist_ok=True)
    original = broken.read_text(encoding="utf-8")
    state = {"attempts": 0, "passed": False, "source": None, "history": []}

    def read_locustfile() -> str:
        """Return the current (failing) locustfile source."""
        return original

    def propose_locustfile(content: str) -> dict:
        """Validate and smoke-test a replacement locustfile."""
        if state["attempts"] >= max_attempts:
            return {"error": f"attempt budget ({max_attempts}) exhausted"}
        state["attempts"] += 1
        n = state["attempts"]
        problems = validate_locustfile(content)
        if problems:
            state["history"].append({"attempt": n, "passed": False, "reasons": problems[:4], "stage": "validation"})
            return {"accepted": False, "problems": problems}
        cand = heal_dir / f"attempt{n}" / "locustfile.py"
        cand.parent.mkdir(exist_ok=True)
        cand.write_text(content, encoding="utf-8")
        shutil.copy(broken.parent / "recorder.py", cand.parent / "recorder.py")
        res = run_smoke(cfg, cand, heal_dir, tag=f"attempt{n}")
        state["history"].append({"attempt": n, "passed": res["passed"], "reasons": res["reasons"], "stage": "smoke",
                                 "requests": res["requests"], "failures": res["failures"]})
        if res["passed"]:
            state["passed"], state["source"] = True, content
        return {"accepted": True, "smoke_passed": res["passed"], "requests": res["requests"], "failures": res["failures"],
                "reasons": res["reasons"], "top_failures": res["top_failures"][:4]}

    tools = {
        "read_locustfile": ({"description": "Return the current failing locustfile.", "type": "object", "properties": {}},
                            read_locustfile),
        "fetch_page": ({"description": "GET a page of the app under test (path starting with /).", "type": "object",
                        "properties": {"path": {"type": "string"}}, "required": ["path"]}, _page_tool(cfg["target"]["base_url"])),
        "propose_locustfile": ({"description": "Submit a full replacement locustfile; it is validated and smoke-tested.",
                                "type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
                               propose_locustfile),
    }
    user = ("The smoke test failed.\n" + "\n".join(f"- {r}" for r in smoke_result["reasons"]) +
            "\nTop failures:\n" + "\n".join(f"- {f['endpoint']} -> {f['status'] or f['error']} (x{f['count']})"
                                          for f in smoke_result["top_failures"]) +
            f"\nApp base URL: {cfg['target']['base_url']}.\n" + _landing(cfg) + "\nStart by calling read_locustfile().")

    def on_text(reply: str):
        """A script pasted in a code block is submitted for the model (small models often fumble JSON-escaped tool arguments)."""
        m = re.search(r"```(?:python|py)?[ \t]*\n(.*?)```", reply, re.S)
        if not m or state["passed"] or state["attempts"] >= max_attempts:
            return None
        res = propose_locustfile(m.group(1))
        if res.get("smoke_passed"):
            return "The smoke test passed. Reply with one short sentence describing what you changed."
        return ("Result of submitting your script: " + json.dumps(res, default=str)[:1500] +
                ". Fix it and submit again (tool call or one ```python block).")

    summary = ""
    try:
        summary = llm.run_agent("heal", SYSTEM, user, tools, until=lambda: state["passed"] or state["attempts"] >= max_attempts,
                                max_turns=14, max_tokens=4000, on_text=on_text)      # a full script plus reasoning needs more than the default budget
    except LLMUnavailable as exc:
        state["history"].append({"attempt": state["attempts"], "passed": False, "reasons": [str(exc)], "stage": "llm"})

    diff = ""
    if state["source"]:
        diff = "".join(difflib.unified_diff(original.splitlines(True), state["source"].splitlines(True),
                                            "locustfile.broken.py", "locustfile.healed.py", n=2))
        (heal_dir / "locustfile.healed.py").write_text(state["source"], encoding="utf-8")
        (heal_dir / "heal.diff").write_text(diff, encoding="utf-8")
    return {"healed": state["passed"], "attempts": state["attempts"], "history": state["history"],
            "diff": diff, "source": state["source"], "summary": summary if state["passed"] else ""}   # never repeat an unverified claim
