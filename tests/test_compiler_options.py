"""Configuration checks for optional compiler flag combinations."""

import unittest

from binforge.compiler_options import (
    compiler_option_sets,
    configured_compiler_options,
    option_set_label,
    option_set_slug,
)


class CompilerOptionTests(unittest.TestCase):
    def test_single_flag_set_is_unchanged_by_default(self):
        build = {"extra_compiler_options": {
            "pie": True, "no_inline": False, "stack_protector": True}}
        self.assertEqual(compiler_option_sets(build), [
            ("", {"pie": True, "no_inline": False, "stack_protector": True})])
        build["combine_extra_compiler_options"] = "false"
        self.assertEqual(len(compiler_option_sets(build)), 1)

    def test_three_enabled_flags_expand_to_eight_distinct_sets(self):
        build = {"extra_compiler_options": {
            "pie": True, "no_inline": True, "stack_protector": True},
            "combine_extra_compiler_options": True}
        sets = compiler_option_sets(build)
        self.assertEqual(len(sets), 8)
        self.assertEqual([option_set_label(name) for name, _ in sets], [
            "F/F/F", "F/F/T", "F/T/F", "F/T/T",
            "T/F/F", "T/F/T", "T/T/F", "T/T/T"])
        self.assertEqual([name for name, _ in sets],
                         [option_set_slug(options) for _, options in sets])
        build["combine_extra_compiler_options"] = "true"
        self.assertEqual(compiler_option_sets(build), sets)

    def test_disabled_flag_stays_off_in_every_combination(self):
        sets = compiler_option_sets({
            "extra_compiler_options": {"pie": True, "no_inline": False,
                                        "stack_protector": True},
            "combine_extra_compiler_options": True})
        self.assertEqual([option_set_label(name) for name, _ in sets],
                         ["F/F/F", "F/F/T", "T/F/F", "T/F/T"])

    def test_no_enabled_flags_produce_one_all_off_set(self):
        sets = compiler_option_sets({"combine_extra_compiler_options": True})
        self.assertEqual(len(sets), 1)
        self.assertEqual(sets[0][1], dict.fromkeys(
            ("pie", "no_inline", "stack_protector"), False))

    def test_invalid_combine_and_legacy_modes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "combine_extra_compiler_options"):
            configured_compiler_options({"combine_extra_compiler_options": "yes"})
        with self.assertRaisesRegex(ValueError, "no longer supported"):
            configured_compiler_options({"extra_compiler_option_modes": ["off", "on"]})


if __name__ == "__main__":
    unittest.main()
