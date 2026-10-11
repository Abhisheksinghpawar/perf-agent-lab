import json
import unittest

from agent.skills.publish import summary_of

FINDING = {"title": "10 failed request(s) on POST /order/new (confirm)", "severity": "medium", "category": "errors",
           "endpoint": "POST /order/new (confirm)", "evidence": ["HTTP 500 x10", "HsqlException: PK_ORDERS " + "x" * 400, "third"],
           "suspected_cause": "non-atomic id generation", "recommendation": "Allocate ids atomically.",
           "code_refs": [{"file": "src/main/java/x/OrderService.java", "line": 85, "note": "insertOrder"},
                         {"file": "src/main/java/x/OrderService.java", "line": 75, "note": "id generated here"},
                         {"file": "src/main/java/x/OrderController.java", "line": 98, "note": "third"}]}


def make_run(findings):
    return {"run_id": "20261010-000000", "finished_at": "2026-10-10T00:00:00+00:00", "verdict": "attention", "duration_s": 100,
            "commit": {"sha": "a" * 40, "short": "aaaaaaaa", "subject": "s", "date": "", "repo": "https://github.com/x/y"},
            "smoke": {"passed": True, "requests": 80}, "heal": None, "load": {"users": 250, "duration_s": 60},
            "analysis": {"overall": {"requests": 100, "rps": 5, "error_rate": 0.1, "p50": 1, "p95": 2, "p99": 3}, "resources": {"cpu_avg": 30}},
            "findings": findings, "improvements": [{"priority": 1, "action": "Allocate ids atomically.", "effort": "medium"}],
            "summary": "1 item need attention", "tokens": {"total": 0, "skipped": []}, "llm_mode": "off (deterministic)"}


class SummaryTests(unittest.TestCase):
    def test_top_finding_and_improvements_are_published_compactly(self):
        s = summary_of(make_run([FINDING]))
        tf = s["top_finding"]
        self.assertEqual(tf["endpoint"], "POST /order/new (confirm)")
        self.assertEqual(len(tf["evidence"]), 2)                       # trimmed for the portfolio
        self.assertLessEqual(max(len(e) for e in tf["evidence"]), 170)
        self.assertEqual([r["line"] for r in tf["code_refs"]], [85, 75])
        self.assertEqual(s["improvements"][0]["action"], "Allocate ids atomically.")
        self.assertLess(len(json.dumps(s)), 4000)

    def test_healthy_run_has_no_top_finding(self):
        self.assertIsNone(summary_of(make_run([]))["top_finding"])


if __name__ == "__main__":
    unittest.main()
