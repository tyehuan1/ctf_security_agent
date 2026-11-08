import unittest
from types import SimpleNamespace

import tests  # noqa: F401  (src on sys.path)
from controller.verifier import verify_privately


class VerifyPrivatelyTest(unittest.TestCase):
    def setUp(self):
        self.chal = SimpleNamespace(flag="flag{c0ntroller_only}\n")

    def test_correct_flag_verifies(self):
        self.assertEqual(verify_privately("flag{c0ntroller_only}", self.chal), {"verified": True})

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(verify_privately("  flag{c0ntroller_only}\n", self.chal), {"verified": True})

    def test_wrong_flags_do_not_verify(self):
        for wrong in ("flag{wrong}", "", "flag{C0NTROLLER_ONLY}", "flag{c0ntroller_only", "c0ntroller_only"):
            with self.subTest(wrong=wrong):
                self.assertEqual(verify_privately(wrong, self.chal), {"verified": False})

    def test_result_contains_only_the_boolean(self):
        out = verify_privately("flag{wrong}", self.chal)
        self.assertEqual(set(out), {"verified"})
        self.assertNotIn("c0ntroller_only", repr(out))


if __name__ == "__main__":
    unittest.main()
