import json
import unittest

from tests.helpers import CFG, make_metrics, make_records, make_repo
from agent.context import RunContext
from agent.llm import LLM, extract_json
from agent.skills import analyze, correlate, report
from agent.skills.logs import parse
from pathlib import Path
import tempfile


class AgentLoopTests(unittest.TestCase):
    def test_tool_loop_executes_tools_and_counts_tokens(self):
        ctx = RunContext(CFG, Path(tempfile.mkdtemp()))
        calls = []

        def transport(payload):
            calls.append(payload)
            if len(calls) == 1:                      # model asks for a tool
                return {"usage": {"prompt_tokens": 50, "completion_tokens": 10}, "choices": [{"message": {
                    "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "add", "arguments": json.dumps({"a": 2, "b": 3})}}]}}]}
            tool_msg = [m for m in payload["messages"] if m["role"] == "tool"][0]
            return {"usage": {"prompt_tokens": 70, "completion_tokens": 5}, "choices": [{"message": {"content": "sum is " + tool_msg["content"]}}]}

        llm = LLM(CFG, ctx, transport=transport)
        seen = []
        out = llm.run_agent("demo", "sys", "user", {"add": ({"description": "add", "type": "object", "properties": {}}, lambda a, b: a + b)},
                            on_tool=lambda n, a, r: seen.append((n, r)))
        self.assertEqual(out, "sum is 5")
        self.assertEqual(seen, [("add", 5)])
        self.assertEqual(ctx.tokens["demo"], {"prompt": 120, "completion": 15, "calls": 2})
        self.assertIn("tools", calls[0])

    def test_tool_errors_go_back_to_the_model(self):
        ctx = RunContext(CFG, Path(tempfile.mkdtemp()))
        seen = {}

        def transport(payload):
            tool = [m for m in payload["messages"] if m["role"] == "tool"]
            if not tool:
                return {"choices": [{"message": {"content": "", "tool_calls": [{"id": "1", "function": {"name": "boom", "arguments": "{}"}}]}}]}
            seen["result"] = tool[0]["content"]
            return {"choices": [{"message": {"content": "recovered"}}]}

        def boom():
            raise ValueError("nope")

        out = LLM(CFG, ctx, transport=transport).run_agent("d", "s", "u", {"boom": ({"type": "object", "properties": {}}, boom)})
        self.assertEqual(out, "recovered")
        self.assertIn("ValueError", seen["result"])

    def test_disabled_without_key(self):
        ctx = RunContext(CFG, Path(tempfile.mkdtemp()))
        self.assertFalse(LLM(CFG, ctx, enabled=False).enabled)

    def test_extract_json(self):
        self.assertEqual(extract_json('Sure!\n```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('text {"a": [1,2]} tail'), {"a": [1, 2]})
        self.assertIsNone(extract_json("no json here"))


class CorrelateTests(unittest.TestCase):
    def setUp(self):
        recs, self.t0 = make_records(spike=(30, 40, 600), fail_endpoint="GET /cart")
        self.metrics = make_metrics(self.t0)
        self.analysis = analyze.analyze(recs, self.metrics, CFG)
        self.repo = make_repo()
        self.inv = correlate.Investigator(str(self.repo), self.analysis, {"events": [], "exceptions": [], "gc": [], "lines": 0}, self.metrics, self.t0)

    def test_find_handler_maps_endpoint_to_source_line(self):
        h = self.inv.find_handler("GET /catalog/viewProduct")
        self.assertTrue(h["file"].endswith("CatalogController.java"))
        self.assertEqual(h["line"], 9)
        self.assertIn("error", self.inv.find_handler("GET /nothing/here"))

    def test_grep_and_read_file(self):
        self.assertTrue(any("getItemListByProduct" in r for r in self.inv.grep_code("getItemListByProduct")))
        self.assertIn("viewProduct", self.inv.read_file("src/main/java/org/mybatis/jpetstore/web/controllers/CatalogController.java", 9, 11))

    def test_read_file_blocks_path_traversal(self):
        with self.assertRaises(ValueError):
            self.inv.read_file("../../etc/passwd")

    def test_heuristic_findings_cover_spike_and_errors(self):
        logs = {"log_lines": 0, "levels": {}, "exceptions": [], "warnings": [], "gc": {"count": 0, "total_pause_ms": 0, "max_pause_ms": 0, "full_gc": 0, "heap_after_max_mb": 0}}
        bundle = {"commit": {"changed_files": []}}
        out = correlate.correlate_heuristic(self.inv, bundle, self.analysis, logs)
        cats = {f["category"] for f in out["findings"]}
        self.assertTrue({"latency", "errors"} <= cats)
        self.assertTrue(out["improvements"])
        spike = next(f for f in out["findings"] if f["category"] == "latency")
        self.assertTrue(spike["code_refs"] and spike["code_refs"][0]["file"].endswith("CatalogController.java"))


class ReportTests(unittest.TestCase):
    def test_report_renders_and_escapes(self):
        recs, t0 = make_records(spike=(30, 40, 600))
        a = analyze.analyze(recs, make_metrics(t0), CFG)
        R = {"run_id": "20261010-010101", "verdict": "attention", "duration_s": 99, "summary": "ok <b>x</b>",
             "commit": {"sha": "a" * 40, "short": "aaaaaaaa", "subject": "<script>alert(1)</script>", "date": "", "repo": "https://github.com/mybatis/jpetstore-6", "changed_files": []},
             "smoke": {"passed": True, "requests": 20, "failures": 0, "p95_ms": 50, "reasons": [], "top_failures": []}, "heal": None,
             "analysis": a, "logs": {"log_lines": 1, "levels": {"INFO": 1}, "exceptions": [], "warnings": [], "gc": {"count": 0}},
             "findings": [{"title": "t <img onerror=x>", "severity": "high", "category": "latency", "evidence": ["e"], "suspected_cause": "c",
                           "code_refs": [{"file": "src/A.java", "line": 3, "note": "n"}], "recommendation": "r"}],
             "improvements": [{"priority": 1, "action": "do it", "expected_impact": "faster", "effort": "low"}],
             "load": {"users": 25, "duration_s": 60}, "llm_mode": "off",
             "tokens": {"total": 0, "by_step": {}, "skipped": [{"step": "heal", "reason": "not needed", "est_tokens_saved": 1234}]},
             "trace": [{"key": "pull", "title": "Pull", "kind": "code", "status": "ok", "detail": "", "duration_s": 1.0}]}
        html = report.build_html(R)
        self.assertIn("<svg", html)
        self.assertNotIn("<script>alert", html)                # XSS-safe
        self.assertNotIn("<img onerror", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("blob/" + "a" * 40 + "/src/A.java#L3", html)
        self.assertIn("1234 tokens", html)
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))   # self-contained: no external resources


if __name__ == "__main__":
    unittest.main()
