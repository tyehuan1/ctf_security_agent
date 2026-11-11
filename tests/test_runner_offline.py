"""Controller loop with a scripted fake solver on a file-only challenge (no Docker, no model).

Records are sample_type="synthetic" and written to a temp dir; they are not measured results.
"""

import json
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401
from common import config
from controller import loader
from controller.runner import Action, run_attempt

DATA = config.path("external/CTFTiny/ctftiny.json").exists()
KEY = "rev-beleaf"   # file-only


class ScriptedSolver:
    def __init__(self, actions):
        self.actions = list(actions)
        self.seen = []

    def reset(self, workspace, context):
        self.context = context

    def request_next_action(self, observations):
        self.seen.append(json.loads(json.dumps(observations)))
        action = self.actions.pop(0) if self.actions else Action("tool", "noop", {})
        return action, {"input_tokens": 100, "output_tokens": 10, "cost_usd": None}

    def execute(self, action, timeout_s):
        return {"status": "ok", "output": "x" * 10000, "error": None}

    def kill(self):
        self.stopped = True
        return {"stopped": True}


@unittest.skipUnless(DATA, "external/CTFTiny not cloned")
class RunnerOfflineTest(unittest.TestCase):
    def run_with(self, actions, **budget):
        settings = config.load_settings()
        settings["budget"].update(budget)
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        solver = ScriptedSolver(actions)
        rec = run_attempt(KEY, 1, solver, settings=settings, sample_type="synthetic",
                          results_path=t / "results.jsonl", traces_dir=t / "traces", runs_dir=t / "runs")
        trace = [json.loads(l) for l in (t / "traces" / f"{rec['run_id']}.jsonl").read_text().splitlines()]
        return rec, solver, trace

    def tearDown(self):
        self.tmp.cleanup()

    def test_wrong_then_right_flag(self):
        flag = loader.load_challenge(KEY).flag
        rec, solver, trace = self.run_with([Action("submit_flag", flag="flag{nope}"),
                                            Action("submit_flag", flag=flag)])
        self.assertEqual((rec["status"], rec["verified"]), ("solved", True))
        self.assertEqual(solver.seen[1][-1], {"verified": False})       # solver sees only the boolean
        self.assertNotIn(flag, json.dumps(solver.seen))
        self.assertEqual(rec["input_tokens"], 200)
        self.assertIsNone(rec["cost_usd"])
        self.assertEqual(trace[-1]["event"], "attempt_ended")

    def test_normal_end_stops_solver_and_removes_network(self):
        rec, solver, trace = self.run_with([], max_turns=1)
        self.assertTrue(getattr(solver, "stopped", False))
        events = [e["event"] for e in trace]
        self.assertLess(events.index("solver_stopped"), events.index("target_stopped"))
        stopped = next(e for e in trace if e["event"] == "target_stopped")
        self.assertTrue(stopped["network_removed"] and stopped["clean"])

    def test_model_text_is_in_the_main_trace(self):
        act = Action("tool", "noop", {})
        act.content = "Hypothesis: the flag is in a string constant."
        rec, solver, trace = self.run_with([act], max_turns=1)
        responses = [e for e in trace if e["event"] == "model_response"]
        self.assertEqual(responses[0]["content"], "Hypothesis: the flag is in a string constant.")

    def test_turn_cap_terminates_and_output_is_capped(self):
        rec, solver, trace = self.run_with([], max_turns=3, max_chars=500)
        self.assertEqual(rec["status"], "budget_exhausted")
        self.assertEqual(len(solver.seen), 3)
        self.assertTrue(any(e["event"] == "truncation" for e in trace))
        self.assertTrue(all(len(o.get("output", "")) < 600 for o in solver.seen[-1][1:]))


class HangingSolver(ScriptedSolver):
    def __init__(self):
        super().__init__([])
        self.killed = False

    def request_next_action(self, observations):
        import time
        time.sleep(30)               # a runaway model call
        return Action("give_up"), {}

    def kill(self):
        self.killed = True


@unittest.skipUnless(DATA, "external/CTFTiny not cloned")
class HardDeadlineTest(unittest.TestCase):
    def test_runaway_model_call_cut_off_at_attempt_deadline(self):
        import time
        settings = config.load_settings()
        settings["budget"]["attempt_deadline_s"] = 1
        solver = HangingSolver()
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            started = time.monotonic()
            rec = run_attempt(KEY, 1, solver, settings=settings, sample_type="synthetic",
                              results_path=t / "results.jsonl", traces_dir=t / "traces", runs_dir=t / "runs")
            took = time.monotonic() - started
            events = [json.loads(l)["event"] for l in (t / "traces" / f"{rec['run_id']}.jsonl").read_text().splitlines()]
        self.assertEqual(rec["status"], "timeout")
        self.assertLess(took, 5)
        self.assertTrue(solver.killed)
        self.assertIn("budget_stop", events)
        self.assertEqual(events[-1], "attempt_ended")


if __name__ == "__main__":
    unittest.main()
