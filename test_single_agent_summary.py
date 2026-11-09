"""Offline tests for ctf_single_agent_summary.py (synthetic records only; not measured results).

    python -m unittest -v test_helpers test_single_agent_summary
"""

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import ctf_single_agent_summary as S

ROOT = Path(__file__).resolve().parent


def rec(cid, trial, verified, inp=None, out=None, elapsed=10, status=None, sample_type="synthetic", config="agent"):
    status = status or ("solved" if verified else "no_flag")
    return {"run_id": f"syn-{cid}-{trial}", "config_id": config, "challenge_id": cid, "trial": trial,
            "status": status, "verified": verified, "elapsed_s": elapsed, "input_tokens": inp,
            "output_tokens": out, "cost_usd": None, "trace_file": f"traces/syn-{cid}-{trial}.jsonl",
            "reset_id": f"fresh-syn-{cid}-{trial}", "sample_type": sample_type}


MANIFEST = {"source_commit": "0" * 40, "challenges": [
    {"challenge_id": "practice-0", "category": "rev", "difficulty": "Unrated"},
    {"challenge_id": "practice-1", "category": "pwn", "difficulty": "Unrated"}]}

# Appendix A6 worked example: outcomes 1,0,1; input 100/200/300; output 10/20/30; time 10/20/30.
WORKED = [rec("practice-0", 1, True, 100, 10, 10), rec("practice-0", 2, False, 200, 20, 20),
          rec("practice-0", 3, True, 300, 30, 30)]
OTHER = [rec("practice-1", t, False, 50, 5, 60, status="budget_exhausted") for t in (1, 2, 3)]


class WorkedExampleTest(unittest.TestCase):
    def test_appendix_worked_example(self):
        p = S.summarize(MANIFEST, WORKED + OTHER, "agent", allow_synthetic=True)["per_challenge"][0]
        self.assertEqual(p["success_rate_pct"], 66.7)
        self.assertEqual(p["sample_sd_pp"], 57.7)
        self.assertEqual(p["mean_input_tokens"], 200.0)
        self.assertEqual(p["mean_output_tokens"], 20.0)
        self.assertEqual(p["mean_total_tokens"], 220.0)
        self.assertEqual(p["mean_elapsed_s"], 20.0)
        self.assertEqual((p["attempts"], p["successes_of_3"]), (3, "2/3"))

    def test_sd_edges(self):
        self.assertEqual(S.sample_sd_pp([1, 1, 1]), 0.0)
        self.assertEqual(S.sample_sd_pp([0, 0, 0]), 0.0)
        self.assertAlmostEqual(S.sample_sd_pp([1, 0, 0]), 57.735, places=3)

    def test_overall_rate_counts_failures(self):
        s = S.summarize(MANIFEST, WORKED + OTHER, "agent", allow_synthetic=True)
        self.assertTrue(s["complete"])
        self.assertEqual(s["overall_success_rate_pct"], round(100 * 2 / 6, 1))
        self.assertEqual(s["per_challenge"][1]["success_rate_pct"], 0.0)


class IncompleteAndNullTest(unittest.TestCase):
    def test_missing_trial_makes_overall_null(self):
        s = S.summarize(MANIFEST, WORKED + OTHER[:2], "agent", allow_synthetic=True)
        self.assertIsNone(s["overall_success_rate_pct"])
        self.assertEqual(s["incomplete_challenges"], ["practice-1"])
        p = s["per_challenge"][1]
        self.assertEqual((p["attempts"], p["successes"]), (2, 0))
        self.assertIsNone(p["success_rate_pct"])

    def test_incident_makes_challenge_incomplete(self):
        recs = WORKED + OTHER[:2] + [rec("practice-1", 3, False, status="infrastructure_error")]
        s = S.summarize(MANIFEST, recs, "agent", allow_synthetic=True)
        self.assertFalse(s["per_challenge"][1]["complete"])
        self.assertIn("incident", s["per_challenge"][1]["incomplete_reason"])
        self.assertIsNone(s["overall_success_rate_pct"])

    def test_duplicate_slot_is_incomplete(self):
        s = S.summarize(MANIFEST, WORKED + OTHER + [rec("practice-1", 3, True)], "agent", allow_synthetic=True)
        self.assertIn("duplicate", s["per_challenge"][1]["incomplete_reason"])

    def test_missing_tokens_stay_null_with_subtotal(self):
        recs = [rec("practice-0", 1, True, None, 10), rec("practice-0", 2, False, 200, 20),
                rec("practice-0", 3, True, 300, 30)] + OTHER
        p = S.summarize(MANIFEST, recs, "agent", allow_synthetic=True)["per_challenge"][0]
        self.assertIsNone(p["mean_input_tokens"])
        self.assertIsNone(p["mean_total_tokens"])
        self.assertEqual(p["mean_output_tokens"], 20.0)
        self.assertEqual(p["token_subtotals"]["input"], {"known_subtotal": 500, "known_count": 2, "missing_count": 1})

    def test_other_config_ignored(self):
        s = S.summarize(MANIFEST, WORKED + OTHER + [rec("practice-0", 1, False, config="baseline")],
                        "agent", allow_synthetic=True)
        self.assertEqual(s["records_other_config"], 1)
        self.assertTrue(s["complete"])


class SyntheticGuardAndCliTest(unittest.TestCase):
    def test_synthetic_refused_without_flag(self):
        with self.assertRaises(SystemExit):
            S.summarize(MANIFEST, WORKED + OTHER, "agent")

    def test_cli_writes_json_and_csv_with_blank_nulls(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "m.json").write_text(json.dumps(MANIFEST))
            recs = [rec("practice-0", 1, True, None, 10)] + WORKED[1:] + OTHER
            (t / "r.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
            args = ["--manifest", str(t / "m.json"), "--results", str(t / "r.jsonl"), "--config", "agent",
                    "--out", str(t / "s.json"), "--csv", str(t / "c.csv")]
            refused = subprocess.run([sys.executable, str(ROOT / "ctf_single_agent_summary.py"), *args],
                                     capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            ok = subprocess.run([sys.executable, str(ROOT / "ctf_single_agent_summary.py"), *args,
                                 "--allow-synthetic"], capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            summary = json.loads((t / "s.json").read_text())
            with (t / "c.csv").open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
        self.assertTrue(summary["contains_synthetic"])
        self.assertIsNone(summary["per_challenge"][0]["mean_input_tokens"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["mean_input_tokens"], "")
        self.assertEqual(rows[0]["successes_of_3"], "2/3")
        self.assertEqual(rows[0]["sample_sd_pp"], "57.7")


if __name__ == "__main__":
    unittest.main()
