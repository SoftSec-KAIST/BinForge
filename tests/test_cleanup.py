"""Safety checks for removing compiler build artifacts."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tools = self.root / "tools"
        self.llvm = self.tools / "clang/llvm-project"
        self.clang_logs = self.tools / "clang/logs"
        self.crosstool_ng = self.tools / "crosstool-ng"
        self.ctng_tarballs = self.tools / "ctng_tarballs"
        self.bootstrap = self.tools / "bootstrap/build"
        self.gcc = self.tools / "gcc/build"
        for path in (self.llvm / "build", self.clang_logs, self.crosstool_ng,
                     self.ctng_tarballs,
                     self.bootstrap, self.gcc):
            path.mkdir(parents=True)
            (path / "build.log").write_text("build record")
        (self.tools / "gcc/x86_64-unknown-linux-gnu-13.3.0").mkdir()
        (self.tools / "clang/clang-19.1.7").mkdir()

    def run_cleanup(self, *args, tool_path=None):
        with mock.patch.object(cleanup.config, "TOOL_PATH", str(tool_path or self.tools)):
            with mock.patch("sys.argv", ["cleanup.py", *args]):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    cleanup.main()
        return output.getvalue()

    def test_status_does_not_delete_and_shows_symlink_target(self):
        alias = self.root / "alias"
        alias.symlink_to(self.tools, target_is_directory=True)
        output = self.run_cleanup("--status", tool_path=alias)
        self.assertIn(f"{alias} -> {self.tools}", output)
        for path in cleanup.cleanup_paths(alias):
            self.assertIn(f"Would remove: {path}", output)
            self.assertTrue(path.exists())

    def test_default_removes_requested_build_artifacts(self):
        alias = self.root / "alias"
        alias.symlink_to(self.tools, target_is_directory=True)
        self.run_cleanup(tool_path=alias)
        self.assertTrue(alias.is_symlink())
        for path in (self.llvm, self.clang_logs, self.crosstool_ng,
                     self.ctng_tarballs,
                     self.bootstrap, self.gcc):
            self.assertFalse(path.exists())
        self.assertTrue((self.tools / "gcc/x86_64-unknown-linux-gnu-13.3.0").exists())
        self.assertTrue((self.tools / "clang/clang-19.1.7").exists())

    def test_preflight_rejects_later_symlink_before_deleting_llvm(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        for path in (self.gcc,):
            for child in path.iterdir():
                child.unlink()
            path.rmdir()
        self.gcc.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(SystemExit):
            self.run_cleanup()
        self.assertTrue(self.llvm.exists())
        self.assertTrue((outside / "keep").exists())

    def test_preflight_rejects_intermediate_symlink(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "build").mkdir()
        for child in self.tools.joinpath("gcc/build").iterdir():
            child.unlink()
        self.gcc.rmdir()
        self.tools.joinpath("gcc/x86_64-unknown-linux-gnu-13.3.0").rmdir()
        self.tools.joinpath("gcc").rmdir()
        self.tools.joinpath("gcc").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(SystemExit):
            self.run_cleanup()
        self.assertTrue(self.llvm.exists())
        self.assertTrue((outside / "build").exists())


if __name__ == "__main__":
    unittest.main()
