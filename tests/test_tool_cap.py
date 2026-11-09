"""The output cap applies before the model sees a tool result (no Docker, no gateway)."""

import json
import unittest

import tests  # noqa: F401
from common import config
from solver.agent import Action, Agent
from solver.sandbox import ExecResult
from tools.runner import ToolRunner, clean_binary

BIG = "A" * 25000 + "MIDDLE" + "Z" * 25000


class FakeSandbox:
    def exec(self, argv, timeout_s, stdin=None):
        return ExecResult("ok", stdout=BIG, exit_code=0)


class ToolCapTest(unittest.TestCase):
    def test_runner_caps_output_and_reports_it(self):
        res = ToolRunner(FakeSandbox(), None, max_chars=6000).execute(
            "inspect_file", {"path": "/workspace/x", "op": "strings"}, 10)
        self.assertLessEqual(len(res["output"]), 6000)
        self.assertTrue(res["output"].startswith("AAAA") and res["output"].endswith("ZZZZ"))
        self.assertNotIn("MIDDLE", res["output"])
        self.assertEqual(res["truncated"]["original_chars"], len(BIG))

    def test_model_message_is_capped(self):
        settings = config.load_settings()
        agent = Agent(settings, network="unused", run_id="cap-test")
        agent.runner = ToolRunner(FakeSandbox(), None, max_chars=settings["budget"]["max_chars"])
        agent.execute(Action("tool", "run_shell", {"command": "strings big"}, tool_call_id="call_1"), 10)
        sent = json.loads(agent.messages[-1]["content"])
        self.assertLessEqual(len(sent["output"]), settings["budget"]["max_chars"])
        self.assertIn("truncated", sent)                 # the model is told the output was cut
        self.assertLess(len(agent.messages[-1]["content"]), settings["budget"]["max_chars"] + 400)


class BinarySandbox:
    # What `head -40` of an ELF looks like after decoding with errors="replace".
    def exec(self, argv, timeout_s, stdin=None):
        return ExecResult("ok", stdout=("\x7fELF\x02\x01\x01\x00��\x00\x00" * 2000) + "\nend", exit_code=0)


class BinaryCleanTest(unittest.TestCase):
    def test_binary_is_cleaned_before_cap(self):
        res = ToolRunner(BinarySandbox(), None, max_chars=3000).execute("run_shell", {"command": "head rock"}, 10)
        self.assertTrue(all(c in "\n\t" or (c.isprintable() and c.isascii()) for c in res["output"]))
        self.assertGreater(res["binary_bytes_replaced"], 0)
        self.assertLessEqual(len(json.dumps(res["output"])), 3000 + 200)   # escaped size ~ char size now

    def test_clean_binary_keeps_text(self):
        self.assertEqual(clean_binary("flag{ok}\n\tcafé"), ("flag{ok}\n\tcafé", 0))
        self.assertEqual(clean_binary("a\x00b�c"), ("a.b.c", 2))


if __name__ == "__main__":
    unittest.main()
