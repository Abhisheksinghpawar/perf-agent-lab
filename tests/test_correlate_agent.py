"""The correlation agent with a misbehaving model: prose instead of JSON, running out of turns, provider errors."""
import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import CFG, make_metrics, make_records, make_repo
from agent.context import RunContext
from agent.llm import LLM, LLMUnavailable
from agent.skills import correlate, report

GOOD = {"summary": "s", "findings": [{"title": "t", "severity": "high"}], "improvements": []}


def agent_for(replies):
    sent = []

    def transport(payload):
        sent.append(payload)
        r = replies[min(len(sent) - 1, len(replies) - 1)]
        if isinstance(r, Exception):
            raise r
        return {"usage": {"prompt_tokens": 10, "completion_tokens": 5}, "choices": [{"message": {"content": r}}]}

    ctx = RunContext(CFG, Path(tempfile.mkdtemp()))
    recs, t0 = make_records()
    inv = correlate.Investigator(str(make_repo()), {"spikes": []}, {"events": [], "exceptions": [], "gc": [], "lines": 0}, make_metrics(t0), t0)
    return LLM(CFG, ctx, transport=transport), inv, sent


class CorrelateAgentTests(unittest.TestCase):
    def test_prose_is_answered_with_a_request_for_json(self):
        llm, inv, sent = agent_for(["I looked at the logs and the checkout is racing.", json.dumps(GOOD)])
        out = correlate.correlate_agent(llm, inv, {}, "shop")
        self.assertEqual(out["findings"][0]["title"], "t")
        self.assertEqual(len(sent), 2)
        self.assertTrue(any("not the required JSON" in m["content"] for m in sent[-1]["messages"] if m["role"] == "user"))

    def test_json_in_a_code_fence_is_accepted_without_nudging(self):
        llm, inv, sent = agent_for(["```json\n" + json.dumps(GOOD) + "\n```"])
        self.assertIsNotNone(correlate.correlate_agent(llm, inv, {}, "shop"))
        self.assertEqual(len(sent), 1)

    def test_persistent_prose_gives_up_and_records_why(self):
        llm, inv, sent = agent_for(["no json here"])
        self.assertIsNone(correlate.correlate_agent(llm, inv, {}, "shop"))
        self.assertEqual(len(sent), 3)                                              # 1 answer + 2 nudges, then stop
        self.assertIn("not the required JSON", llm.last_error)
        self.assertIn("no json here", llm.last_error)

    def test_provider_errors_fall_back_with_the_reason(self):
        llm, inv, _ = agent_for([LLMUnavailable("LLM unreachable after retries (HTTP 429)", "")])
        self.assertIsNone(correlate.correlate_agent(llm, inv, {}, "shop"))
        self.assertIn("429", llm.last_error)

    def test_the_prompt_limits_tool_use_and_names_the_app(self):
        llm, inv, sent = agent_for([json.dumps(GOOD)])
        correlate.correlate_agent(llm, inv, {}, "orders-service")
        system = sent[0]["messages"][0]["content"]
        self.assertIn("orders-service", system)
        self.assertIn("at most 5 tool calls", system)
        self.assertEqual(sent[0]["max_tokens"], 3000)


class HonestLabelTests(unittest.TestCase):
    def render(self, tokens, by):
        from agent.skills import analyze
        recs, t0 = make_records()
        R = {"run_id": "20261011-000000", "verdict": "attention", "duration_s": 9, "summary": "s", "target": {"name": "app"}, "analysis_by": by,
             "commit": {"sha": "a" * 40, "short": "aaaaaaaa", "subject": "x", "date": "", "repo": "", "changed_files": []},
             "smoke": {"passed": True, "requests": 20, "failures": 0, "p95_ms": 5, "reasons": [], "top_failures": []}, "heal": None,
             "analysis": analyze.analyze(recs, make_metrics(t0), CFG), "logs": {"log_lines": 1, "levels": {}, "exceptions": [], "warnings": [], "gc": {"count": 0}},
             "findings": [], "improvements": [], "load": {"users": 5, "duration_s": 9}, "llm_mode": "some-model",
             "tokens": {"total": tokens, "by_step": {}, "skipped": []}, "trace": []}
        return report.build_html(R)

    def test_report_says_who_wrote_the_findings(self):
        self.assertIn("findings by the rule-based fallback", self.render(24000, "rules"))
        self.assertIn("findings by the LLM agent", self.render(24000, "llm"))
        html = self.render(0, "rules")
        self.assertNotIn("findings by", html)                                       # the LLM was never involved




class ExtractJsonTests(unittest.TestCase):
    def test_fenced_json_is_found(self):
        from agent.llm import extract_json
        fence = "`" * 3
        self.assertEqual(extract_json(f"{fence}json\n{{\"a\": 1}}\n{fence}"), {"a": 1})
        self.assertEqual(extract_json(f"Here you go:\n{fence}\n{{\"a\": 2}}\n{fence}\nHope it helps"), {"a": 2})
        self.assertEqual(extract_json('chatter {"a": 3} chatter'), {"a": 3})
        self.assertIsNone(extract_json("no json at all"))


if __name__ == "__main__":
    unittest.main()
