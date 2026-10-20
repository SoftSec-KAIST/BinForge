"""Regression checks for exact compiler and linker flag removal."""

import unittest

from binforge.compile_manager import remove_exact_flag


class CompileFlagTests(unittest.TestCase):
    def test_removes_flag_at_token_boundaries(self):
        cases = {
            "-flag -O2": " -O2",
            "-O2 -flag -g": "-O2  -g",
            "-O2 -flag": "-O2 ",
            "-flag\t-O2\n-flag": "\t-O2\n",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(remove_exact_flag(value, "-flag"), expected)

    def test_does_not_remove_similar_tokens(self):
        value = "-flag-extra prefix-flag -Wl,-flag=-flag"
        self.assertEqual(remove_exact_flag(value, "-flag"), value)

    def test_removes_duplicate_exact_tokens_only(self):
        value = "-flag -flag-extra -flag"
        self.assertEqual(remove_exact_flag(value, "-flag"), " -flag-extra ")

    def test_preserves_quoted_macro_and_pwd_expression(self):
        value = '-D_PATH_PROCNET_DEV="/proc/net/dev" -flag -I${PWD}/libxmi'
        self.assertEqual(
            remove_exact_flag(value, "-flag"),
            '-D_PATH_PROCNET_DEV="/proc/net/dev"  -I${PWD}/libxmi',
        )

    def test_empty_value_stays_empty(self):
        self.assertEqual(remove_exact_flag("", "-flag"), "")
        self.assertEqual(remove_exact_flag(None, "-flag"), "")


if __name__ == "__main__":
    unittest.main()
