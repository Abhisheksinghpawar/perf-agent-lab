import unittest
from unittest import mock

from tests.helpers import CFG
from agent.llm import LLM, LLMUnavailable


class FakeResp:
    def __init__(self, status, headers=None, body=None):
        self.status_code, self.headers, self._body = status, headers or {}, body or {}

    def json(self):
        return self._body


class Ctx:
    def add_tokens(self, *a):
        pass


def make():
    llm = LLM(CFG, Ctx(), enabled=True, transport=None)
    llm.key, llm.enabled = "k", True
    return llm


OK = FakeResp(200, body={"choices": [{"message": {"content": "hi"}}], "usage": {}})


class RetryTests(unittest.TestCase):
    def test_rate_limit_waits_as_the_provider_asks_then_succeeds(self):
        with mock.patch("agent.llm.requests.post", side_effect=[FakeResp(429, {"retry-after": "12"}), OK]) as post, \
                mock.patch("agent.llm.time.sleep") as sleep:
            self.assertEqual(make()._post({})["choices"][0]["message"]["content"], "hi")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(12.0)

    def test_rate_limit_without_a_hint_backs_off_in_long_enough_steps(self):
        with mock.patch("agent.llm.requests.post", side_effect=[FakeResp(429), FakeResp(429), OK]), \
                mock.patch("agent.llm.time.sleep") as sleep:
            make()._post({})
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5.0, 10.0])

    def test_the_wait_is_capped(self):
        self.assertEqual(LLM._backoff(FakeResp(429, {"retry-after": "600"}), 0), 45.0)
        self.assertLessEqual(LLM._backoff(FakeResp(503), 9), 20.0)

    def test_gives_up_with_a_clear_error_after_all_retries(self):
        with mock.patch("agent.llm.requests.post", return_value=FakeResp(429)) as post, mock.patch("agent.llm.time.sleep"):
            with self.assertRaises(LLMUnavailable) as cm:
                make()._post({})
        self.assertEqual(post.call_count, 6)
        self.assertIn("429", str(cm.exception))

    def test_client_errors_are_not_retried(self):
        with mock.patch("agent.llm.requests.post", return_value=FakeResp(401)) as post, mock.patch("agent.llm.time.sleep"):
            with self.assertRaises(LLMUnavailable):
                make()._post({})
        self.assertEqual(post.call_count, 1)


class NudgeTests(unittest.TestCase):
    """Real models sometimes say 'done' without calling the tool that does the work."""

    def run_with(self, replies, until, max_turns=6):
        sent, applied = [], []

        def transport(payload):
            sent.append(payload)
            r = replies[min(len(sent) - 1, len(replies) - 1)]
            msg = {"content": r} if r != "TOOL" else {"content": None, "tool_calls": [{"id": "1", "type": "function", "function": {"name": "apply", "arguments": "{}"}}]}
            return {"usage": {"prompt_tokens": 1, "completion_tokens": 1}, "choices": [{"message": msg}]}

        tools = {"apply": ({"description": "d", "type": "object", "properties": {}}, lambda: applied.append(1) or {"ok": True})}
        out = LLM(CFG, Ctx(), transport=transport).run_agent("t", "s", "u", tools, until=until(applied), max_turns=max_turns)
        return out, sent, applied

    def test_a_claim_without_the_tool_call_is_not_accepted(self):
        out, sent, applied = self.run_with(["I fixed it!", "TOOL", "Done."], lambda applied: (lambda: bool(applied)))
        self.assertEqual((out, len(applied), len(sent)), ("Done.", 1, 3))
        self.assertTrue(any("call the tool now" in m["content"] for m in sent[-1]["messages"] if m["role"] == "user"))   # the nudge is in the history

    def test_it_gives_up_after_two_nudges_instead_of_looping(self):
        out, sent, applied = self.run_with(["I fixed it!"], lambda applied: (lambda: False))
        self.assertEqual((out, len(applied), len(sent)), ("I fixed it!", 0, 3))

    def test_no_nudge_when_the_work_is_verified(self):
        out, sent, _ = self.run_with(["TOOL", "All good."], lambda applied: (lambda: bool(applied)))
        self.assertEqual((out, len(sent)), ("All good.", 2))

    def test_without_until_behaviour_is_unchanged(self):
        sent = []
        llm = LLM(CFG, Ctx(), transport=lambda p: sent.append(p) or {"choices": [{"message": {"content": "hi"}}]})
        self.assertEqual(llm.run_agent("t", "s", "u", {}), "hi")
        self.assertEqual(len(sent), 1)


class RobustnessTests(unittest.TestCase):
    def test_http_400_keeps_only_the_short_error_code(self):
        resp = FakeResp(400, body={"error": {"code": "tool_use_failed", "message": "secret-account-detail", "failed_generation": "x"}})
        with mock.patch("agent.llm.requests.post", return_value=resp):
            with self.assertRaises(LLMUnavailable) as cm:
                make()._post({})
        self.assertEqual(cm.exception.code, "tool_use_failed")
        self.assertIn("tool_use_failed", str(cm.exception))
        self.assertNotIn("secret-account-detail", str(cm.exception))                # bodies never reach logs or reports

    def test_a_malformed_tool_call_is_reported_back_to_the_model_and_retried(self):
        calls = []

        def transport(payload):
            calls.append(payload)
            if len(calls) == 1:
                raise LLMUnavailable("LLM request failed with HTTP 400 (tool_use_failed)", "tool_use_failed")
            return {"choices": [{"message": {"content": "ok"}}]}

        out = LLM(CFG, Ctx(), transport=transport).run_agent("t", "s", "u", {})
        self.assertEqual((out, len(calls)), ("ok", 2))
        self.assertTrue(any("malformed" in m["content"] for m in calls[1]["messages"] if m["role"] == "user"))

    def test_other_400s_are_not_swallowed(self):
        def transport(payload):
            raise LLMUnavailable("LLM request failed with HTTP 400 (invalid_request_error)", "invalid_request_error")
        with self.assertRaises(LLMUnavailable):
            LLM(CFG, Ctx(), transport=transport).run_agent("t", "s", "u", {})

    def test_a_script_in_a_code_block_is_treated_as_a_submission(self):
        fence = "`" * 3
        replies = [f"Here is the fix:\n{fence}python\nprint('v1')\n{fence}", "Done, it passes."]
        seen, sent = [], []

        def transport(payload):
            sent.append(payload)
            return {"choices": [{"message": {"content": replies[len(sent) - 1]}}]}

        def on_text(reply):
            if "```python" in reply:
                seen.append(reply)
                return "The smoke test passed."

        out = LLM(CFG, Ctx(), transport=transport).run_agent("t", "s", "u", {}, on_text=on_text)
        self.assertEqual((out, len(seen), len(sent)), ("Done, it passes.", 1, 2))

    def test_when_the_turn_budget_runs_out_tools_stay_defined_but_disabled(self):
        tools = {"apply": ({"description": "d", "type": "object", "properties": {}}, lambda: {"ok": True})}
        sent = []

        def transport(payload):
            sent.append(payload)
            if payload.get("tool_choice") == "none":
                return {"choices": [{"message": {"content": "final"}}]}
            return {"choices": [{"message": {"content": None, "tool_calls": [{"id": "1", "type": "function", "function": {"name": "apply", "arguments": "{}"}}]}}]}

        out = LLM(CFG, Ctx(), transport=transport).run_agent("t", "s", "u", tools, max_turns=2)
        self.assertEqual(out, "final")
        self.assertIn("tools", sent[-1])                                              # providers reject tool history without definitions
        self.assertEqual(sent[-1]["tool_choice"], "none")


if __name__ == "__main__":
    unittest.main()
