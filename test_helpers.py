"""Offline tests for the controller helpers: manifest, batch planning/recovery, bound verifier,
hard deadline, usage capture. Synthetic data only — no live targets or model calls.

    python -m unittest -v test_helpers test_single_agent_summary
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import make_manifest  # noqa: E402
import run_batch  # noqa: E402
from common.usage import usage_from_gateway  # noqa: E402
from controller.budget import DeadlineExceeded, call_with_deadline  # noqa: E402
from controller.record import load_results  # noqa: E402
from controller.verifier import bind_verifier, normalize_candidate  # noqa: E402

DATASET = ROOT / "external" / "CTFTiny" / "ctftiny.json"


class ManifestTest(unittest.TestCase):
    def test_build_from_synthetic_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "index.json").write_text(json.dumps({
                "rev-demo": {"year": "2023", "event": "CSAW-Quals", "category": "rev", "challenge": "demo",
                             "path": "x/rev/demo"}}))
            (t / "README.md").write_text("| rev | 2023q | demo | Easy |\n")
            m = make_manifest.build(t / "index.json", ["rev-demo"], "evaluation", "a" * 40)
        self.assertEqual(m["challenges"][0]["difficulty"], "Easy")
        self.assertEqual((m["source_commit"], m["config_id"], m["n_challenges"]), ("a" * 40, "agent", 1))
        self.assertNotIn("flag", json.dumps(m).lower())

    def test_unknown_id_and_bad_commit_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "i.json").write_text("{}")
            with self.assertRaises(SystemExit):
                make_manifest.build(Path(tmp) / "i.json", ["nope"], "evaluation", "a" * 40)
            (Path(tmp) / "ids.txt").write_text("nope\n")
            with self.assertRaises(SystemExit):
                make_manifest.main(["--dataset", str(Path(tmp) / "i.json"), "--ids-file", str(Path(tmp) / "ids.txt"),
                                    "--split", "evaluation", "--source-commit", "HEAD", "--out", str(Path(tmp) / "o")])

    @unittest.skipUnless(DATASET.exists(), "external/CTFTiny not cloned")
    def test_real_index_our_ids(self):
        ids = make_manifest.read_ids(ROOT / "evaluation_ids.txt")
        m = make_manifest.build(DATASET, ids, "evaluation", "f" * 40)
        self.assertEqual([c["challenge_id"] for c in m["challenges"]], ids)
        self.assertTrue(all(c["difficulty"] != "Unrated" for c in m["challenges"]))


def live(cid, trial, status="no_flag"):
    return {"run_id": f"r-{cid}-{trial}", "config_id": "agent", "challenge_id": cid, "trial": trial,
            "status": status, "verified": status == "solved", "elapsed_s": 1, "input_tokens": None,
            "output_tokens": None, "cost_usd": None, "trace_file": "t", "reset_id": "x", "sample_type": "live"}


class BatchTest(unittest.TestCase):
    MANIFEST = {"challenges": [{"challenge_id": "a"}, {"challenge_id": "b"}]}

    def test_plan_is_3n_and_skips_recorded_slots(self):
        self.assertEqual(len(run_batch.plan(self.MANIFEST, [])), 6)
        todo = run_batch.plan(self.MANIFEST, [live("a", 1), live("a", 2, "infrastructure_error")])
        self.assertEqual(todo, [("a", 3), ("b", 1), ("b", 2), ("b", 3)])

    def test_synthetic_records_do_not_fill_slots(self):
        syn = dict(live("a", 1), sample_type="synthetic")
        self.assertIn(("a", 1), run_batch.plan(self.MANIFEST, [syn]))

    def test_crashed_attempt_recovered_once_as_infrastructure_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "traces").mkdir()
            events = [{"ts": "2026-11-08T10:00:00.000-05:00", "run_id": "agent-a-t2-x", "seq": 1,
                       "event": "attempt_started", "config_id": "agent", "challenge_id": "a", "trial": 2,
                       "sample_type": "live"},
                      {"ts": "2026-11-08T10:00:42.500-05:00", "run_id": "agent-a-t2-x", "seq": 2,
                       "event": "target_started", "reset_id": "fresh-agent-a-t2-x-1"}]
            (t / "traces" / "agent-a-t2-x.jsonl").write_text("\n".join(map(json.dumps, events)) + "\n")
            first = run_batch.recover_orphans(t / "traces", t / "results.jsonl")
            again = run_batch.recover_orphans(t / "traces", t / "results.jsonl")
            recs = load_results(t / "results.jsonl")
        self.assertEqual(len(first), 1)
        self.assertEqual(again, [])
        self.assertEqual((recs[0]["status"], recs[0]["trial"], recs[0]["elapsed_s"]), ("infrastructure_error", 2, 42.5))
        self.assertIsNone(recs[0]["input_tokens"])

    def test_lock_blocks_second_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / ".lock"
            with run_batch.BatchLock(lock):
                with self.assertRaises(SystemExit):
                    run_batch.BatchLock(lock).__enter__()
            self.assertFalse(lock.exists())


class VerifierTest(unittest.TestCase):
    def test_bound_single_argument_verify(self):
        verify_privately = bind_verifier(SimpleNamespace(flag="flag{abc}\n"))
        self.assertTrue(verify_privately("  flag{abc} ").verified)
        self.assertTrue(verify_privately("`flag{abc}`").verified)
        self.assertFalse(verify_privately("flag{abd}").verified)
        self.assertEqual(verify_privately("x"), {"verified": False})
        self.assertEqual(normalize_candidate(None), "")


class DeadlineAndUsageTest(unittest.TestCase):
    def test_runaway_call_is_cut_off(self):
        started = time.monotonic()
        with self.assertRaises(DeadlineExceeded):
            call_with_deadline(time.sleep, 0.2, 5)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(call_with_deadline(lambda x: x + 1, 1, 1), 2)
        with self.assertRaises(ZeroDivisionError):
            call_with_deadline(lambda: 1 / 0, 1)

    def test_usage_from_gateway(self):
        body = {"usage": {"prompt_tokens": 66, "completion_tokens": 13}}
        self.assertEqual(usage_from_gateway(body, {"x-litellm-response-cost": "3.8e-05"}),
                         {"input_tokens": 66, "output_tokens": 13, "cost_usd": 3.8e-05})
        self.assertEqual(usage_from_gateway({}, {}), {"input_tokens": None, "output_tokens": None, "cost_usd": None})


if __name__ == "__main__":
    unittest.main()
