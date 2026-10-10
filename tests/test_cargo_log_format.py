import tempfile
import time
import unittest
from pathlib import Path

from agent.skills import logs


class CargoLogFormatTests(unittest.TestCase):
    """The format Tomcat/cargo really prints: a date line, then 'LEVEL: message' (12-hour clock, cargo's [INFO] prefix)."""

    def test_java_simpleformatter_two_line_format(self):
        text = "\n".join([
            "[INFO] Oct 10, 2026 5:51:04 PM org.apache.coyote.AbstractProtocol start",
            '[INFO] INFO: Starting ProtocolHandler ["http-nio-8080"]',
            "[INFO] Oct 10, 2026 5:52:30 PM org.apache.catalina.core.StandardWrapperValve invoke",
            "[INFO] SEVERE: Servlet.service() for servlet [dispatcher] threw exception",
            "[INFO] java.lang.IllegalStateException: pool exhausted",
            "[INFO] \tat org.mybatis.jpetstore.service.OrderService.insertOrder(OrderService.java:88)",
        ])
        p = Path(tempfile.mkdtemp()) / "app.log"
        p.write_text(text, encoding="utf-8")
        parsed = logs.parse(p)
        levels = [(e["level"], e["source"]) for e in parsed["events"]]
        self.assertIn(("SEVERE", "org.apache.catalina.core.StandardWrapperValve.invoke"), levels)
        t_sev = next(e["ts"] for e in parsed["events"] if e["level"] == "SEVERE")
        self.assertEqual(time.strftime("%H:%M:%S", time.localtime(t_sev)), "17:52:30")     # PM converted to 24 h
        self.assertEqual(parsed["exceptions"][0]["type"], "IllegalStateException")
        self.assertIn("OrderService.insertOrder", parsed["exceptions"][0]["trace"][0])


if __name__ == "__main__":
    unittest.main()
