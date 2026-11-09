"""Agent.request_next_action with a scripted gateway client (no Docker, no network)."""

import json
import unittest

import tests  # noqa: F401
from common import config
from controller.verifier import Verification
from solver.agent import MAX_NO_TOOL_NUDGES, Agent, _add_usage
from solver.llm import ModelReply, ToolCall


def usage(i=10, o=2, attempts=1):
    return {"input_tokens": i, "output_tokens": o, "cost_usd": 0.001, "attempts": attempts, "usage_incomplete": False}


def prose(text="I will look at the file."):
    return ModelReply(content=text, usage=usage())


def call(name, args, cid="c1"):
    return ModelReply(content="", tool_calls=[ToolCall(cid, name, args)], usage=usage())


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, *, tools=None, timeout_s=None, purpose=""):
        self.calls.append({"timeout_s": timeout_s, "purpose": purpose})
        return self.replies.pop(0)


def agent_with(replies):
    a = Agent(config.load_settings(), network="unused", run_id="loop-test")
    a.client = FakeClient(replies)
    a.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    return a


class AgentLoopTest(unittest.TestCase):
    def test_tool_call_becomes_action(self):
        a = agent_with([call("inspect_file", {"path": "/workspace/x", "op": "file"})])
        action, u = a.request_next_action([{}])
        self.assertEqual((action.kind, action.tool, action.tool_call_id), ("tool", "inspect_file", "c1"))
        self.assertEqual((u["input_tokens"], u["output_tokens"]), (10, 2))

    def test_nudge_then_tool_sums_usage_in_one_turn(self):
        a = agent_with([prose(), call("run_shell", {"command": "ls"})])
        action, u = a.request_next_action([{}])
        self.assertEqual(action.tool, "run_shell")
        self.assertEqual((u["input_tokens"], u["attempts"]), (20, 2))     # both gateway calls counted
        self.assertEqual(a.turns_used, 1)                                 # still one controller turn
        self.assertLess(a.client.calls[1]["timeout_s"], a.client.calls[0]["timeout_s"] + 1e-9)

    def test_gives_up_after_nudges(self):
        a = agent_with([prose()] * (MAX_NO_TOOL_NUDGES + 1))
        action, u = a.request_next_action([{}])
        self.assertEqual(action.kind, "give_up")
        self.assertEqual(u["input_tokens"], 10 * (MAX_NO_TOOL_NUDGES + 1))

    def test_unknown_usage_stays_unknown(self):
        self.assertIsNone(_add_usage(usage(), dict(usage(), input_tokens=None))["input_tokens"])
        self.assertEqual(_add_usage(None, usage())["input_tokens"], 10)

    def test_failed_submission_is_returned_to_model(self):
        a = agent_with([call("submit_flag", {"flag": " `flag{x}` "}, cid="s1"),
                        call("run_shell", {"command": "ls"}, cid="c2")])
        action, _ = a.request_next_action([{}])
        self.assertTrue(action.is_flag_submission)
        self.assertEqual(action.flag, "flag{x}")                          # stripped/normalized
        a.request_next_action([{}, Verification(verified=False)])
        tool_msgs = [m for m in a.messages if m["role"] == "tool"]
        self.assertEqual(tool_msgs[0]["tool_call_id"], "s1")
        self.assertEqual(json.loads(tool_msgs[0]["content"])["verified"], False)


if __name__ == "__main__":
    unittest.main()
