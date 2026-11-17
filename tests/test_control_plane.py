"""Manual control plane: target parsing, operator challenge, file staging, platform toggle.
Pure (no Docker, no network)."""

import json
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401
from controller.control_plane import (ManualControlPlane, McpControlPlane, build_control_plane,
                                       parse_target)
from controller.forwarder import OfflineTarget
from controller.manual_env import ManualChallenge, stage_manual_context

SETTINGS = {"target": {"local_host": "target", "network_prefix": "ctfeval"}}


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
    def test_manual_requires_files_dir(self):
        with self.assertRaises(ValueError):
            build_control_plane("manual", files_dir=None, target=None)

    def test_manual_file_only_uses_offline_target_and_null_endpoint(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as ws:
            (Path(d) / "chall.bin").write_bytes(b"data")
            cp = build_control_plane("manual", files_dir=d)     # no target => file-only
            self.assertIsInstance(cp, ManualControlPlane)
            chal, target, provider = cp.provision("run-x", SETTINGS)
            self.assertIsInstance(target, OfflineTarget)
            self.assertIs(target.has_service, False)
            self.assertIsNone(target.endpoint)
            ctx, inv = provider(Path(ws))                        # staging still works, target is null
            self.assertTrue((Path(ws) / "chall.bin").exists())
            self.assertIsNone(ctx["target"])
            self.assertEqual([i["path"] for i in inv], ["chall.bin"])

    def test_manual_rejects_missing_dir(self):
        with self.assertRaises(ValueError):
            build_control_plane("manual", files_dir="does/not/exist", target="h:80")

    def test_mcp_requires_challenge_id(self):
        with self.assertRaises(ValueError):
            build_control_plane("mcp")

    def test_mcp_provision_with_injected_client_builds_forwarder(self):
        # A fake client avoids any network: provision reads host/port and stages (no download here).
        class FakeClient:
            def start_instance(self, cid): return {"status": "started"}
            def connection_info(self, cid): return {"host": "1.2.3.4", "port": 31999}
            def get_download_link(self, cid): return None
            def stop_container(self, cid): return {"status": "stopped"}
        cp = McpControlPlane("4242", client=FakeClient())
        chal, target, provider = cp.provision("run-x", SETTINGS)
        self.assertEqual((target.remote_host, target.remote_port), ("1.2.3.4", 31999))
        self.assertTrue(callable(target.on_stop))            # teardown stops the HTB container
        self.assertEqual(chal.name, "4242")

    def test_unknown_platform(self):
        with self.assertRaises(ValueError):
            build_control_plane("bogus")


if __name__ == "__main__":
    unittest.main()
