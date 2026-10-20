"""Checks for the separate-terminal compiler dashboard."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rich.console import Console

from scripts import monitor_compilers


class CompilerMonitorTests(unittest.TestCase):
    def test_tracks_growing_ctng_log_and_restarts_after_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "build.log"
            tracker = monitor_compilers.CtngStageTracker()
            log.write_text("[INFO ]  Installing C library\n[EXTRA]    Building C library\n")
            self.assertEqual(tracker.progress(log), ("Installing C library", "Building C library", 0))

            with log.open("a") as stream:
                stream.write("[INFO ]  Installing C library: done in 10.00s (at 00:10)\n")
                stream.write("[INFO ]  Installing final gcc compiler\n")
            expected = ("Installing final gcc compiler", None, 1)
            self.assertEqual(tracker.progress(log), expected)
            self.assertEqual(tracker.progress(log), expected)

            log.write_text("[INFO ]  Installing kernel headers\n")
            self.assertEqual(tracker.progress(log), ("Installing kernel headers", None, 0))

    def test_two_runners_and_long_log_fit_short_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "build.log"
            log.write_text("[INFO ]  Installing final gcc compiler\n")
            logs = [log]
            for index in range(4):
                extra = Path(directory) / f"clang-{index}_make.log"
                extra.write_text("[42%] Building CXX object\n")
                logs.append(extra)
            data = (
                False,
                [(version, 0, 11) for version in ("8.5.0", "9.5.0", "10.5.0", "11.5.0", "12.4.0", "13.3.0")],
                [(version, False) for version in ("19.1.7", "18.1.8", "17.0.6", "16.0.6", "15.0.7", "14.0.6", "13.0.1", "12.0.1", "11.1.0", "10.0.1", "9.0.1", "8.0.1", "7.1.0")],
                [(123, 120, "python scripts/setup_gcc.py --build-toolchains"),
                 (456, 60, "python scripts/setup_clang.py")],
                logs,
                [("8.5.0", arch, Path(directory), 123 + index, 120)
                 for index, arch in enumerate(("x86_64", "x86_32", "arm_64", "arm_32"))],
            )
            for width, height in ((80, 18), (80, 20), (80, 32), (100, 20)):
                with self.subTest(width=width, height=height):
                    console = Console(width=width, height=height, force_terminal=False)
                    with mock.patch.object(monitor_compilers.shutil, "get_terminal_size", return_value=os.terminal_size((width, height))):
                        with mock.patch.object(monitor_compilers.config, "CLANG_DIR_PATH", Path(directory) / "no-clang"):
                            with console.capture() as capture:
                                console.print(monitor_compilers.render(data, height=height))
                    output = capture.get()
                    self.assertLessEqual(len(output.splitlines()), height)
                    self.assertLess(output.index("RUN" if height < 20 else "Total"),
                                    output.index("Active compiler targets"))
                    if height >= 20:
                        self.assertLess(output.index("Attention"), output.index("Active compiler targets"))
                        self.assertGreaterEqual(output.count("╭"), 4)
                    else:
                        self.assertLess(output.index("Active compiler targets"), output.index("Attention"))
                    self.assertIn("Install matrix", output)
                    self.assertIn("13.3.0", output)
                    self.assertIn("7.1.0", output)
                    self.assertIn("GCC 8.5.0/x86_64", output)
                    if height <= 20:
                        self.assertIn("GCC 8.5.0/arm_32", output)
                        self.assertEqual(len(output.splitlines()), height)
                    if width == 100:
                        self.assertNotIn("…", output)
                    self.assertIn("final gcc compiler", output)
                    self.assertRegex(output, r"GCC 8\.5\.0/x86_64\s+\[\d\d:\d\d:\d\d\]")
                    self.assertRegex(output, r"\[\d\d:\d\d:\d\d\]\s+phase 13/19\s+final gcc compiler")
                    self.assertIn("Clang setup", output)
                    self.assertRegex(output, r"\[run 00:01:00\]\s+phase 1/4")

    def test_repeated_install_substep_is_shown_once(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "build.log"
            log.write_text("[INFO ]  Installing C library\n[EXTRA]    Installing C library\n")
            data = (False, [], [], [], [], [("8.5.0", "arm_64", Path(directory), 123, 60)])
            line = monitor_compilers.active_target_lines(data)[0]
            self.assertIn("C library · 0 done", line)
            self.assertNotIn("/ Installing C library", line)
            self.assertRegex(line, r"\[\d\d:\d\d:\d\d\]\s+phase 11/19\s+C library")

    def test_ctng_phase_uses_top_level_step_not_log_milestone_count(self):
        self.assertEqual(monitor_compilers.ctng_phase_label("Installing core C gcc compiler"), "phase 10/19")
        self.assertEqual(monitor_compilers.ctng_phase_label("Installing final gcc compiler"), "phase 13/19")
        self.assertEqual(monitor_compilers.ctng_phase_label("Installing ncurses for host"), "phase 5/19")
        self.assertEqual(monitor_compilers.ctng_phase_label("Retrieving needed toolchain components' tarballs"), "prep")

    def test_active_detail_fits_100_columns_without_repeating_component(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "build.log"
            log.write_text("[INFO ]  Installing final gcc compiler\n"
                           "[EXTRA]    Building final gcc compiler\n")
            data = (False, [], [], [], [],
                    [("13.3.0", "mipseb_64", Path(directory), 123, 60)])
            line = monitor_compilers.active_target_lines(data)[0]
            self.assertIn("phase 13/19", line)
            self.assertIn("final gcc compiler / build · 0 done", line)
            self.assertLessEqual(len(line) + 2, 100)  # panel borders
        self.assertEqual(
            monitor_compilers.ctng_work_detail("Extracting and patching toolchain components", None, 2),
            "extract/patch components · 2 done",
        )

    def test_phase_column_aligns_with_clang_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "build.log"
            log.write_text("[INFO ]  Installing C library\n")
            data = (False, [], [], [(456, 60, "python scripts/setup_clang.py")], [],
                    [("8.5.0", "arm_64", Path(directory), 123, 60)])
            with mock.patch.object(monitor_compilers.config, "CLANG_DIR_PATH", directory):
                gcc_line, clang_line = monitor_compilers.active_target_lines(data)
            self.assertEqual(gcc_line.index("phase"), clang_line.index("phase"))


if __name__ == "__main__":
    unittest.main()
