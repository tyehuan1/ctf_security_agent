"""Manual control plane: target parsing, operator challenge, file staging, platform toggle.
Pure (no Docker, no network)."""

import json
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401
from controller.control_plane import (McpControlPlane, build_control_plane, parse_target)
from controller.htb_mcp import HTBNotWired
from controller.manual_env import ManualChallenge, stage_manual_context


class ParseTargetTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_target("154.57.164.83:31763"), ("154.57.164.83", 31763))
        self.assertEqual(parse_target(" host:1 "), ("host", 1))

    def test_invalid(self):
        for bad in ("noport", "h:0", "h:70000", "a:b:c", "host:", ":80", "host:x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_target(bad)


class ManualChallengeAndStagingTest(unittest.TestCase):
    def _files(self, d):
        (Path(d) / "a.bin").write_bytes(b"\x00\x01flagish")
        (Path(d) / "readme.txt").write_text("hello", encoding="utf-8")

    def test_manual_challenge_lists_files_no_flag(self):
        with tempfile.TemporaryDirectory() as d:
            self._files(d)
            chal = ManualChallenge("example", Path(d))
            self.assertIsNone(chal.flag)
            self.assertEqual(chal.files, ["a.bin", "readme.txt"])
            self.assertEqual(chal.name, "example")

    def test_staging_copies_files_and_builds_context(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as ws:
            self._files(d)
            chal = ManualChallenge("example", Path(d))
            endpoint = {"host": "target", "port": 31763, "type": "nc"}
            ctx, inv = stage_manual_context(chal, "example", Path(ws), endpoint,
                                            description="desc", flag_format="HTB{...}")
            self.assertTrue((Path(ws) / "a.bin").exists() and (Path(ws) / "readme.txt").exists())
            self.assertEqual(ctx["target"], endpoint)
            self.assertEqual(sorted(ctx["files"]), ["a.bin", "readme.txt"])
            self.assertEqual(ctx["flag_format"], "HTB{...}")
            self.assertNotIn("flag", ctx)                       # no answer key in context
            self.assertTrue(all(len(i["sha256"]) == 64 for i in inv))
            self.assertEqual(len(json.dumps(ctx)) > 0, True)


class BuildControlPlaneTest(unittest.TestCase):
    def test_manual_requires_files_and_target(self):
        with self.assertRaises(ValueError):
            build_control_plane("manual", files_dir=None, target=None)

    def test_manual_rejects_missing_dir(self):
        with self.assertRaises(ValueError):
            build_control_plane("manual", files_dir="does/not/exist", target="h:80")

    def test_mcp_is_scaffold_and_provision_raises(self):
        cp = build_control_plane("mcp", challenge_id="htb-x")
        self.assertIsInstance(cp, McpControlPlane)
        with self.assertRaises(HTBNotWired):
            cp.provision("run-x", {"target": {"local_host": "target", "network_prefix": "ctfeval"}})

    def test_unknown_platform(self):
        with self.assertRaises(ValueError):
            build_control_plane("bogus")


if __name__ == "__main__":
    unittest.main()
