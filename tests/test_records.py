import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401
from common.schemas import RESULT_FIELDS, SchemaError, make_result, validate_result
from controller.budget import AttemptBudget, Budget, truncate
from controller.record import Tracer, append_result

APPENDIX_EXAMPLE = {
    "run_id": "example-agent-01",
    "config_id": "agent", "challenge_id": "practice-0",
    "trial": 1, "status": "no_flag", "verified": False,
    "elapsed_s": 10, "input_tokens": None,
    "output_tokens": None, "cost_usd": None,
    "trace_file": "traces/example-agent-01.jsonl",
    "reset_id": "fresh-example-01", "sample_type": "synthetic",
}


def result(**kw):
    base = dict(run_id="r1", challenge_id="pwn-roppity", trial=1, status="no_flag", elapsed_s=1.0,
                input_tokens=None, output_tokens=None, cost_usd=None, trace_file="traces/r1.jsonl",
                reset_id="fresh-r1", sample_type="synthetic")
    base.update(kw)
    return make_result(**base)


class SchemaTest(unittest.TestCase):
    def test_appendix_example_validates_and_key_order_matches(self):
        validate_result(dict(APPENDIX_EXAMPLE))
        self.assertEqual(tuple(APPENDIX_EXAMPLE), RESULT_FIELDS)
        self.assertEqual(tuple(result()), RESULT_FIELDS)

    def test_rejects_other_config_bad_trial_status_and_zeroed_nulls(self):
        for bad in ({"config_id": "baseline"}, {"trial": 4}, {"status": "partial"},
                    {"verified": True}, {"input_tokens": -1}, {"sample_type": "pilot"}):
            with self.subTest(bad=bad), self.assertRaises(SchemaError):
                validate_result({**APPENDIX_EXAMPLE, **bad})

    def test_verified_derived_from_status(self):
        self.assertTrue(result(status="solved")["verified"])
        self.assertFalse(result(status="wrong_flag")["verified"])

    def test_duplicate_live_slot_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "results.jsonl"
            append_result(p, result(sample_type="live"))
            with self.assertRaises(ValueError):
                append_result(p, result(run_id="r2", sample_type="live"))
            self.assertEqual(len(p.read_text().splitlines()), 1)


class BudgetTest(unittest.TestCase):
    def test_budget_is_frozen(self):
        b = Budget(12, 6000, 45, 600, 1000)
        with self.assertRaises(Exception):
            b.max_turns = 99

    def test_turn_token_and_deadline_caps(self):
        now = [0.0]
        ab = AttemptBudget(Budget(2, 6000, 45, 600, 1000), clock=lambda: now[0])
        ab.start()
        self.assertIsNone(ab.exhausted_reason())
        ab.record_usage(100, 10)
        ab.record_usage(100, 10)
        self.assertEqual(ab.exhausted_reason(), "budget_exhausted")
        ab2 = AttemptBudget(Budget(99, 6000, 45, 600, 100), clock=lambda: now[0])
        ab2.start()
        ab2.record_usage(90, 20)
        self.assertEqual(ab2.exhausted_reason(), "budget_exhausted")
        ab3 = AttemptBudget(Budget(99, 6000, 45, 600, 10**9), clock=lambda: now[0])
        ab3.start()
        now[0] = 590
        self.assertEqual(ab3.tool_timeout(), 10)
        now[0] = 600
        self.assertEqual(ab3.exhausted_reason(), "timeout")

    def test_missing_usage_stays_null(self):
        ab = AttemptBudget(Budget(12, 6000, 45, 600, 1000))
        ab.record_usage(None, 5)
        self.assertEqual(ab.totals(), {"input_tokens": None, "output_tokens": 5, "cost_usd": None})

    def test_truncate_keeps_head_and_tail_within_cap(self):
        text = "H" * 500 + "M" * 10000 + "T" * 500
        out, info = truncate(text, 200)
        self.assertLessEqual(len(out), 200)                 # marker counts toward the cap
        self.assertTrue(out.startswith("H" * info["kept_head"]) and out.endswith("T" * info["kept_tail"]))
        self.assertEqual(info["kept_head"] + info["kept_tail"] + info["omitted"], len(text))
        self.assertEqual(truncate("short", 100), ("short", None))

    def test_truncate_is_idempotent(self):
        once, _ = truncate("x" * 50000, 6000)
        self.assertEqual(truncate(once, 6000), (once, None))   # no double marker


class TracerTest(unittest.TestCase):
    def test_secrets_redacted_and_truncation_logged(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("common.config.secret_values", return_value=["sk-SECRET123"]):
            tr = Tracer(Path(tmp) / "t.jsonl", "r1", max_chars=20)
            tr("model_request", headers={"Authorization": "Bearer sk-SECRET123"})
            tr.capture("x" * 100, "tool_result.output")
            lines = [json.loads(l) for l in (Path(tmp) / "t.jsonl").read_text().splitlines()]
        self.assertNotIn("sk-SECRET123", json.dumps(lines))
        self.assertEqual([l["event"] for l in lines], ["model_request", "truncation"])
        self.assertEqual(lines[1]["original_chars"], 100)

    def test_unknown_event_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                Tracer(Path(tmp) / "t.jsonl", "r1", 100)("raise_limits")


if __name__ == "__main__":
    unittest.main()
