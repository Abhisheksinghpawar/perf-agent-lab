import json
import socket
import tempfile
import unittest
from pathlib import Path
from urllib import request as urlrequest

from tests.helpers import CFG
from agent import demo_llm, doctor
from agent.config import ROOT
from agent.skills.app_env import App


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DoctorTests(unittest.TestCase):
    def test_checks_have_status_and_fixes(self):
        rows = doctor.run_checks(CFG)
        self.assertTrue(rows)
        for status, name, detail, fix in rows:
            self.assertIn(status, (doctor.OK, doctor.FAIL, doctor.WARN))
            if status != doctor.OK:
                self.assertTrue(fix, f"'{name}' fails without telling the user how to fix it")
        self.assertTrue(any("Python" in r[1] and r[0] == doctor.OK for r in rows))

    def test_port_in_use_is_reported(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            self.assertFalse(doctor._port_free(s.getsockname()[1]))


class PortGuardTests(unittest.TestCase):
    def test_start_refuses_when_port_is_busy_and_launches_nothing(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            cfg = json.loads(json.dumps(CFG))
            cfg["target"]["base_url"] = f"http://localhost:{s.getsockname()[1]}/jpetstore"
            app = App(cfg, tempfile.mkdtemp(), Path(tempfile.mkdtemp()))
            with self.assertRaises(RuntimeError) as cm:
                app.start()
            self.assertIn("already in use", str(cm.exception))
            self.assertIsNone(app.proc)                       # nothing was spawned


class ScriptedDemoServerTests(unittest.TestCase):
    def test_server_speaks_the_openai_tool_calling_protocol(self):
        port = _free_port()
        server = demo_llm.start_in_thread(port, str(ROOT / "locust" / "locustfile.py"))
        try:
            body = {"messages": [{"role": "system", "content": "You repair a Locust load-test script"}, {"role": "user", "content": "go"}]}
            req = urlrequest.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            msg = json.load(urlrequest.urlopen(req, timeout=10))["choices"][0]["message"]
            self.assertEqual(msg["tool_calls"][0]["function"]["name"], "read_locustfile")
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
