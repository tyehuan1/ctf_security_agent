"""Loader/export and compose sanitizing. Data tests skip when external/ is not cloned."""

import json
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401
from common import config
from controller import loader
from controller.target import sanitize_compose

DATA = config.path("external/CTFTiny/ctftiny.json").exists()


class SanitizeComposeTest(unittest.TestCase):
    UPSTREAM = {
        "name": "showdown",
        "services": {"showdown": {
            "image": "llmctf/2018f-msc-showdown", "privileged": True,
            "volumes": [{"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}],
            "ports": [{"target": 9222, "published": "9222"}],
            "networks": {"ctfnet": {"aliases": ["showdown.csaw.io"]}}}},
        "networks": {"ctfnet": {"name": "ctfnet", "external": True}},
    }

    def test_strips_host_access_and_uses_internal_network(self):
        cfg, removed = sanitize_compose(self.UPSTREAM, "ctfeval-x-net", "target")
        svc = cfg["services"]["showdown"]
        for key in ("privileged", "volumes", "ports"):
            self.assertNotIn(key, svc)
        self.assertEqual(svc["networks"], {"ctfeval": {"aliases": ["target"]}})
        self.assertEqual(cfg["networks"], {"ctfeval": {"name": "ctfeval-x-net", "internal": True}})
        self.assertNotIn("csaw", json.dumps(cfg))
        self.assertTrue(any("docker.sock" in r for r in removed))


@unittest.skipUnless(DATA, "external/CTFTiny not cloned")
class LoaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = config.load_settings()
        cls.ds = loader.load_dataset(cls.settings)

    def test_all_evaluation_ids_load(self):
        for key in loader.load_evaluation_ids(self.settings):
            with self.subTest(key=key):
                chal = loader.load_challenge(key, self.ds, self.settings)
                self.assertTrue(chal.name and chal.category)

    def test_export_has_no_flag_no_csaw_host_and_hash_inventory(self):
        for key in loader.load_evaluation_ids(self.settings):
            chal = loader.load_challenge(key, self.ds, self.settings)
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp) / "ws"
                ctx, inv = loader.export_contestant_context(chal, key, ws, "target")
                blob = json.dumps(ctx)
                self.assertNotIn(chal.flag.strip(), blob)
                self.assertNotIn("csaw.io", blob.lower())
                self.assertEqual(sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file()),
                                 sorted(i["path"] for i in inv))
                self.assertFalse((ws / "challenge.json").exists())
                self.assertTrue(all(len(i["sha256"]) == 64 for i in inv))

    def test_path_remap_for_get_it(self):
        chal = loader.load_challenge("pwn-get-it", self.ds, self.settings)
        self.assertEqual(chal.challenge_dir.name, "get_it_")


if __name__ == "__main__":
    unittest.main()
