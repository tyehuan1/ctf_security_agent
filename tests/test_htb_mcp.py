"""HTB MCP client: tool-id map, defensive response parsers, submitter wiring. No network."""

import unittest

import tests  # noqa: F401
from controller.htb_mcp import (HTBMcpSubmitter, HTBNotWired, MCPHTBClient, TOOLS,
                                 interpret_submit_response, parse_challenges, parse_connection_info,
                                 parse_download_url)


class ToolMapTest(unittest.TestCase):
    def test_confirmed_identifiers(self):
        # exact identifiers confirmed from a live tools/list + arg probe
        self.assertEqual(TOOLS["retrieve_ctf"], "retrieve_ctf")
        self.assertEqual(TOOLS["start"], "start_container")
        self.assertEqual(TOOLS["stop"], "stop_container")
        self.assertEqual(TOOLS["status"], "container_status")
        self.assertEqual(TOOLS["submit"], "submit_flag")
        self.assertEqual(TOOLS["download"], "get_download_link")
        self.assertEqual(TOOLS["scores"], "retrieve_ctf_scores")


class InterpretSubmitTest(unittest.TestCase):
    def test_boolean_fields(self):
        self.assertTrue(interpret_submit_response({"correct": True, "message": "Solved"}).verified)
        self.assertFalse(interpret_submit_response({"solved": False}).verified)
        self.assertFalse(interpret_submit_response({"success": False}).verified)

    def test_message_only(self):
        self.assertTrue(interpret_submit_response({"message": "Correct flag! congrats"}).verified)
        self.assertFalse(interpret_submit_response({"message": "Incorrect flag"}).verified)

    def test_unclear_is_none(self):
        self.assertIsNone(interpret_submit_response({"message": "queued"}).verified)

    def test_error_object_is_none_but_submitted(self):
        r = interpret_submit_response({"error": "nope", "status_code": 500})
        self.assertIsNone(r.verified)
        self.assertTrue(r.submitted)
        self.assertIn("nope", r.detail)


class ConnectionInfoTest(unittest.TestCase):
    def test_flat_ip_port(self):
        self.assertEqual(parse_connection_info({"ip": "10.0.0.1", "port": 4444}),
                         {"host": "10.0.0.1", "port": 4444})

    def test_nested(self):
        self.assertEqual(parse_connection_info({"data": {"host": "h", "port": "5555"}}),
                         {"host": "h", "port": 5555})

    def test_ip_colon_port_string(self):
        self.assertEqual(parse_connection_info({"connection": "1.2.3.4:31763"}),
                         {"host": "1.2.3.4", "port": 31763})

    def test_missing_returns_none(self):
        self.assertIsNone(parse_connection_info({"status": "starting"}))
        self.assertIsNone(parse_connection_info("not a dict"))


class ChallengesAndDownloadTest(unittest.TestCase):
    def test_parse_challenges_nested(self):
        detail = {"ctf": {"challenges": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]}}
        self.assertEqual([c["id"] for c in parse_challenges(detail)], [1, 2])

    def test_parse_challenges_absent(self):
        self.assertEqual(parse_challenges({"x": 1}), [])

    def test_parse_download_url(self):
        self.assertEqual(parse_download_url("https://s3/x.zip?sig=1"), "https://s3/x.zip?sig=1")
        self.assertEqual(parse_download_url({"url": "https://s3/y"}), "https://s3/y")
        self.assertIsNone(parse_download_url({"nope": 1}))


class SubmitterWiringTest(unittest.TestCase):
    def test_unwired_submitter_not_ready_and_raises(self):
        s = HTBMcpSubmitter(challenge_id="123", client=None)
        self.assertFalse(s.ready())
        with self.assertRaises(HTBNotWired):
            s.submit("HTB{x}")

    def test_wired_submitter_uses_client(self):
        class FakeClient:
            def submit_flag(self, cid, flag): return {"correct": True, "message": "ok"}
        s = HTBMcpSubmitter(challenge_id="123", client=FakeClient())
        self.assertTrue(s.ready())
        self.assertTrue(s.submit("HTB{x}").verified)

    def test_needs_challenge_id(self):
        class FakeClient:
            def submit_flag(self, cid, flag): return {}
        s = HTBMcpSubmitter(challenge_id=None, client=FakeClient())
        self.assertFalse(s.ready())           # no challenge_id -> not ready
        with self.assertRaises(HTBNotWired):
            s.submit("HTB{x}")

    def test_client_call_builds_tool_args(self):
        # _call maps logical->identifier and passes the right args, using the real sync bridge but a
        # fake _acall (no network).
        calls = {}
        c = MCPHTBClient(token="t")

        async def fake_acall(tool, args):
            calls["tool"], calls["args"] = tool, dict(args)
            return '{"ok": true}'

        c._acall = fake_acall
        c.start_instance("77")
        self.assertEqual(calls, {"tool": "start_container", "args": {"challenge_id": "77"}})
        c.submit_flag("77", "HTB{z}")
        self.assertEqual(calls, {"tool": "submit_flag", "args": {"challenge_id": "77", "flag": "HTB{z}"}})


if __name__ == "__main__":
    unittest.main()
