import json
import unittest

from tests.helpers import CFG, make_metrics, make_records
from agent.skills import analyze


class AnalyzeTests(unittest.TestCase):
    def test_healthy_run_has_no_findings(self):
        recs, t0 = make_records()
        a = analyze.analyze(recs, make_metrics(t0), CFG)
        self.assertEqual(a["verdict"], "healthy")
        self.assertEqual(a["overall"]["requests"], 1200)
        self.assertAlmostEqual(a["overall"]["rps"], 20, delta=1)
        self.assertFalse(a["spikes"])
        self.assertFalse(analyze.needs_investigation(a))

    def test_steady_state_spike_is_detected_and_attributed(self):
        recs, t0 = make_records(spike=(30, 40, 600))
        a = analyze.analyze(recs, make_metrics(t0), CFG)
        self.assertEqual(a["verdict"], "attention")
        self.assertEqual(len(a["spikes"]), 1)
        s = a["spikes"][0]
        self.assertEqual(s["phase"], "steady-state")
        self.assertTrue(28 <= s["start_rel"] <= 32 and 38 <= s["end_rel"] <= 42)
        self.assertEqual(s["top_endpoints"][0]["name"], "GET /catalog/viewProduct")
        self.assertGreater(s["peak_p95"], 400)

    def test_error_clusters_and_rate(self):
        recs, t0 = make_records(fail_endpoint="GET /cart")
        a = analyze.analyze(recs, make_metrics(t0), CFG)
        self.assertGreater(a["overall"]["error_rate"], 0.01)
        c = a["error_clusters"][0]
        self.assertEqual((c["endpoint"], c["status"]), ("GET /cart", "500"))
        self.assertTrue(analyze.needs_investigation(a))

    def test_baseline_regression_detected(self):
        recs, t0 = make_records()
        base = {"GET /catalog/viewProduct": {"p95": 20.0}, "GET /cart": {"p95": 80.0}}
        a = analyze.analyze(recs, make_metrics(t0), CFG, base)
        self.assertEqual([r["endpoint"] for r in a["regressions"]], ["GET /catalog/viewProduct"])

    def test_slower_on_the_same_commit_is_variance_not_a_regression(self):
        recs, t0 = make_records()
        base = {"GET /catalog/viewProduct": {"p95": 20.0}, "GET /cart": {"p95": 80.0}}
        a = analyze.analyze(recs, make_metrics(t0), CFG, base, same_code=True)
        self.assertEqual(a["regressions"], [])
        self.assertEqual([v["endpoint"] for v in a["variance"]], ["GET /catalog/viewProduct"])
        self.assertEqual(a["verdict"], "healthy")                    # runner noise must not raise an alarm

    def test_cpu_saturation_flags_attention(self):
        recs, t0 = make_records()
        a = analyze.analyze(recs, make_metrics(t0, cpu=95), CFG)
        self.assertEqual(a["verdict"], "attention")
        self.assertGreaterEqual(a["resources"]["cpu_saturated_s"], 10)

    def test_empty_input(self):
        a = analyze.analyze([], [], CFG)
        self.assertEqual(a["verdict"], "attention")

    def test_evidence_bundle_is_small(self):
        recs, t0 = make_records(spike=(30, 40, 600), fail_endpoint="GET /cart")
        a = analyze.analyze(recs, make_metrics(t0), CFG)
        git = {"short": "abc1234", "subject": "x", "changed_files": ["a.java"] * 30, "commits_since": ["c"] * 30}
        logs = {"log_lines": 500, "levels": {}, "exceptions": [], "warnings": [], "gc": {"count": 3}}
        text = json.dumps(analyze.evidence_bundle(a, git, logs, {}))
        self.assertLess(len(text), 6000, "the LLM evidence bundle must stay compact (token budget)")


if __name__ == "__main__":
    unittest.main()
