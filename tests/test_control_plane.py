"""Manual control plane: target parsing, operator challenge, file staging, platform toggle.
Pure (no Docker, no network)."""

import json
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401
from controller.budget import htb_budget_defaults, htb_is_long_category
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

    def test_nested_files_recursed_and_macos_junk_skipped(self):
        # Reproduces the hw_critical_flight failure: files nested in a subdir + __MACOSX / ._* junk.
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as ws:
            sub = Path(d) / "board"
            sub.mkdir()
            (sub / "a.gbr").write_text("x", encoding="utf-8")
            (sub / "b.gbr").write_text("y", encoding="utf-8")
            junk = Path(d) / "__MACOSX" / "board"
            junk.mkdir(parents=True)
            (junk / "._a.gbr").write_bytes(b"junk")
            (Path(d) / ".DS_Store").write_bytes(b"junk")
            (sub / "._b.gbr").write_bytes(b"junk")
            chal = ManualChallenge("hw", Path(d))
            self.assertEqual(chal.files, ["board/a.gbr", "board/b.gbr"])   # recursed, junk gone
            ctx, inv = stage_manual_context(chal, "hw", Path(ws), None, description="d")
            self.assertTrue((Path(ws) / "board" / "a.gbr").exists())       # subpath preserved
            self.assertFalse((Path(ws) / "__MACOSX").exists())
            self.assertEqual(sorted(ctx["files"]), ["board/a.gbr", "board/b.gbr"])

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

    def test_manual_category_threads_into_challenge_and_context(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as ws:
            (Path(d) / "chall.bin").write_bytes(b"data")
            cp = build_control_plane("manual", files_dir=d, category="pwn")
            self.assertEqual(cp.category, "pwn")
            chal, target, provider = cp.provision("run-x", SETTINGS)
            self.assertEqual(chal.category, "pwn")
            ctx, _ = provider(Path(ws))
            self.assertEqual(ctx["category"], "pwn")

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


class HtbSelectiveBudgetTest(unittest.TestCase):
    """HTB-only per-category turn/timeout/token policy (run_htb.py). Never touches the scored path."""

    def test_pwn_and_reversing_get_the_long_allowance(self):
        for cat in ("pwn", "rev", "reversing", "RE", "  Pwn  "):
            self.assertTrue(htb_is_long_category(cat), cat)
            d = htb_budget_defaults(cat)
            self.assertEqual(d["max_turns"], 40)
            self.assertEqual(d["attempt_deadline_s"], 1200)
            self.assertEqual(d["attempt_token_cap"], 600000)

    def test_other_categories_stay_lean(self):
        for cat in ("crypto", "web", "stego", "hardware", "misc", "hackthebox", None, ""):
            self.assertFalse(htb_is_long_category(cat), cat)
            d = htb_budget_defaults(cat)
            self.assertEqual(d["max_turns"], 30)
            self.assertEqual(d["attempt_deadline_s"], 900)
            self.assertEqual(d["attempt_token_cap"], 450000)

    def test_defaults_are_independent_copies(self):
        a = htb_budget_defaults("pwn")
        a["max_turns"] = 1
        self.assertEqual(htb_budget_defaults("pwn")["max_turns"], 40)


if __name__ == "__main__":
    unittest.main()
