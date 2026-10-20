"""Compatibility checks between automatic repair and configured builds."""

from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import binforge.llm_manager as llm_manager
import binforge.run_openai_api as run_openai_api
from binforge import config
from binforge.models import Project
from binforge.compiler_options import option_set_slug


class LlmBuildCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def make_stale_project(self) -> Project:
        project = Project("hello", str(self.root / "build"))
        source_root = Path(project.source_path) / project.name
        source_root.mkdir(parents=True)
        project.log_path = str(self.root / "logs")
        project.use_configure = True
        project.configure_file_path = str(source_root / "configure")
        project.cflags = "-DTEST"
        project.ldflags = "-lm"
        project.conf_options = "--disable-nls"

        configure = source_root / "configure"
        configure.write_text("#!/bin/sh\n", encoding="utf-8")
        configure_ac = source_root / "configure.ac"
        configure_ac.write_text("AC_INIT([hello], [1])\n", encoding="utf-8")
        configure.touch()
        configure_ac.touch()
        configure.chmod(0o755)
        configure_time = 100
        source_time = 200
        os.utime(configure, (configure_time, configure_time))
        os.utime(configure_ac, (source_time, source_time))
        return project

    def test_prompt_paths_follow_the_installed_module(self):
        module_dir = Path(llm_manager.__file__).resolve().parent
        self.assertEqual(llm_manager.PROMPT_PATH, module_dir / "prompt.txt")
        self.assertEqual(
            llm_manager.PROMPT_NO_DEP_LOOKUP_PATH,
            module_dir / "prompt_no_dep_lookup.txt",
        )
        self.assertEqual(
            run_openai_api.DEFAULT_PROMPT_PATH,
            Path(run_openai_api.__file__).resolve().with_name("prompt.txt"),
        )
        self.assertTrue(llm_manager.PROMPT_PATH.is_file())
        self.assertTrue(llm_manager.PROMPT_NO_DEP_LOOKUP_PATH.is_file())

        previous_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            self.assertTrue(run_openai_api.load_prompt_template())
        finally:
            os.chdir(previous_cwd)

    def test_auto_resolve_uses_guarded_autotools_fallback(self):
        project = self.make_stale_project()
        node = llm_manager.DepNode(project)
        regenerated = Project("hello", str(self.root / "regenerated"))
        fingerprint = (("make", 2, ("error: stale Makefile.in",)),)

        with mock.patch.object(llm_manager, "analyze_projects"), \
             mock.patch("binforge.autotools_fallback.compile_probe_variant",
                        side_effect=[False, False, True]) as probe, \
             mock.patch("binforge.autotools_fallback.variant_failure_fingerprint",
                        side_effect=[fingerprint, fingerprint]), \
             mock.patch("binforge.autotools_fallback.regenerated_autotools_source",
                        return_value=nullcontext(regenerated)) as regenerate:
            result = llm_manager.compile_project_wrapper(node)

        self.assertTrue(result)
        self.assertEqual(probe.call_count, 3)
        regenerate.assert_called_once_with(project)
        compile_kwargs = probe.call_args.args[5]
        self.assertEqual(compile_kwargs["cflags"], "-DTEST")
        self.assertEqual(compile_kwargs["ldflags"], "-lm")
        self.assertEqual(compile_kwargs["conf_options"], "--disable-nls")

    def test_auto_resolve_dependency_root_is_isolated_by_default(self):
        project = Project("hello", str(self.root / "build"))

        selected = llm_manager.select_auto_resolve_dependency_path(project)

        self.assertEqual(selected, str(Path(project.log_path) / "deps"))
        self.assertEqual(project.dep_path, selected)
        self.assertTrue(Path(selected).is_dir())

    def test_auto_resolve_can_use_accumulated_extra_deps(self):
        project = Project("hello", str(self.root / "build"))
        extra_deps = self.root / "tools" / "extra_deps"

        with mock.patch.object(config, "EXTRA_DEP_PATH", str(extra_deps)):
            selected = llm_manager.select_auto_resolve_dependency_path(
                project,
                use_extra_deps=True,
            )

        self.assertEqual(selected, str(extra_deps))
        self.assertEqual(project.dep_path, str(extra_deps))
        self.assertTrue(extra_deps.is_dir())

    def test_auto_resolve_forwards_extra_deps_choice_to_each_target(self):
        project = Project("hello", str(self.root / "build"))

        with mock.patch.object(llm_manager, "download_and_extract_projects"), \
             mock.patch.object(llm_manager, "analyze_projects"), \
             mock.patch.object(llm_manager, "initialize_solver_state"), \
             mock.patch.object(
                 llm_manager,
                 "auto_resolve_target_project",
             ) as resolve_target:
            llm_manager.auto_resolve(
                [project],
                [project.name],
                use_extra_deps=True,
            )

        resolve_target.assert_called_once_with(project, use_extra_deps=True)

    def test_auto_resolve_reports_a_failed_target(self):
        projects = [
            Project("first", str(self.root / "build")),
            Project("second", str(self.root / "build")),
        ]

        with mock.patch.object(llm_manager, "download_and_extract_projects"), \
             mock.patch.object(llm_manager, "analyze_projects"), \
             mock.patch.object(llm_manager, "initialize_solver_state"), \
             mock.patch.object(
                 llm_manager,
                 "auto_resolve_target_project",
                 side_effect=[True, False],
             ) as resolve_target:
            success = llm_manager.auto_resolve(
                projects,
                [project.name for project in projects],
            )

        self.assertFalse(success)
        self.assertEqual(resolve_target.call_count, 2)

    def test_auto_resolve_expands_selected_option_combinations(self):
        config_path = self.root / "modes.yaml"
        config_path.write_text(
            "build:\n"
            "  compiler: [gcc-13.3.0]\n"
            "  arch: [x86_64]\n"
            "  opti: [O0, O2]\n"
            "  extra_compiler_options: {pie: true, no_inline: true, stack_protector: true}\n"
            "  combine_extra_compiler_options: true\n",
            encoding="utf-8",
        )

        llm_manager.initialize_solver_build_context(str(config_path))

        variants = llm_manager.BUILD_VARIANTS
        self.assertEqual(len(variants), 16)
        self.assertEqual(
            [(variant.optmz, variant.flag_bits)
             for variant in variants],
            [(optmz, f"{bits:03b}") for optmz in ("O0", "O2")
             for bits in range(8)],
        )
        self.assertTrue(all(variant.option_mode ==
                            option_set_slug(variant.extra_compiler_options)
                            for variant in variants))

    def test_option_combinations_use_distinct_build_prefixes_and_state_records(self):
        config_path = self.root / "modes.yaml"
        config_path.write_text(
            "build:\n"
            "  compiler: [gcc-13.3.0]\n"
            "  arch: [x86_64]\n"
            "  opti: [O0]\n"
            "  extra_compiler_options: {pie: true, no_inline: true, stack_protector: true}\n"
            "  combine_extra_compiler_options: true\n",
            encoding="utf-8",
        )
        llm_manager.initialize_solver_build_context(str(config_path))

        observed = []
        project = Project("hello", str(self.root / "build"))
        project.cflags = ""
        project.ldflags = ""
        project.conf_options = ""
        node = llm_manager.DepNode(project)

        def capture(project, **kwargs):
            observed.append(
                (project.variant_tag, llm_manager.current_log_prefix(project.name))
            )
            return True

        with mock.patch.object(llm_manager, "analyze_projects"), \
             mock.patch.object(llm_manager, "guarded_autotools_probe",
                               return_value=nullcontext(None)), \
             mock.patch.object(llm_manager, "compile_project", side_effect=capture):
            for variant in llm_manager.BUILD_VARIANTS:
                llm_manager.set_solver_build_context(variant)
                llm_manager.compile_project_wrapper(node)

        self.assertEqual(
            observed,
            [(f"flags-{bits:03b}",
              f"hello_gcc-13.3.0_x86_64_O0_flags-{bits:03b}")
             for bits in range(8)],
        )

        llm_manager.SOURCE_CONFIG_PATH = str(config_path)
        llm_manager.initialize_solver_state(project.base_path, [project.name])
        state = json.loads(
            Path(llm_manager.get_solver_state_path(project.base_path)).read_text()
        )
        self.assertEqual(
            [(item["option_mode"], item["flag_set"])
             for item in state["build_variants"]],
            [(variant.option_mode, variant.flag_bits)
             for variant in llm_manager.BUILD_VARIANTS],
        )


if __name__ == "__main__":
    unittest.main()
