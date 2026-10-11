"""Everything that lets the pipeline test an app other than JPetStore: config merge, Python logs, route lookup,
local folders, command runner, report labels."""
import json
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.helpers import CFG, make_metrics, make_records
from agent import config as config_mod
from agent.skills import correlate, git_sync, logs, report
from agent.skills.app_env import App

PY_LOG = textwrap.dedent('''\
    2026-10-11 03:12:44,100 INFO shop shop listening on port 8081
    2026-10-11 03:12:50,250 ERROR shop checkout failed
    Traceback (most recent call last):
      File "C:\\work\\shop\\app.py", line 77, in do_POST
        order_id = place_order(1)
      File "C:\\work\\shop\\app.py", line 39, in place_order
        raise DuplicateOrderError(f"order id {order_id} already exists")
      File "C:\\Python311\\Lib\\json\\decoder.py", line 12, in decode
        pass
    DuplicateOrderError: order id 1022 already exists
    2026-10-11 03:12:50,260 ERROR shop checkout failed
    Traceback (most recent call last):
      File "C:\\work\\shop\\app.py", line 39, in place_order
        raise DuplicateOrderError(f"order id {order_id} already exists")
    DuplicateOrderError: order id 1031 already exists
    ''')


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ConfigMergeTests(unittest.TestCase):
    def test_a_tiny_config_inherits_every_default(self):
        p = Path(tempfile.mkdtemp()) / "mine.toml"
        p.write_text('[target]\nname = "mine"\nstart_cmd = "node server.js"\nbase_url = "http://127.0.0.1:3000"\n', encoding="utf-8")
        cfg = config_mod.load_config(p, ["load.users=7"])
        self.assertEqual((cfg["target"]["name"], cfg["target"]["runner"], cfg["load"]["users"]), ("mine", "command", 7))
        self.assertEqual(cfg["smoke"]["max_heal_attempts"], 3)                  # untouched default
        self.assertEqual(cfg["target"]["source_dirs"], ["src"])

    def test_bundled_configs_load(self):
        self.assertEqual(config_mod.load_config()["target"]["runner"], "maven-cargo")
        ex = config_mod.load_config(config_mod.ROOT / "examples" / "python-shop" / "config.toml")
        self.assertEqual((ex["target"]["runner"], ex["publish"]["memory_dir"]), ("command", "memory/python-shop"))
        self.assertTrue(config_mod.resolve(ex["target"]["locustfile"]).exists())

    def test_missing_config_is_a_clear_error(self):
        with self.assertRaises(SystemExit):
            config_mod.load_config("does-not-exist.toml")


class PythonLogTests(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        (d / "app.log").write_text(PY_LOG, encoding="utf-8")
        self.parsed = logs.parse(d / "app.log")

    def test_timestamps_levels_and_tracebacks_are_read(self):
        self.assertEqual([e["level"] for e in self.parsed["events"]], ["INFO", "ERROR", "ERROR"])
        self.assertEqual(len(self.parsed["exceptions"]), 2)
        x = self.parsed["exceptions"][0]
        self.assertEqual((x["type"], x["msg"]), ("DuplicateOrderError", "order id 1022 already exists"))
        self.assertIn("decoder.py", x["trace"][0])                               # innermost frame first, like Java

    def test_only_application_frames_are_blamed(self):
        frames = logs.app_frames(self.parsed["exceptions"][0]["trace"], "", root="C:/work/shop")
        self.assertEqual([(f["file"], f["line"]) for f in frames], [("app.py", 39), ("app.py", 77)])   # json/decoder.py is stdlib
        unix = logs.app_frames(['File "/srv/shop/app.py", line 39, in place_order', 'File "/usr/lib/python3.11/json/decoder.py", line 3, in decode'], "", root="/srv/shop")
        self.assertEqual([(f["file"], f["method"]) for f in unix], [("app.py", "app.place_order")])

    def test_changing_ids_in_the_message_do_not_split_the_group(self):
        t0 = self.parsed["events"][1]["ts"]
        s = logs.summarize(self.parsed, t0 - 1, t0 + 1, root="C:/work/shop", pkg="")
        self.assertEqual([(g["type"], g["count"]) for g in s["exceptions"]], [("DuplicateOrderError", 2)])

    def test_iso_timestamps_with_a_zone(self):
        d = Path(tempfile.mkdtemp()) / "a.log"
        d.write_text("2026-10-11T03:00:00.500Z WARN slow query\n2026-10-11T05:00:00+02:00 ERROR boom\n", encoding="utf-8")
        ev = logs.parse(d)["events"]
        self.assertAlmostEqual(ev[0]["ts"] - 0.5, ev[1]["ts"], delta=0.01)       # 03:00:00.5Z is 0.5 s after 05:00:00+02:00


class RouteLookupTests(unittest.TestCase):
    def make(self):
        root = Path(tempfile.mkdtemp())
        (root / "app.py").write_text('import x\n\ndef handler(path):\n    if path == "/health":\n        return 1\n    if path == "/checkout":\n        return 2\n', encoding="utf-8")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "dep.py").write_text('"/checkout"\n', encoding="utf-8")
        recs, t0 = make_records()
        return correlate.Investigator(str(root), {"spikes": []}, {"events": [], "exceptions": [], "gc": [], "lines": 0}, make_metrics(t0), t0,
                                      pkg="", dirs=["."], globs=["*.py"])

    def test_route_string_is_found_outside_spring(self):
        h = self.make().find_handler("POST /checkout")
        self.assertEqual((h["file"], h["line"]), ("app.py", 6))                     # not node_modules/dep.py

    def test_unknown_route_reports_an_error_not_a_crash(self):
        self.assertIn("error", self.make().find_handler("GET /nope"))

    def test_duplicate_style_exceptions_get_a_concurrency_explanation(self):
        inv = self.make()
        (Path(inv.root) / "shop.py").write_text("def place():\n    oid = next_order_id()\n    raise DuplicateOrderError('x')\n", encoding="utf-8")
        refs = [{"file": "shop.py", "line": 3, "note": "n"}]
        top = {"type": "DuplicateOrderError", "message": "order id 5 already exists", "context": [], "where": "", "app_frames": [{"file": "shop.py", "line": 3, "method": "shop.place"}]}
        cause, rec = correlate._explain_exception(top, inv, refs)
        self.assertIn("non-atomic", cause)
        self.assertIn("atomically", rec)
        self.assertEqual(refs[1]["line"], 2)                                         # points at the id generator


class LocalFolderTests(unittest.TestCase):
    def git(self, cwd, *a):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True, capture_output=True)

    def cfg(self, path):
        return {"target": {"local_path": str(path)}}

    def test_monorepo_subfolder_tracks_its_own_commits(self):
        top = Path(tempfile.mkdtemp())
        self.git(top, "init", "-q")
        app = top / "services" / "shop"
        app.mkdir(parents=True)
        (app / "a.py").write_text("v1\n", encoding="utf-8")
        (top / "other.txt").write_text("x\n", encoding="utf-8")
        self.git(top, "add", "-A"); self.git(top, "commit", "-qm", "first")
        a = git_sync.sync(self.cfg(app), top, None)
        self.assertEqual((a["subdir"], a["subject"], a["changed"]), ("services/shop", "first", True))
        (top / "other.txt").write_text("y\n", encoding="utf-8")                     # a change elsewhere in the repo ...
        self.git(top, "add", "-A"); self.git(top, "commit", "-qm", "unrelated")
        b = git_sync.sync(self.cfg(app), top, a["sha"])
        self.assertFalse(b["changed"])                                              # ... does not trigger a re-test of this app
        (app / "a.py").write_text("v2\n", encoding="utf-8")
        self.git(top, "add", "-A"); self.git(top, "commit", "-qm", "change shop")
        c = git_sync.sync(self.cfg(app), top, a["sha"])
        self.assertEqual((c["changed"], c["changed_files"]), (True, ["a.py"]))

    def test_uncommitted_edits_still_count_as_a_change(self):
        top = Path(tempfile.mkdtemp())
        self.git(top, "init", "-q")
        (top / "a.py").write_text("v1\n", encoding="utf-8")
        self.git(top, "add", "-A"); self.git(top, "commit", "-qm", "first")
        clean = git_sync.sync(self.cfg(top), top, None)["sha"]
        (top / "a.py").write_text("v2 edited\n", encoding="utf-8")
        self.assertNotEqual(git_sync.sync(self.cfg(top), top, None)["sha"], clean)

    def test_folder_without_git_is_fingerprinted(self):
        d = Path(tempfile.mkdtemp())
        (d / "app.js").write_text("1", encoding="utf-8")
        a = git_sync.sync(self.cfg(d), d, None)
        self.assertTrue(a["sha"].startswith("local+"))
        self.assertFalse(git_sync.sync(self.cfg(d), d, a["sha"])["changed"])
        (d / "app.js").write_text("22", encoding="utf-8")
        self.assertTrue(git_sync.sync(self.cfg(d), d, a["sha"])["changed"])

    def test_bad_inputs_have_clear_messages(self):
        with self.assertRaises(RuntimeError) as cm:
            git_sync.sync(self.cfg("no/such/folder"), Path("."), None)
        self.assertIn("local_path", str(cm.exception))
        with self.assertRaises(RuntimeError) as cm:
            git_sync.sync({"target": {"local_path": "", "repo_url": ""}}, Path("."), None)
        self.assertIn("repo_url", str(cm.exception))


class CommandRunnerTests(unittest.TestCase):
    def cfg(self, **target):
        port = free_port()
        base = json.loads(json.dumps(CFG))
        base["target"].update({"runner": "command", "base_url": f"http://127.0.0.1:{port}", "health_path": "/", "startup_timeout_s": 30,
                               "build_cmd": "", "start_cmd": "", "process_match": "", "log_file": "", **target})
        return base, port

    def test_build_is_skipped_without_a_build_command(self):
        cfg, _ = self.cfg()
        self.assertEqual(App(cfg, tempfile.mkdtemp(), Path(tempfile.mkdtemp())).build(), 0.0)

    def test_failing_build_names_the_command_output(self):
        cfg, _ = self.cfg(build_cmd='{python} -c "import sys; print(\'compiler says no\'); sys.exit(3)"')
        with self.assertRaises(RuntimeError) as cm:
            App(cfg, tempfile.mkdtemp(), Path(tempfile.mkdtemp())).build()
        self.assertIn("compiler says no", str(cm.exception))

    def test_missing_start_command_is_explained(self):
        cfg, _ = self.cfg()
        with self.assertRaises(RuntimeError) as cm:
            App(cfg, tempfile.mkdtemp(), Path(tempfile.mkdtemp())).start()
        self.assertIn("start_cmd", str(cm.exception))

    def test_start_sample_stop(self):
        workdir, out = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (workdir / "app.log").write_text("2026-10-11 03:00:00,000 ERROR from a log file\n", encoding="utf-8")
        cfg, port = self.cfg(start_cmd="{python} -u -m http.server {port} --bind 127.0.0.1", log_file="app.log")
        app = App(cfg, str(workdir), out)
        app.start()
        try:
            proc = app.app_process()
            self.assertIsNotNone(proc)
            self.assertIn("http.server", " ".join(proc.cmdline()))                  # the python process, not the shell
        finally:
            app.stop()
        with socket.socket() as s:
            self.assertNotEqual(s.connect_ex(("127.0.0.1", port)), 0)               # really stopped
        self.assertIn("from a log file", (out / "app.log").read_text(encoding="utf-8"))   # file logs are folded in


class ReportLabelTests(unittest.TestCase):
    def build(self, target, commit_repo, gc_count):
        recs, t0 = make_records(spike=(30, 40, 600))
        from agent.skills import analyze
        a = analyze.analyze(recs, make_metrics(t0), CFG)
        R = {"run_id": "20261011-000000", "verdict": "attention", "duration_s": 9, "summary": "s", "target": target,
             "commit": {"sha": "local+abc", "short": "local+ab", "subject": "x", "date": "", "repo": commit_repo, "changed_files": [], "subdir": "svc"},
             "smoke": {"passed": True, "requests": 20, "failures": 0, "p95_ms": 5, "reasons": [], "top_failures": []}, "heal": None, "analysis": a,
             "logs": {"log_lines": 1, "levels": {}, "exceptions": [], "warnings": [], "gc": {"count": gc_count}},
             "findings": [{"title": "t", "severity": "high", "category": "errors", "evidence": [], "suspected_cause": "c", "recommendation": "r",
                           "code_refs": [{"file": "app.py", "line": 7, "note": "n"}]}],
             "improvements": [], "load": {"users": 5, "duration_s": 9}, "llm_mode": "off",
             "tokens": {"total": 0, "by_step": {}, "skipped": []}, "trace": []}
        return report.build_html(R)

    def test_non_java_app_without_a_remote(self):
        html = self.build({"name": "shop", "process_label": "Python process"}, "", 0)
        self.assertIn("<h1>shop", html)
        self.assertIn("Python process memory", html)
        self.assertNotIn("JVM", html)                                                # no JVM wording, no GC card
        self.assertNotIn("/blob/", html)                                             # nothing to link to without a remote
        self.assertIn("<code>app.py:7</code>", html)

    def test_code_links_include_the_subfolder(self):
        html = self.build({"name": "shop", "process_label": "Python process"}, "https://github.com/o/r", 0)
        self.assertIn("https://github.com/o/r/blob/local+abc/svc/app.py#L7", html)

    def test_java_app_still_shows_gc(self):
        html = self.build({"name": "JPetStore", "process_label": "Tomcat JVM"}, "https://github.com/o/r", 12)
        self.assertIn("JVM garbage collection", html)


if __name__ == "__main__":
    unittest.main()
