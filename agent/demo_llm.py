"""A scripted, OpenAI-compatible /chat/completions server: lets anyone run the whole pipeline - including the two
agent steps - with NO API key and NO network access to an LLM (`python -m agent demo`).

It does NOT pretend to be a smart model: it replays a fixed tool-calling script so the *plumbing* (HTTP, tool loop,
script validation, smoke re-test, report) can be verified end to end. The report labels such runs "scripted demo".
Real model quality is a property of the provider you configure, not of this repo.
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

GOOD = ""


def reply(content=None, tool_calls=None, prompt_tokens=0):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [{"id": f"call_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                             for i, (n, a) in enumerate(tool_calls)]
    return {"choices": [{"message": msg}], "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 60}}


def tool_results(messages):
    return [json.loads(m["content"]) if m["content"].startswith(("{", "[")) else m["content"] for m in messages if m["role"] == "tool"]


def heal_turn(messages):
    n = len(tool_results(messages))
    if n == 0:
        return reply(tool_calls=[("read_locustfile", {})])
    if n == 1:
        return reply(tool_calls=[("fetch_page", {"path": "/catalog"})])
    if n == 2:
        return reply(tool_calls=[("propose_locustfile", {"content": GOOD})])
    return reply("Updated the stale *.do URLs to the real Spring MVC paths (/catalog/..., /cart/..., /account/signon) and added page-marker checks.")


def correlate_turn(messages):
    results = tool_results(messages)
    bundle = json.loads(messages[1]["content"].split("Evidence bundle:\n", 1)[1])
    top = (bundle.get("slowest_endpoints") or [{"name": "GET /catalog"}])[0]
    if len(results) == 0:
        return reply(tool_calls=[("find_handler", {"endpoint": top["name"]})])
    if len(results) == 1:
        return reply(tool_calls=[("get_log_window", {"start_rel": 0, "end_rel": 70, "max_lines": 10})])
    h = results[0] if isinstance(results[0], dict) else {}
    ref = [{"file": h["file"], "line": h["line"], "note": f"handler for {top['name']}"}] if "file" in h else []
    spikes = bundle.get("spikes") or []
    findings = [{"title": f"{top['name']} is the slowest endpoint (p95 {top.get('p95')} ms)", "severity": "medium", "category": "latency",
                 "endpoint": top["name"], "evidence": [f"p95 {top.get('p95')} ms over {top.get('count')} requests",
                                                        f"{len(spikes)} latency spike window(s) detected"],
                 "suspected_cause": "(scripted test answer) handler does database work per request", "code_refs": ref,
                 "recommendation": "Cache the read-mostly catalogue queries and verify with a before/after run."}]
    return reply(json.dumps({"summary": "(scripted test answer) One endpoint dominates latency; see finding.", "findings": findings,
                             "improvements": [{"priority": 1, "action": "Cache catalogue reads", "expected_impact": "lower p95 on read paths", "effort": "low"}]}))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        messages = body.get("messages", [])
        system = messages[0]["content"] if messages else ""
        if "repair a Locust" in system:
            out = heal_turn(messages)
        elif "senior performance engineer" in system:
            out = correlate_turn(messages)
        else:
            out = reply("ok")
        out["usage"]["prompt_tokens"] = sum(len(str(m.get("content", ""))) for m in messages) // 4
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_in_thread(port: int, good_locustfile: str):
    """Start the scripted server on a background thread; returns the HTTPServer (call .shutdown() to stop)."""
    import threading
    global GOOD
    GOOD = Path(good_locustfile).read_text(encoding="utf-8")
    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--good-locustfile", default=str(Path(__file__).resolve().parent.parent / "locust" / "locustfile.py"))
    a = ap.parse_args()
    GOOD = Path(a.good_locustfile).read_text(encoding="utf-8")
    print(f"scripted demo LLM on http://127.0.0.1:{a.port}/v1", flush=True)
    HTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
