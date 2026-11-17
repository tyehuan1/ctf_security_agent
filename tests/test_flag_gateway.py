"""Controller flag gateway: local verify (frozen), HTB manual capture, HTB auto-submit, and the
HTB MCP scaffold. No Docker, no model, no network."""

import json
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401
from common import config
from controller.flag_gateway import (HTB_CANDIDATE, AutoHTBGateway, CandidateLog, LocalGateway,
                                      ManualHTBGateway, SubmitResult, build_gateway)
from controller.htb_mcp import (HTBMcpSubmitter, HTBNotWired, build_default_submitter,
                                interpret_submit_response)


class FakeVerification:
    def __init__(self, ok): self.verified = ok


def verify_true(_): return FakeVerification(True)
def verify_false(_): return FakeVerification(False)


class FakeSubmitter:
    def __init__(self, result, ready=True):
        self._result, self._ready, self.calls = result, ready, []

    def ready(self): return self._ready

    def submit(self, flag):
        self.calls.append(flag)
        return self._result


def make_log(tmp, echo):
    return CandidateLog(Path(tmp), "htb-example", "agent-htb-example-t1-x", "manual", echo=echo,
                        challenge_name="Example")


class LocalGatewayTest(unittest.TestCase):
    def test_verified_true_and_false_passthrough(self):
        self.assertEqual(LocalGateway(verify_true).handle("flag{x}").verified, True)
        out = LocalGateway(verify_false).handle("flag{x}")
        self.assertEqual((out.verified, out.pending, out.source), (False, False, "local"))


class ManualGatewayTest(unittest.TestCase):
    def test_captures_pending_and_writes_both_files_and_console(self):
        echoed = []
        with tempfile.TemporaryDirectory() as tmp:
            g = ManualHTBGateway(make_log(tmp, echoed.append))
            out = g.handle("HTB{manual_flag}", reason="leaked libc then ret2system")
            self.assertEqual((out.verified, out.pending, out.submitted), (None, True, False))
            txt = (Path(tmp) / "htb_candidates.txt").read_text(encoding="utf-8")
            rows = [json.loads(l) for l in (Path(tmp) / "htb_candidates.jsonl").read_text().splitlines()]
        self.assertIn("HTB{manual_flag}", txt)
        self.assertIn("NOT submitted", txt)                      # auto-submit OFF is explicit
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["candidate"], "HTB{manual_flag}")
        self.assertEqual(rows[0]["submitted"], False)
        self.assertIsNone(rows[0]["verified"])
        self.assertIn("HTB{manual_flag}", "\n".join(echoed))     # surfaced to the console too


class AutoGatewayTest(unittest.TestCase):
    def _run(self, result):
        echoed = []
        with tempfile.TemporaryDirectory() as tmp:
            sub = FakeSubmitter(result)
            out = AutoHTBGateway(sub, make_log(tmp, echoed.append)).handle("HTB{x}")
            rows = [json.loads(l) for l in (Path(tmp) / "htb_candidates.jsonl").read_text().splitlines()]
        return out, rows[0]

    def test_accepted(self):
        out, row = self._run(SubmitResult(verified=True, submitted=True, source="htb-mcp", detail="+10"))
        self.assertEqual((out.verified, out.pending, out.submitted), (True, False, True))
        self.assertEqual((row["verified"], row["submitted"]), (True, True))

    def test_rejected(self):
        out, _ = self._run(SubmitResult(verified=False, submitted=True, source="htb-mcp", detail="nope"))
        self.assertEqual((out.verified, out.pending), (False, False))

    def test_undetermined_is_pending_not_wrong(self):
        out, _ = self._run(SubmitResult(verified=None, submitted=True, source="htb-mcp", detail="?"))
        self.assertEqual((out.verified, out.pending), (None, True))


class BuildGatewayTest(unittest.TestCase):
    def test_mode_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            kw = dict(verify=verify_true, htb_dir=Path(tmp), challenge_id="c", run_id="r", echo=lambda _: None)
            self.assertIsInstance(build_gateway("local", **kw), LocalGateway)
            self.assertIsInstance(build_gateway("manual", **kw), ManualHTBGateway)
            good = build_gateway("auto", submitter=FakeSubmitter(None, ready=True), **kw)
            self.assertIsInstance(good, AutoHTBGateway)

    def test_auto_requires_a_wired_submitter(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                build_gateway("auto", verify=verify_true, htb_dir=Path(tmp), challenge_id="c",
                              run_id="r", submitter=FakeSubmitter(None, ready=False), echo=lambda _: None)


class HtbMcpScaffoldTest(unittest.TestCase):
    def test_default_submitter_tracks_token_presence(self):
        import controller.htb_mcp as mod
        orig = mod.config.secret
        try:
            mod.config.secret = lambda name: None                 # no MCP_TOKEN
            sub = build_default_submitter("htb-example")
            self.assertIsInstance(sub, HTBMcpSubmitter)
            self.assertFalse(sub.ready())                         # unwired -> manual path still works
            with self.assertRaises(HTBNotWired):
                sub.submit("HTB{x}")
            mod.config.secret = lambda name: "tok"                # token present
            self.assertTrue(build_default_submitter("htb-example").ready())
        finally:
            mod.config.secret = orig

    def test_interpret_common_response_shapes(self):
        self.assertEqual(interpret_submit_response({"correct": True, "message": "Solved"}).verified, True)
        self.assertEqual(interpret_submit_response({"solved": False}).verified, False)
        self.assertIsNone(interpret_submit_response({"message": "queued"}).verified)   # unclear -> None

    def test_wired_client_is_used(self):
        class Client:
            def submit_flag(self, cid, flag): return {"correct": True, "message": "ok"}
        sub = HTBMcpSubmitter("htb-example", client=Client())
        self.assertTrue(sub.ready())
        self.assertEqual(sub.submit("HTB{x}").verified, True)


class RedactionAndModeConfigTest(unittest.TestCase):
    def test_mcp_token_is_a_redacted_secret(self):
        self.assertIn("MCP_TOKEN", config.SECRET_KEYS)

    def test_flag_mode_env_override(self):
        import os
        old = os.environ.get("CTF_FLAG_MODE")
        try:
            os.environ["CTF_FLAG_MODE"] = "manual"
            self.assertEqual(config.flag_mode({"mode": "local"}), "manual")   # env wins
            del os.environ["CTF_FLAG_MODE"]
            self.assertEqual(config.flag_mode({"mode": "auto"}), "auto")
            self.assertEqual(config.flag_mode(None), "local")                 # default
            with self.assertRaises(ValueError):
                config.flag_mode({"mode": "bogus"})
        finally:
            if old is not None:
                os.environ["CTF_FLAG_MODE"] = old
            else:
                os.environ.pop("CTF_FLAG_MODE", None)

    def test_htb_candidate_status_is_not_an_a4_status(self):
        from common.schemas import STATUSES
        self.assertNotIn(HTB_CANDIDATE, STATUSES)


if __name__ == "__main__":
    unittest.main()
