"""Dependency builds cover the requested target architectures."""

import tempfile
import unittest
from unittest import mock

from binforge.models import Project
from binforge.extra_deps_manager import DEFAULT_DEP_JOBS, compile_dep, compile_extra_deps


class ExtraDepsTests(unittest.TestCase):
    def test_uses_configured_architectures(self):
        with tempfile.TemporaryDirectory() as root:
            app = Project("app", root)
            app.dep_list = ["lib"]
            lib = Project("lib", root)
            lib.dep_list = []
            with mock.patch("binforge.extra_deps_manager.config.EXTRA_DEP_PATH", root):
                with mock.patch("binforge.extra_deps_manager.download_and_extract_projects"):
                    with mock.patch("binforge.extra_deps_manager.analyze_projects"):
                        with mock.patch("binforge.extra_deps_manager.install_deps") as install:
                            compile_extra_deps([app, lib], ["app"], ["x86_64"])
            self.assertEqual(install.call_args.args[1], ["x86_64"])
            self.assertEqual([p.name for p in install.call_args.args[0]], ["lib"])
            self.assertEqual(DEFAULT_DEP_JOBS, 32)
            self.assertEqual(install.call_args.kwargs["jobs"], DEFAULT_DEP_JOBS)

    def test_dependency_depths_finish_before_dependent_depth(self):
        with tempfile.TemporaryDirectory() as root:
            app = Project("app", root)
            app.dep_list = ["middle", "other"]
            middle = Project("middle", root)
            middle.dep_list = ["lib"]
            lib = Project("lib", root)
            lib.dep_list = []
            other = Project("other", root)
            other.dep_list = []
            with mock.patch("binforge.extra_deps_manager.config.EXTRA_DEP_PATH", root), \
                 mock.patch("binforge.extra_deps_manager.download_and_extract_projects"), \
                 mock.patch("binforge.extra_deps_manager.analyze_projects"), \
                 mock.patch("binforge.extra_deps_manager.install_deps") as install:
                compile_extra_deps([app, middle, lib, other], ["app"], ["x86_64"])
            self.assertEqual([[project.name for project in call.args[0]]
                              for call in install.call_args_list],
                             [["lib", "other"], ["middle"]])
            self.assertTrue(all(call.kwargs["jobs"] == DEFAULT_DEP_JOBS
                                for call in install.call_args_list))

    def test_paper_dependencies_never_download(self):
        with tempfile.TemporaryDirectory() as root:
            app = Project("app", root)
            app.dep_list = ["lib"]
            lib = Project("lib", root)
            lib.dep_list = []
            with mock.patch("binforge.extra_deps_manager.config.EXTRA_DEP_PATH", root), \
                 mock.patch("binforge.extra_deps_manager.materialize_projects") as materialize, \
                 mock.patch("binforge.extra_deps_manager.download_and_extract_projects") as download, \
                 mock.patch("binforge.extra_deps_manager.analyze_projects"), \
                 mock.patch("binforge.extra_deps_manager.tree_sha256", return_value="fixed"), \
                 mock.patch("binforge.extra_deps_manager.install_deps") as install:
                compile_extra_deps([app, lib], ["app"], ["x86_64"],
                                   snapshot_root=root, source_dataset="binkit", jobs=2)
            download.assert_not_called()
            self.assertEqual(materialize.call_args.args[2:], (root, "binkit"))
            self.assertEqual(install.call_args.kwargs,
                             {"jobs": 2, "monitor_run_dir": None})
            self.assertEqual(install.call_args.args[0][0].source_tree_sha256, "fixed")

    def test_popcon_dependencies_use_common_install_root(self):
        with tempfile.TemporaryDirectory() as root:
            app = Project("app", root)
            app.dep_list = ["lib"]
            lib = Project("lib", root)
            lib.dep_list = []
            with mock.patch("binforge.extra_deps_manager.config.EXTRA_DEP_PATH", root), \
                 mock.patch("binforge.extra_deps_manager.materialize_projects"), \
                 mock.patch("binforge.extra_deps_manager.analyze_projects"), \
                 mock.patch("binforge.extra_deps_manager.tree_sha256", return_value="fixed"), \
                 mock.patch("binforge.extra_deps_manager.install_deps") as install:
                compile_extra_deps([app, lib], ["app"], ["x86_64"],
                                   snapshot_root=root, source_dataset="popcon")
            self.assertEqual(lib.base_path, root)
            self.assertEqual(lib.dep_path, root)

    def test_dependency_is_skipped_only_with_completion_marker(self):
        with tempfile.TemporaryDirectory() as root:
            lib = Project("lib", root)
            lib.source_tree_sha256 = "fixed"
            def installed(project, *args, **kwargs):
                from pathlib import Path
                (Path(project.install_prefix) / "library.a").write_bytes(b"archive")
                return True
            with mock.patch("binforge.extra_deps_manager.compile_project", side_effect=installed) as build:
                compile_dep(lib, "x86_64", root)
                compile_dep(lib, "x86_64", root)
                self.assertEqual(build.call_count, 1)
                lib.source_tree_sha256 = "changed"
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    compile_dep(lib, "x86_64", root)


if __name__ == "__main__":
    unittest.main()
