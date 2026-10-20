"""Checks for the guarded Autotools source-regeneration fallback."""

from contextlib import nullcontext
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

from binforge.compile_manager import compile_project
from binforge.autotools_fallback import variant_failure_fingerprint
from binforge.conf_manager import (find_stale_autotools_outputs,
                                   regenerated_autotools_source)
from binforge.models import Project
from binforge.utils import CommandResult
from scripts.compile import compile_projects


class AutoreconfFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(yaml.safe_dump({
            "build": {
                "opti": ["O0"],
                "arch": ["x86_64"],
                "compiler": ["gcc-8.5.0", "gcc-13.3.0"],
            },
            "targets": ["hello"],
            "projects": {"hello": None},
        }, sort_keys=False), encoding="utf-8")

    def make_project(self) -> Project:
        project = Project("hello", str(self.root / "build"))
        source_root = Path(project.source_path) / project.name
        source_root.mkdir(parents=True)
        project.log_path = str(self.root / "logs")
        project.use_configure = True
        project.configure_file_path = str(source_root / "configure")

        configure = source_root / "configure"
        configure.write_text("#!/bin/sh\n", encoding="utf-8")
        configure_ac = source_root / "configure.ac"
        configure_ac.write_text("AC_INIT([hello], [1])\n", encoding="utf-8")
        os.utime(configure, (100, 100))
        os.utime(configure_ac, (200, 200))
        return project

    def test_detects_only_generated_outputs_older_than_inputs(self):
        project = self.make_project()
        stale = find_stale_autotools_outputs(project)
        self.assertEqual([(source.name, generated.name)
                          for source, generated in stale],
                         [("configure.ac", "configure")])

        configure = Path(project.configure_file_path)
        os.utime(configure, (300, 300))
        self.assertEqual(find_stale_autotools_outputs(project), [])

    def test_regeneration_uses_an_isolated_source_copy(self):
        project = self.make_project()
        source_root = Path(project.source_path) / project.name
        (source_root / "m4").mkdir()
        command_result = CommandResult(
            command="autoreconf",
            stdout="prepared",
            stderr="",
            returncode=0,
        )

        with mock.patch("binforge.conf_manager.execute_command",
                        return_value=command_result) as execute:
            with regenerated_autotools_source(project) as prepared:
                self.assertIsNotNone(prepared)
                prepared_root = Path(prepared.source_path) / prepared.name
                self.assertNotEqual(prepared_root, source_root)
                self.assertTrue((prepared_root / "configure.ac").is_file())
                self.assertIn("-I m4", execute.call_args.args[0])
                temporary_parent = Path(prepared.source_path)

        self.assertFalse(temporary_parent.exists())
        self.assertTrue(source_root.is_dir())
        self.assertEqual(
            (Path(project.log_path) / "hello.autoreconf.log").read_text().strip(),
            "prepared",
        )

    def test_repeated_failure_adopts_regenerated_source_for_remaining_variants(self):
        project = self.make_project()
        regenerated = Project("hello", str(self.root / "regenerated"))
        regenerated.source_path = str(self.root / "prepared")
        fingerprint = (("make", 2, ("error: stale Makefile.in",)),)

        with mock.patch("binforge.autotools_fallback.compile_probe_variant",
                        side_effect=[False, False, True]) as probe, \
             mock.patch("binforge.autotools_fallback.variant_failure_fingerprint",
                        side_effect=[fingerprint, fingerprint]), \
             mock.patch("binforge.autotools_fallback.regenerated_autotools_source",
                        return_value=nullcontext(regenerated)) as regenerate, \
             mock.patch("scripts.compile.parallel_execute_func",
                        return_value=[True]) as parallel:
            compile_projects([project], ["hello"], str(self.config_path), 2)

        self.assertEqual(probe.call_count, 3)
        regenerate.assert_called_once_with(project)
        self.assertIs(parallel.call_args.kwargs["project"], regenerated)
        self.assertEqual(parallel.call_args.kwargs["dynamic_values_list"],
                         [("gcc-13.3.0", "x86_64", "O0")])

    def test_failed_regeneration_keeps_original_source_and_runs_remaining_variants(self):
        project = self.make_project()
        fingerprint = (("make", 2, ("error: stale Makefile.in",)),)

        with mock.patch("binforge.autotools_fallback.compile_probe_variant",
                        side_effect=[False, False]), \
             mock.patch("binforge.autotools_fallback.variant_failure_fingerprint",
                        side_effect=[fingerprint, fingerprint]), \
             mock.patch("binforge.autotools_fallback.regenerated_autotools_source",
                        return_value=nullcontext(None)), \
             mock.patch("scripts.compile.parallel_execute_func",
                        return_value=[True]) as parallel:
            with self.assertRaisesRegex(RuntimeError, "hello: 1 of 2"):
                compile_projects([project], ["hello"],
                                 str(self.config_path), 2)

        self.assertIs(parallel.call_args.kwargs["project"], project)
        self.assertEqual(parallel.call_args.kwargs["dynamic_values_list"],
                         [("gcc-13.3.0", "x86_64", "O0")])

    def test_probe_does_not_publish_a_temporary_terminal_state(self):
        project = Project("hello", str(self.root))
        combination = ("gcc-13.3.0", "x86_64", "O0")
        with mock.patch("binforge.compile_manager._compile_project_impl",
                        return_value=False), \
             mock.patch("binforge.compile_manager._record_variant") as record:
            result = compile_project(
                project,
                combination,
                PATH="",
                monitor_run_dir=str(self.root / "monitor"),
                publish_result=False,
            )

        self.assertFalse(result)
        self.assertEqual(record.call_count, 1)
        self.assertEqual(record.call_args.kwargs["state"], "running")

    def test_failure_fingerprint_includes_the_option_mode_tag(self):
        project = self.make_project()
        project.variant_tag = "flags-111"
        log_dir = Path(project.log_path)
        log_dir.mkdir(parents=True, exist_ok=True)
        prefix = "hello_gcc-13.3.0_x86_64_O0_flags-111"
        (log_dir / f"{prefix}_make.returncode").write_text("2", encoding="utf-8")
        (log_dir / f"{prefix}_make.error").write_text(
            "fatal: generated header is missing\n", encoding="utf-8"
        )

        fingerprint = variant_failure_fingerprint(
            project, ("gcc-13.3.0", "x86_64", "O0")
        )

        self.assertEqual(
            fingerprint,
            (("make", 2, ("fatal: generated header is missing",)),),
        )


if __name__ == "__main__":
    unittest.main()
