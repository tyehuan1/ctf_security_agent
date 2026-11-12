"""Agent.request_next_action with a scripted gateway client (no Docker, no network)."""

import json
import os
import unittest

import tests  # noqa: F401
from common import config
from controller.verifier import Verification
from solver.agent import KEEP_TOOL_RESULTS, MAX_NO_TOOL_NUDGES, Agent, _add_usage, extract_candidate
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
        self.calls.append({"timeout_s": timeout_s, "purpose": purpose,
                           "messages": json.loads(json.dumps(messages))})
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


    def test_reasoning_text_is_carried_on_the_action(self):
        reply = ModelReply(content="The ELF may embed the flag; strings should show it.",
                           tool_calls=[ToolCall("c1", "inspect_file", {"path": "/workspace/x", "op": "strings"})],
                           usage=usage())
        action, _ = agent_with([reply]).request_next_action([{}])
        self.assertEqual(action.content, "The ELF may embed the flag; strings should show it.")

    def test_submission_is_reduced_to_the_flag_format_substring(self):
        a = agent_with([call("submit_flag", {"flag": "The flag is flag{abc} (from strings)."}, cid="s1")])
        action, _ = a.request_next_action([{}])
        self.assertEqual((action.flag, action.raw_flag), ("flag{abc}", "The flag is flag{abc} (from strings)."))


class ReasoningFieldTest(unittest.TestCase):
    def test_model_facing_specs_require_reasoning(self):
        from tools.specs import TOOL_SPECS, tool_specs
        for spec in tool_specs():
            fn = spec["function"]
            field = "reason" if fn["name"] == "submit_flag" else "hypothesis"
            self.assertEqual(fn["parameters"]["required"][0], field, fn["name"])
        self.assertNotIn("hypothesis", json.dumps(TOOL_SPECS))     # execution schema unchanged

    def test_hypothesis_is_stripped_and_recorded(self):
        a = agent_with([call("run_shell", {"hypothesis": "The flag is a plain string.", "command": "ls"})])
        action, _ = a.request_next_action([{}])
        self.assertEqual(action.arguments, {"command": "ls"})
        self.assertEqual(action.content, "The flag is a plain string.")
        sent = a.messages[-1]["tool_calls"][0]["function"]["arguments"]
        self.assertIn("hypothesis", sent)                          # the model's own history keeps it

    def test_reason_is_stripped_from_submission(self):
        a = agent_with([call("submit_flag", {"reason": "strings showed it verbatim.", "flag": "flag{q}"})])
        action, _ = a.request_next_action([{}])
        self.assertEqual((action.flag, action.content), ("flag{q}", "strings showed it verbatim."))

    def test_missing_reasoning_does_not_block_the_call(self):
        a = agent_with([call("run_shell", {"command": "ls"})])
        action, _ = a.request_next_action([{}])
        self.assertEqual((action.tool, action.arguments, action.content), ("run_shell", {"command": "ls"}, ""))


class PromptVersionTest(unittest.TestCase):
    def test_prompt_version_matches_agent_version(self):
        from solver.prompts import PROMPT_VERSION
        self.assertEqual(PROMPT_VERSION, f"ctf-solver-{config.load_settings()['agent_version']}")


class ConfigArmTest(unittest.TestCase):
    """Each arm selects image + prompt variant from one tree via CTF_ARM / agent_version."""

    def tearDown(self):
        os.environ.pop("CTF_ARM", None)

    def test_arm_selects_image_and_prompt(self):
        for arm, variant, image in [("baseline", "baseline", "ctf-solver-sandbox:v1"),
                                    ("1.0.0", "frozen", "ctf-solver-sandbox:v1"),
                                    ("1.1.0", "imaging", "ctf-solver-sandbox:v1.1")]:
            os.environ["CTF_ARM"] = arm
            s = config.load_settings()
            self.assertEqual(s["agent_version"], arm)
            self.assertEqual(s["sandbox"]["image"], image)
            self.assertEqual(s["sandbox"]["prompt"], variant)

    def test_unknown_arm_rejected(self):
        os.environ["CTF_ARM"] = "9.9.9"
        with self.assertRaises(ValueError):
            config.load_settings()

    def test_prompt_variants_differ_as_specified(self):
        from solver.prompts import build_system_prompt
        baseline = build_system_prompt("baseline")
        frozen = build_system_prompt("frozen")
        imaging = build_system_prompt("imaging")
        # baseline (pre-improvement): no reasoning block, no run-and-interact block, no imaging
        self.assertNotIn("Reason before you act", baseline)
        self.assertNotIn("run the program", baseline)
        self.assertNotIn("pytesseract", baseline)
        # frozen (selected v1.0.0): reasoning + run-and-interact, but no imaging
        self.assertIn("Reason before you act", frozen)
        self.assertIn("run the program", frozen)
        self.assertNotIn("pytesseract", frozen)
        # imaging (v1.1.0): frozen plus the imaging/OCR guidance
        self.assertIn("pytesseract", imaging)
        self.assertLess(len(baseline), len(frozen))
        self.assertLess(len(frozen), len(imaging))


class ExtractCandidateTest(unittest.TestCase):
    def test_cases(self):
        cases = {
            "flag{abc}": "flag{abc}",
            "flag{abc}xyz": "flag{abc}",
            "  `FLAG{A_b}` ": "FLAG{A_b}",
            "answer: csawctf{x1}.": "csawctf{x1}",
            "flag{a{b}c}": "flag{a{b}c}",          # inner braces kept
            "IoDJuvwxy": "IoDJuvwxy",              # no flag format -> unchanged
            "flag{unterminated": "flag{unterminated",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(extract_candidate(raw), want)


class FakeRunner:
    def execute(self, name, arguments, timeout_s):
        return {"status": "ok", "output": f"full output of {arguments['command']}", "exit_code": 0}


class ElisionTest(unittest.TestCase):
    def test_long_conversation_sends_only_last_six_tool_results_in_full(self):
        n = 10
        a = agent_with([call("run_shell", {"command": f"cmd{i}"}, cid=f"c{i}") for i in range(n + 1)])
        a.runner = FakeRunner()
        for i in range(n):
            action, _ = a.request_next_action([{}])
            a.execute(action, 10)
        a.request_next_action([{}])                       # the 11th call sees 10 tool results
        sent = a.client.calls[-1]["messages"]
        tools = [m for m in sent if m["role"] == "tool"]
        full = [m for m in tools if "full output of" in m["content"]]
        stubs = [m for m in tools if "elided" in m["content"]]
        self.assertEqual((len(tools), len(full), len(stubs)), (n, KEEP_TOOL_RESULTS, n - KEEP_TOOL_RESULTS))
        self.assertEqual([m["tool_call_id"] for m in full], [f"c{i}" for i in range(n - KEEP_TOOL_RESULTS, n)])
        # every assistant tool_call still has a matching tool message
        called = [tc["id"] for m in sent if m["role"] == "assistant" for tc in m.get("tool_calls", [])]
        self.assertTrue(set(called[:-1]) <= {m["tool_call_id"] for m in tools})
        self.assertEqual(sent[0]["role"], "system")
        self.assertEqual(sent[1]["content"], "u")         # initial context kept
        # internal history is untouched
        self.assertEqual(sum("full output of" in m.get("content", "") for m in a.messages), n)

    def test_short_conversation_is_not_elided(self):
        a = agent_with([call("run_shell", {"command": "a"}, cid="c0"), call("run_shell", {"command": "b"}, cid="c1")])
        a.runner = FakeRunner()
        action, _ = a.request_next_action([{}])
        a.execute(action, 10)
        a.request_next_action([{}])
        self.assertEqual(a.client.calls[-1]["messages"][-1]["role"], "tool")
        self.assertIn("full output of a", a.client.calls[-1]["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
