"""A repaired script that passes the smoke test may have silently dropped journeys (the real model once rewrote a 14-endpoint
script into a 3-endpoint one, which hid the checkout race). That must be flagged, never promoted to 'known good'."""
import unittest

from tests.helpers import CFG, make_metrics, make_records
from agent.skills import analyze, heal, report, smoke


class CoverageGuardTests(unittest.TestCase):
    def test_a_much_thinner_script_is_flagged(self):
        self.assertTrue(heal.coverage_reduced(3, 14))                # the real incident
        self.assertTrue(heal.coverage_reduced(8, 14))                # 8 < 60% of 14
        self.assertFalse(heal.coverage_reduced(9, 14))
        self.assertFalse(heal.coverage_reduced(14, 14))

    def test_no_history_means_nothing_to_compare(self):
        self.assertFalse(heal.coverage_reduced(3, 0))

    def test_smoke_result_counts_distinct_endpoints(self):
        recs, _ = make_records()
        self.assertEqual(smoke.evaluate(recs, {"min_requests": 1, "max_error_rate": 1, "p95_ms_limit": 9999})["endpoint_count"], 3)

    def test_the_report_warns_about_a_thin_repair(self):
        recs, t0 = make_records()
        a = analyze.analyze(recs, make_metrics(t0), CFG)
        base = {"run_id": "20261011-000000", "verdict": "attention", "duration_s": 9, "summary": "s", "target": {"name": "app"},
                "commit": {"sha": "a" * 40, "short": "aaaaaaaa", "subject": "x", "date": "", "repo": "", "changed_files": []},
                "smoke": {"passed": True, "requests": 20, "failures": 0, "p95_ms": 5, "reasons": [], "top_failures": []}, "analysis": a,
                "logs": {"log_lines": 1, "levels": {}, "exceptions": [], "warnings": [], "gc": {"count": 0}}, "findings": [], "improvements": [],
                "load": {"users": 5, "duration_s": 9}, "llm_mode": "m", "tokens": {"total": 0, "by_step": {}, "skipped": []}, "trace": []}
        thin = {"healed": True, "attempts": 1, "history": [], "diff": "", "summary": "x", "endpoint_count": 3, "baseline_endpoints": 14, "reduced_coverage": True}
        html = report.build_html({**base, "heal": thin})
        self.assertIn("Coverage warning", html)
        self.assertIn("exercises 3 endpoints", html)
        self.assertNotIn("Coverage warning", report.build_html({**base, "heal": {**thin, "reduced_coverage": False}}))


if __name__ == "__main__":
    unittest.main()
