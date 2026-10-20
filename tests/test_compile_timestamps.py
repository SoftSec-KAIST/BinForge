"""Timestamp workarounds must preserve source ordering and retry narrowly."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from binforge import compile_manager


class CompileTimestampTests(unittest.TestCase):
    def test_normalization_preserves_generated_file_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            makefile_am = source / "Makefile.am"
            makefile_in = source / "Makefile.in"
            makefile_am.write_text("input", encoding="utf-8")
            makefile_in.write_text("generated", encoding="utf-8")

            now = time.time()
            os.utime(makefile_am, (now + 20, now + 20))
            os.utime(makefile_in, (now + 10, now + 10))

            with mock.patch.object(compile_manager.time, "time", return_value=now):
                offset = compile_manager.normalize_future_timestamps(temporary)

            normalized_am = makefile_am.stat().st_mtime
            normalized_in = makefile_in.stat().st_mtime
            self.assertGreater(offset, 0)
            self.assertAlmostEqual(normalized_am - normalized_in, 10, places=4)
            self.assertGreater(normalized_am, normalized_in)
            self.assertLessEqual(normalized_am, now - 2 + 0.001)

    def test_exact_automake_clock_error_retries_once_and_keeps_first_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "project_configure.log"
            log_path.write_text(
                "configure: error: newly created file is older than distributed files!\n",
                encoding="utf-8",
            )
            failed = SimpleNamespace(returncode=1)
            succeeded = SimpleNamespace(returncode=0)

            with mock.patch.object(
                compile_manager,
                "execute_command",
                side_effect=[failed, succeeded],
            ) as execute, mock.patch.object(
                compile_manager,
                "wait_for_automake_clock_retry",
                return_value=0.0,
            ):
                result = compile_manager.run_configure_with_clock_retry(
                    "./configure",
                    temporary,
                    str(log_path),
                    str(Path(temporary) / "configure"),
                )

            self.assertIs(result, succeeded)
            self.assertEqual(execute.call_count, 2)
            first_log = Path(temporary) / "project_configure.clock-sanity-attempt1.log"
            self.assertEqual(first_log.read_text(encoding="utf-8"), log_path.read_text(encoding="utf-8"))

    def test_other_configure_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "project_configure.log"
            log_path.write_text("configure: error: missing dependency\n", encoding="utf-8")
            failed = SimpleNamespace(returncode=1)

            with mock.patch.object(
                compile_manager,
                "execute_command",
                return_value=failed,
            ) as execute, mock.patch.object(
                compile_manager,
                "wait_for_automake_clock_retry",
            ) as wait_for_retry:
                result = compile_manager.run_configure_with_clock_retry(
                    "./configure",
                    temporary,
                    str(log_path),
                    str(Path(temporary) / "configure"),
                )

            self.assertIs(result, failed)
            execute.assert_called_once()
            wait_for_retry.assert_not_called()

    def test_exact_automake_clock_error_during_make_retries_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "project_make.log"
            error_path = Path(temporary) / "project_make.error"
            log_path.write_text("partial build output\n", encoding="utf-8")
            error_path.write_text(
                "configure: error: newly created file is older than distributed files!\n",
                encoding="utf-8",
            )
            failed = SimpleNamespace(returncode=2)
            succeeded = SimpleNamespace(returncode=0)

            with mock.patch.object(
                compile_manager,
                "execute_command",
                side_effect=[failed, succeeded],
            ) as execute, mock.patch.object(
                compile_manager,
                "wait_for_automake_clock_retry",
                return_value=0.0,
            ):
                result = compile_manager.run_make_with_clock_retry(
                    "make",
                    temporary,
                    str(log_path),
                    str(error_path),
                    str(Path(temporary) / "configure"),
                )

            self.assertIs(result, succeeded)
            self.assertEqual(execute.call_count, 2)
            self.assertEqual(
                (Path(temporary) / "project_make.clock-sanity-attempt1.log").read_text(
                    encoding="utf-8"
                ),
                "partial build output\n",
            )
            self.assertEqual(
                (Path(temporary) / "project_make.clock-sanity-attempt1.error").read_text(
                    encoding="utf-8"
                ),
                error_path.read_text(encoding="utf-8"),
            )

    def test_other_make_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "project_make.log"
            error_path = Path(temporary) / "project_make.error"
            log_path.write_text("compiler failed\n", encoding="utf-8")
            error_path.write_text("fatal error: missing.h\n", encoding="utf-8")
            failed = SimpleNamespace(returncode=2)

            with mock.patch.object(
                compile_manager,
                "execute_command",
                return_value=failed,
            ) as execute, mock.patch.object(
                compile_manager,
                "wait_for_automake_clock_retry",
            ) as wait_for_retry:
                result = compile_manager.run_make_with_clock_retry(
                    "make",
                    temporary,
                    str(log_path),
                    str(error_path),
                    str(Path(temporary) / "configure"),
                )

            self.assertIs(result, failed)
            execute.assert_called_once()
            wait_for_retry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
