"""Guard rails around an LLM's correlation answer. The fixture is the REAL output of openai/gpt-oss-20b from the first
successful agent run: it found the real bug, but also reported two few-millisecond latency differences as findings and
ranked 'move the load generator' above the bug fix."""
import unittest

from agent.skills.correlate import tidy_llm_result

REAL = {
    "summary": "The test revealed a recurring duplicate-order-ID error on POST /order/new (confirm) and elevated latency on the sign-on and catalog view endpoints.",
    "findings": [
        {"title": "Elevated latency on POST /account/signon", "severity": "medium", "category": "latency", "endpoint": "POST /account/signon",
         "evidence": ["p95 26.2 ms vs baseline 17.9 ms"], "suspected_cause": "CPU contention", "recommendation": "Move the load generator"},
        {"title": "Duplicate Order ID causing 500 errors on POST /order/new (confirm)", "severity": "high", "category": "errors", "endpoint": "POST /order/new (confirm)",
         "evidence": ["12 HsqlException errors: integrity constraint violation PK_ORDERS"], "suspected_cause": "Race condition on getNextId(\"ordernum\")",
         "code_refs": [{"file": "OrderService.java", "line": 85}], "recommendation": "Switch to a database-generated identity for ORDERID or serialize sequence access"},
        {"title": "Elevated latency on catalog view endpoints", "severity": "medium", "category": "latency", "endpoint": "GET /catalog/viewProduct",
         "evidence": ["p95 15.9 ms vs baseline 8.6 ms"], "suspected_cause": "Database query performance or GC pressure", "recommendation": "Add indexes"},
    ],
    "improvements": [
        {"priority": 1, "action": "Deploy the load generator on a separate machine or reduce its load to eliminate CPU contention", "expected_impact": "Reduction in latency for sign-on and catalog endpoints", "effort": "medium"},
        {"priority": 2, "action": "Modify OrderService to use database auto-increment for ORDERID or lock the sequence table during ID generation", "expected_impact": "Eliminate 500 errors from duplicate order IDs", "effort": "low"},
        {"priority": 3, "action": "Profile and optimize catalog view queries, adding missing indexes", "expected_impact": "Reduce p95/p99 latency for catalog endpoints", "effort": "medium"},
    ],
}
QUIET = {"regressions": [], "spikes": [], "variance": [{"endpoint": "POST /account/signon"}, {"endpoint": "GET /catalog/viewProduct"}]}


class GuardRailTests(unittest.TestCase):
    def test_noise_findings_are_dropped_and_the_real_bug_leads(self):
        out = tidy_llm_result(REAL, QUIET)
        self.assertEqual([f["title"][:9] for f in out["findings"]], ["Duplicate"])
        self.assertEqual(out["dropped"], 2)
        self.assertIn("order", out["improvements"][0]["action"].lower())          # the fix for the bug is now #1
        self.assertEqual(len(out["improvements"]), 1)                              # the load-generator and index advice came from dropped findings
        self.assertEqual(out["improvements"][0]["priority"], 1)
        self.assertIn("2 small latency difference(s)", out["summary"])
        self.assertNotIn("elevated latency", out["summary"].lower())             # the model's own summary no longer claims it

    def test_improvements_stay_when_nothing_was_dropped(self):
        data = {"summary": "x", "findings": [REAL["findings"][1]], "improvements": REAL["improvements"]}
        out = tidy_llm_result(data, QUIET)
        self.assertEqual(len(out["improvements"]), 3)
        self.assertIn("OrderService", out["improvements"][0]["action"])

    def test_the_live_run_case_order_and_orders_match(self):
        data = {"summary": "x", "improvements": [
            {"action": "Cache favourite category product list during signon", "effort": "medium"},
            {"action": "Implement robust unique ID generation for orders", "effort": "high"},
            {"action": "Add caching and indexing for catalog view endpoints", "effort": "medium"}],
            "findings": [REAL["findings"][1], REAL["findings"][0], REAL["findings"][2]]}
        out = tidy_llm_result(data, QUIET)
        self.assertEqual([i["action"] for i in out["improvements"]], ["Implement robust unique ID generation for orders"])

    def test_a_flagged_regression_is_kept(self):
        analysis = {"regressions": [{"endpoint": "POST /account/signon"}], "spikes": [], "variance": []}
        out = tidy_llm_result(REAL, analysis)
        titles = [f["title"] for f in out["findings"]]
        self.assertIn("Elevated latency on POST /account/signon", titles)
        self.assertNotIn("Elevated latency on catalog view endpoints", titles)    # not flagged by the analysis
        self.assertEqual(out["findings"][0]["severity"], "high")                  # severity order

    def test_a_steady_state_spike_counts_as_flagged_but_warm_up_does_not(self):
        spike = {"phase": "steady-state", "top_endpoints": [{"name": "GET /catalog/viewProduct"}]}
        self.assertEqual(tidy_llm_result(REAL, {"regressions": [], "spikes": [spike]})["dropped"], 1)
        warm = {**spike, "phase": "warm-up"}
        self.assertEqual(tidy_llm_result(REAL, {"regressions": [], "spikes": [warm]})["dropped"], 2)

    def test_nothing_dropped_leaves_the_answer_alone(self):
        data = {"summary": "keep me", "findings": [REAL["findings"][1]], "improvements": []}
        out = tidy_llm_result(data, QUIET)
        self.assertEqual((out["summary"], out["dropped"], len(out["findings"])), ("keep me", 0, 1))

    def test_all_noise_means_no_findings(self):
        data = {"summary": "x", "findings": [REAL["findings"][0]], "improvements": []}
        out = tidy_llm_result(data, QUIET)
        self.assertEqual(out["findings"], [])
        self.assertIn("No errors", out["summary"])


if __name__ == "__main__":
    unittest.main()
