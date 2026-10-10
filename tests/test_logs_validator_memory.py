import tempfile
import time
from datetime import datetime
import unittest
from pathlib import Path

from tests.helpers import make_repo
from agent.memory import Memory
from agent.skills import logs
from agent.skills.heal import validate_locustfile

GOOD = '''
from locust import HttpUser, task, between
import recorder
import random

class U(HttpUser):
    wait_time = between(1, 2)
    @task
    def home(self):
        self.client.get("/actions/Catalog.action")
'''


class LogTests(unittest.TestCase):
    def test_parse_and_summarize(self):
        base = time.mktime((2026, 10, 10, 8, 0, 0, 0, 0, -1))
        text = "\n".join([
            "[INFO] [talledLocalContainer] 10-Oct-2026 08:00:05.100 INFO [main] org.apache.catalina.startup.Catalina.start Server startup in [4000] milliseconds",
            "[INFO] [talledLocalContainer] 10-Oct-2026 08:00:20.250 SEVERE [http-nio-8080-exec-3] org.apache.catalina.core.StandardWrapperValve.invoke Servlet.service() threw exception",
            "java.lang.NullPointerException: item was null",
            "\tat org.mybatis.jpetstore.web.actions.CartActionBean.addItemToCart(CartActionBean.java:61)",
            "\tat sun.reflect.X.invoke(X.java:1)",
            # the JVM prints GC times in LOCAL time with a UTC offset
            "[" + datetime.fromtimestamp(base + 21.5).astimezone().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + datetime.fromtimestamp(base + 21.5).astimezone().strftime("%z") + "][21.5s] GC(7) Pause Young (Normal) (G1 Evacuation Pause) 120M->30M(256M) 85.500ms",
        ])
        p = Path(tempfile.mkdtemp()) / "app.log"
        p.write_text(text, encoding="utf-8")
        parsed = logs.parse(p)
        self.assertEqual(len(parsed["exceptions"]), 1)
        self.assertEqual(parsed["exceptions"][0]["type"], "NullPointerException")
        self.assertIn("CartActionBean.addItemToCart", parsed["exceptions"][0]["trace"][0])
        self.assertEqual(len(parsed["gc"]), 1)
        self.assertEqual(parsed["gc"][0]["pause_ms"], 85.5)
        s = logs.summarize(parsed, base, base + 60)
        self.assertEqual(s["exceptions"][0]["type"], "NullPointerException")
        self.assertEqual(s["gc"]["max_pause_ms"], 85.5)
        self.assertTrue(any("SEVERE" in line for line in logs.window(parsed, base, base + 60)))

    def test_missing_log_is_not_fatal(self):
        self.assertEqual(logs.parse(Path("/nonexistent/app.log"))["lines"], 0)


class ValidatorTests(unittest.TestCase):
    def test_good_file_passes(self):
        self.assertEqual(validate_locustfile(GOOD), [])

    def test_rejects_dangerous_constructs(self):
        cases = {
            "import os": GOOD + "\nimport os\n",
            "subprocess": GOOD + "\nimport subprocess\n",
            "eval": GOOD + "\nx = eval('1')\n",
            "open": GOOD + "\nopen('/etc/passwd')\n",
            "absolute url": GOOD.replace('"/actions/Catalog.action"', '"http://evil.example/x"'),
            "dunder": GOOD + "\nx = ().__class__\n",
            "no recorder": GOOD.replace("import recorder", ""),
            "no user class": "import recorder\nx = 1\n",
            "syntax": "def (:\n",
        }
        for label, src in cases.items():
            with self.subTest(label):
                self.assertTrue(validate_locustfile(src), f"{label} should be rejected")

    def test_size_limit(self):
        self.assertTrue(validate_locustfile(GOOD + "# " + "x" * 20000))


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.m = Memory(Path(tempfile.mkdtemp()))

    def test_state_and_baseline_median(self):
        self.assertIsNone(self.m.last_sha())
        self.m.save_state("abc", "run1", "healthy")
        self.assertEqual(self.m.last_sha(), "abc")
        for i, v in enumerate([100, 110, 400]):
            self.m.add_baseline_run(f"r{i}", {"ep": v})
        self.assertEqual(self.m.baseline()["ep"]["p95"], 110)          # median, robust to one bad run

    def test_known_issue_recurrence(self):
        f = {"title": "slow cart", "endpoint": "Cart.action", "category": "latency"}
        first = self.m.record_findings([dict(f)], "r1")[0]
        again = self.m.record_findings([dict(f)], "r2")[0]
        self.assertFalse(first["recurring"])
        self.assertTrue(again["recurring"])
        self.assertEqual(again["seen_count"], 2)

    def test_good_locustfile_roundtrip(self):
        self.assertIsNone(self.m.good_locustfile())
        self.m.save_good_locustfile(GOOD)
        self.assertEqual(self.m.good_locustfile(), GOOD)

    def test_token_stats(self):
        self.m.update_token_stats({"heal": {"prompt": 100, "completion": 20, "calls": 2}})
        self.assertEqual(self.m.token_stats()["heal"], 120)


if __name__ == "__main__":
    unittest.main()
