"""Regression tests built from a REAL stack trace captured from JPetStore under 250 concurrent users
(tests/fixtures/real_duplicate_key.log): duplicate order ids -> PK_ORDERS violation -> HTTP 500."""
import time
import unittest
from pathlib import Path

from tests.helpers import CFG, make_metrics, make_records
from agent.skills import analyze, correlate, logs

FIXTURE = Path(__file__).parent / "fixtures" / "real_duplicate_key.log"


def make_service(frame_line: int) -> str:
    """OrderService.java with the id generation 10 lines above the failing insert (which sits on `frame_line`)."""
    lines = ["// filler"] * (frame_line + 5)
    lines[0] = "package org.mybatis.jpetstore.service;"
    lines[frame_line - 11] = '    order.setOrderId(getNextId("ordernum"));'
    lines[frame_line - 1] = "    orderMapper.insertOrder(order);"
    return "\n".join(lines) + "\n"



class RealLogTests(unittest.TestCase):
    def setUp(self):
        self.parsed = logs.parse(FIXTURE)

    def test_exception_and_application_frame_are_extracted(self):
        self.assertGreaterEqual(len(self.parsed["exceptions"]), 1)
        x = self.parsed["exceptions"][0]
        self.assertEqual(x["type"], "HsqlException")
        self.assertIn("PK_ORDERS", x["msg"])
        g = logs.group_exceptions(self.parsed["exceptions"], "org.mybatis.jpetstore")[0]
        self.assertEqual(g["app_frames"][0]["method"], "OrderService.insertOrder")
        self.assertEqual(g["app_frames"][0]["file"], "src/main/java/org/mybatis/jpetstore/service/OrderService.java")
        self.assertGreater(g["app_frames"][0]["line"], 0)

    def test_mybatis_sql_context_is_captured(self):
        ctx = " ".join(logs.group_exceptions(self.parsed["exceptions"], "org.mybatis.jpetstore")[0]["context"])
        self.assertIn("INSERT INTO ORDERS", ctx)

    def test_summary_is_compact_and_deduplicated(self):
        t0 = min(e["ts"] for e in self.parsed["events"]) - 1
        s = logs.summarize(self.parsed, t0, t0 + 300)
        self.assertEqual(s["levels"].get("SEVERE", 0) >= 1, True)
        self.assertLessEqual(len(s["warnings"]), 5)
        self.assertLess(len(str(s)), 3000)

    def test_rule_based_correlation_finds_the_race_condition_lead(self):
        recs, t0 = make_records(fail_endpoint="GET /cart")
        for r in recs:                                   # re-label the failing endpoint like the real run
            if r["name"] == "GET /cart":
                r["name"] = "POST /order/new (confirm)"
        metrics = make_metrics(t0)
        a = analyze.analyze(recs, metrics, CFG)
        # move the real log's events onto this synthetic timeline
        shift = t0 + 35 - self.parsed["exceptions"][0]["ts"]
        for group in ("events", "exceptions", "gc"):
            for row in self.parsed[group]:
                row["ts"] += shift
        import tempfile
        root = Path(tempfile.mkdtemp())
        d = root / "src/main/java/org/mybatis/jpetstore/service"
        d.mkdir(parents=True)
        frame = logs.group_exceptions(self.parsed["exceptions"], "org.mybatis.jpetstore")[0]["app_frames"][0]["line"]
        (d / "OrderService.java").write_text(make_service(frame), encoding="utf-8")
        c = root / "src/main/java/org/mybatis/jpetstore/web/controllers"
        c.mkdir(parents=True)
        (c / "OrderController.java").write_text('@RequestMapping("/order")\npublic class OrderController {\n  @PostMapping("/new")\n  public String x() { return null; }\n}\n', encoding="utf-8")
        inv = correlate.Investigator(str(root), a, self.parsed, metrics, t0)
        lg = logs.summarize(self.parsed, t0, t0 + 60)
        out = correlate.correlate_heuristic(inv, {"commit": {"changed_files": []}}, a, lg)
        f = next(x for x in out["findings"] if x["category"] == "errors")
        self.assertIn("primary key", f["suspected_cause"])
        files = [r["file"].rsplit("/", 1)[-1] for r in f["code_refs"]]
        self.assertIn("OrderService.java", files)
        self.assertTrue(any("atomic" in r.get("note", "") for r in f["code_refs"]), f["code_refs"])
        self.assertTrue(any("HsqlException" in e for e in f["evidence"]))
        self.assertIn("atomically", f["recommendation"])


if __name__ == "__main__":
    unittest.main()
