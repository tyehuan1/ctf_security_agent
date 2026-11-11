"""429 handling in GatewayClient and between-attempt pacing (fake HTTP; no network, no sleeping)."""

import unittest
from unittest import mock

import tests  # noqa: F401
from common import config
from controller.pacing import fallback_sleep_s, wait_for_rate_window
from solver.llm import GatewayClient, LLMError

OK_BODY = {"choices": [{"message": {"content": "", "tool_calls": [
    {"id": "c1", "function": {"name": "run_shell", "arguments": "{\"command\": \"ls\"}"}}]},
    "finish_reason": "tool_calls"}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 0}}}


class Resp:
    def __init__(self, status, body=None, headers=None, text=""):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, text or str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def client():
    with mock.patch("solver.llm.config.secret", return_value="test-token"):
        c = GatewayClient(config.load_settings())
    c._token = lambda: "test-token"
    return c


class GatewayRateLimitTest(unittest.TestCase):
    def test_429_waits_retry_after_then_succeeds(self):
        replies = [Resp(429, headers={"retry-after": "7"}, text="rate limit"), Resp(200, OK_BODY)]
        slept = []
        with mock.patch("solver.llm.requests.post", side_effect=lambda *a, **k: replies.pop(0)), \
                mock.patch("solver.llm.time.sleep", side_effect=slept.append):
            reply = client().chat([{"role": "user", "content": "x"}], timeout_s=195, purpose="t1")
        self.assertEqual(slept, [7.0])
        waits = reply.usage["rate_limit_waits"]
        self.assertEqual((len(waits), waits[0]["wait_s"], waits[0]["wait_source"]), (1, 7.0, "retry-after"))
        self.assertEqual(reply.usage["input_tokens"], 100)          # the 429 consumed no tokens

    def test_429_default_wait_when_no_retry_after(self):
        replies = [Resp(429, text="rate limit"), Resp(200, OK_BODY)]
        slept = []
        with mock.patch("solver.llm.requests.post", side_effect=lambda *a, **k: replies.pop(0)), \
                mock.patch("solver.llm.time.sleep", side_effect=slept.append):
            client().chat([{"role": "user", "content": "x"}], timeout_s=195)
        self.assertEqual(slept, [65.0])

    def test_429_wait_that_does_not_fit_fails_with_waits_attached(self):
        with mock.patch("solver.llm.requests.post", return_value=Resp(429, text="rate limit")), \
                mock.patch("solver.llm.time.sleep") as sl:
            with self.assertRaises(LLMError) as ctx:
                client().chat([{"role": "user", "content": "x"}], timeout_s=30)   # 65s wait > 30s budget
        sl.assert_not_called()
        self.assertIn("does not fit", str(ctx.exception))

    def test_429_retries_are_capped(self):
        with mock.patch("solver.llm.requests.post", return_value=Resp(429, headers={"retry-after": "1"}, text="rl")), \
                mock.patch("solver.llm.time.sleep"):
            with self.assertRaises(LLMError) as ctx:
                client().chat([{"role": "user", "content": "x"}], timeout_s=195)
        self.assertEqual(len(ctx.exception.waits), 3)              # rate_limit.max_429_retries
        self.assertIn("waits already spent", str(ctx.exception))


class RequestTimeoutTest(unittest.TestCase):
    def test_hung_request_is_cut_at_request_timeout_and_retried(self):
        import requests as rq
        replies = [rq.ReadTimeout("hung"), Resp(200, OK_BODY)]
        timeouts = []

        def post(*a, **k):
            timeouts.append(k["timeout"])
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch("solver.llm.requests.post", side_effect=post), mock.patch("solver.llm.time.sleep"):
            reply = client().chat([{"role": "user", "content": "x"}], timeout_s=195)
        self.assertEqual(timeouts[0], 60.0)                       # model.request_timeout_s, not 195
        self.assertEqual((reply.usage["attempts"], reply.usage["usage_incomplete"]), (2, True))


class PacingTest(unittest.TestCase):
    def test_waits_until_window_clears(self):
        windows = [Resp(200, headers={"x-ratelimit-remaining-tokens": "5000", "x-ratelimit-remaining-requests": "15"}),
                   Resp(200, headers={"x-ratelimit-remaining-tokens": "39990", "x-ratelimit-remaining-requests": "19"})]
        slept = []
        rep = wait_for_rate_window(config.load_settings(), probe=lambda s: windows.pop(0),
                                   sleep=slept.append, log=lambda m: None)
        self.assertEqual((rep["mode"], rep["waited_s"], slept), ("headers", 15.0, [15.0]))

    def test_fixed_sleep_when_headers_missing(self):
        slept = []
        rep = wait_for_rate_window(config.load_settings(), last_attempt_tokens=20000,
                                   probe=lambda s: Resp(200), sleep=slept.append, log=lambda m: None)
        self.assertEqual(rep["mode"], "fixed")
        self.assertAlmostEqual(slept[0], 60 * 20000 / 40000 + 5)
        self.assertEqual(fallback_sleep_s(None, config.load_settings()), 65.0)


if __name__ == "__main__":
    unittest.main()
