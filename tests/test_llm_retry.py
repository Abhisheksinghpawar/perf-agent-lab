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


if __name__ == "__main__":
    unittest.main()
