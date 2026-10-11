import tempfile
import unittest
from pathlib import Path

from tests.helpers import CFG, make_metrics, make_records
from agent.memory import Memory
from agent.skills import correlate


class BaselineAndRecurrenceTests(unittest.TestCase):
    def setUp(self):
        self.m = Memory(Path(tempfile.mkdtemp()))

    def test_baseline_is_per_load_level(self):
        self.m.add_baseline_run("r1", {"ep": 8.0}, users=25)
        self.m.add_baseline_run("r2", {"ep": 140.0}, users=250)
        self.assertEqual(self.m.baseline(25)["ep"]["p95"], 8.0)
        self.assertEqual(self.m.baseline(250)["ep"]["p95"], 140.0)
        self.assertEqual(self.m.baseline(100), {})                       # never compare against a different load

    def test_baseline_remembers_the_commits_it_was_recorded_on(self):
        self.m.add_baseline_run("r1", {"ep": 8.0}, users=250, sha="abc")
        self.m.add_baseline_run("r2", {"ep": 9.0}, users=250, sha="abc")
        self.m.add_baseline_run("r3", {"ep": 7.0}, users=25, sha="old")
        self.assertEqual(self.m.baseline_commits(250), {"abc"})
        self.m.add_baseline_run("r4", {"ep": 9.0}, users=250)                 # recorded before commits were tracked
        self.assertEqual(self.m.baseline_commits(250), {"abc", None})

    def test_recurrence_survives_changing_numbers_in_titles(self):
        a = {"title": "126 failed request(s) on POST /order/new (confirm)", "endpoint": "POST /order/new (confirm)", "category": "errors"}
        b = {"title": "153 failed request(s) on POST /order/new (confirm)", "endpoint": "POST /order/new (confirm)", "category": "errors"}
        c = {"title": "GET /catalog is 2020% slower than baseline", "endpoint": "GET /catalog", "category": "regression"}
        d = {"title": "GET /catalog is 5421% slower than baseline", "endpoint": "GET /catalog", "category": "regression"}
        self.m.record_findings([a, c], "run1")
        out = self.m.record_findings([b, d], "run2")
        self.assertTrue(all(f["recurring"] for f in out))
        self.assertEqual({f["seen_count"] for f in out}, {2})
        self.assertEqual(len(self.m.known_issues()), 2)

    def test_different_endpoints_are_different_issues(self):
        self.m.record_findings([{"title": "5 failed request(s)", "endpoint": "A", "category": "errors"}], "r1")
        out = self.m.record_findings([{"title": "5 failed request(s)", "endpoint": "B", "category": "errors"}], "r2")
        self.assertFalse(out[0]["recurring"])


class RootHandlerTests(unittest.TestCase):
    def test_find_handler_for_the_root_mapping(self):
        root = Path(tempfile.mkdtemp()) / "repo"
        d = root / "src/main/java/org/x/web/controllers"
        d.mkdir(parents=True)
        (d / "CatalogController.java").write_text(
            'package x;\n@Controller\n@RequestMapping("/catalog")\npublic class CatalogController {\n'
            '  @GetMapping({ "", "/" })\n  public String main() { return null; }\n'
            '  @GetMapping("/viewCategory")\n  public String viewCategory() { return null; }\n}\n', encoding="utf-8")
        recs, t0 = make_records()
        inv = correlate.Investigator(str(root), {"spikes": []}, {"events": [], "exceptions": [], "gc": [], "lines": 0}, make_metrics(t0), t0)
        self.assertEqual(inv.find_handler("GET /catalog")["line"], 5)
        self.assertEqual(inv.find_handler("GET /catalog/viewCategory")["line"], 7)


if __name__ == "__main__":
    unittest.main()
