"""Focused checks for config-driven project builds."""

import argparse
from contextlib import nullcontext
from itertools import product
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import yaml

from binforge.models import Project
from binforge.compiler_options import compiler_option_sets, option_set_slug
from binforge.utils import load_yaml
from binforge.build_monitor_state import read_json
from scripts.compile import (compile_projects, load_build_config, parse_arguments,
                             run_option_combinations, validate_structure)


class CompileConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        pointer_patch = mock.patch("binforge.build_monitor_state.COMPLETED_POINTER",
                                   self.root / "last_completed.json")
        pointer_patch.start()
        self.addCleanup(pointer_patch.stop)
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(
            "build:\n"
            "  opti: [O0]\n"
            "  arch: [x86_64]\n"
            "  compiler: [gcc-13.3.0]\n"
            "targets: [hello]\n"
            "projects:\n"
            "  hello: null\n"
        )

    def test_yaml_null_project_uses_default_apt_source(self):
        config = load_yaml(self.config_path)
        validate_structure(config)
        projects = load_build_config(config, config["targets"], str(self.root))
        self.assertEqual(projects[0].source_type, "apt")
        self.assertEqual(projects[0].source_value, "hello")

    def test_auto_resolve_extra_deps_is_opt_in(self):
        with mock.patch.object(
            sys,
            "argv",
            ["compile.py", "--config", str(self.config_path), "--auto-resolve"],
        ):
            args = parse_arguments()
        self.assertFalse(args.auto_resolve_use_extra_deps)

        with mock.patch.object(
            sys,
            "argv",
            [
                "compile.py",
                "--config",
                str(self.config_path),
                "--auto-resolve",
                "--auto-resolve-use-extra-deps",
            ],
        ):
            args = parse_arguments()
        self.assertTrue(args.auto_resolve_use_extra_deps)

    def test_num_jobs_limits_project_build_workers(self):
        project = Project("hello", str(self.root))
        with mock.patch("scripts.compile.parallel_execute_func", return_value=[True]) as run:
            compile_projects([project], ["hello"], str(self.config_path), num_jobs=2)
        self.assertEqual(run.call_args.kwargs["max_parallel_tasks"], 2)

    def test_project_failure_does_not_skip_later_projects(self):
        config = load_yaml(self.config_path)
        config["targets"] = ["hello", "second"]
        config["projects"]["second"] = None
        self.config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        projects = [Project(name, str(self.root)) for name in config["targets"]]

        with mock.patch("scripts.compile.parallel_execute_func",
                        side_effect=([False], [True])) as run:
            with self.assertRaisesRegex(RuntimeError, "hello: 1 of 1"):
                compile_projects(projects, config["targets"],
                                 str(self.config_path), num_jobs=2)

        self.assertEqual(run.call_count, 2)
        self.assertEqual([call.kwargs["project"].name
                          for call in run.call_args_list],
                         ["hello", "second"])

    def test_num_jobs_must_be_positive(self):
        project = Project("hello", str(self.root))
        with self.assertRaisesRegex(ValueError, "--num-jobs"):
            compile_projects([project], ["hello"], str(self.config_path), num_jobs=0)

    def test_missing_config_does_not_report_success(self):
        project = Project("hello", str(self.root))
        with self.assertRaises(FileNotFoundError):
            compile_projects([project], ["hello"], str(self.root / "missing.yaml"), num_jobs=1)

    def test_empty_build_matrix_is_rejected(self):
        config = load_yaml(self.config_path)
        config["build"]["arch"] = []
        with self.assertRaisesRegex(ValueError, "build.arch"):
            validate_structure(config)

    def test_missing_targets_are_rejected(self):
        config = load_yaml(self.config_path)
        del config["targets"]
        with self.assertRaisesRegex(ValueError, "targets"):
            validate_structure(config)

    def test_option_combinations_create_distinct_configs_and_report_failure(self):
        config = load_yaml(self.config_path)
        config["build"]["extra_compiler_options"] = {
            "pie": True, "no_inline": True, "stack_protector": True}
        config["build"]["combine_extra_compiler_options"] = True
        args = argparse.Namespace(base_path=str(self.root / "build"),
                                  config=str(self.config_path), build=True,
                                  download=False, build_deps=False,
                                  auto_resolve=False, num_jobs=3)
        pointer = self.root / "last_completed.json"
        with mock.patch("binforge.build_monitor_state.COMPLETED_POINTER", pointer), \
             mock.patch("scripts.compile.download_and_extract_projects"), \
             mock.patch("scripts.compile.analyze_projects"), \
             mock.patch("scripts.compile.parallel_execute_func", return_value=[True, False] * 4) as build:
            with self.assertRaisesRegex(RuntimeError, "suite variants failed"):
                run_option_combinations(args, config)
        suite_dir = next((self.root / "build" / "monitor").glob("*/suite.json")).parent
        self.assertEqual(read_json(suite_dir / "suite.json")["state"], "failed")
        self.assertEqual(read_json(pointer)["record_path"], str(suite_dir / "suite.json"))
        self.assertEqual(build.call_count, 1)
        for mode, options in compiler_option_sets(config["build"]):
            generated = yaml.safe_load((suite_dir / f"{mode}.yaml").read_text())
            self.assertEqual(generated["build"]["extra_compiler_options"], options)
            self.assertFalse(generated["build"]["combine_extra_compiler_options"])
        self.assertEqual(build.call_args.kwargs["max_parallel_tasks"], 3)

    def test_option_combinations_forward_source_snapshot(self):
        config = load_yaml(self.config_path)
        config["build"]["extra_compiler_options"] = {"pie": True}
        config["build"]["combine_extra_compiler_options"] = True
        args = argparse.Namespace(base_path=str(self.root / "build"),
                                  config=str(self.config_path), build=True,
                                  download=False, build_deps=False,
                                  auto_resolve=False, num_jobs=3,
                                  source_snapshot=str(self.root / "snapshot"),
                                  source_dataset="binkit", source_mode="paper")
        with mock.patch("scripts.compile.paper_snapshot_root",
                        return_value=nullcontext(self.root)), \
             mock.patch("scripts.compile.materialize_projects") as prepare, \
             mock.patch("scripts.compile.analyze_projects"), \
             mock.patch("scripts.compile.parallel_execute_func", return_value=[True, True]):
            run_option_combinations(args, config)
        self.assertEqual(prepare.call_count, 2)
        self.assertTrue(all(call.args[2] == self.root and call.args[3] == "binkit"
                            for call in prepare.call_args_list))

    def test_option_combinations_finish_project_before_next(self):
        config = load_yaml(self.config_path)
        config["targets"] = ["hello", "second"]
        config["projects"]["second"] = None
        config["build"]["extra_compiler_options"] = {"pie": True}
        config["build"]["combine_extra_compiler_options"] = True
        config["build"]["compiler"] = ["gcc-8.5.0", "gcc-13.3.0"]
        config["build"]["arch"] = ["x86_64", "arm_32"]
        config["build"]["opti"] = ["O0", "O2"]
        args = argparse.Namespace(base_path=str(self.root / "build"),
                                  config=str(self.config_path), build=True,
                                  download=False, build_deps=False,
                                  auto_resolve=False, num_jobs=3)
        def all_succeed(_, jobs, **kwargs):
            return [True] * len(jobs)

        with mock.patch("scripts.compile.download_and_extract_projects"), \
             mock.patch("scripts.compile.analyze_projects"), \
             mock.patch("scripts.compile.parallel_execute_func", side_effect=all_succeed) as build:
            run_option_combinations(args, config)
        self.assertEqual(build.call_count, 2)
        for call, project_name in zip(build.call_args_list, ("hello", "second")):
            jobs = call.args[1]
            observed = [(job[1][0], job[1][1], job[1][2],
                         option_set_slug(job[2]), job[0].name)
                        for job in jobs]
            expected_order = [(*comb, project_name) for comb in product(
                config["build"]["compiler"], config["build"]["arch"],
                config["build"]["opti"],
                [name for name, _ in compiler_option_sets(config["build"])])]
            self.assertEqual(observed, expected_order)
        names = [name for name, _ in compiler_option_sets(config["build"])]
        expected = [("hello", name, f"{name}.yaml") for name in names]
        expected += [("second", name, f"{name}-1.yaml") for name in names]
        suite_dir = next((self.root / "build" / "monitor").glob("*/suite.json")).parent
        for project, mode, name in expected:
            config_path = suite_dir / name
            self.assertEqual(config_path.name, name)
            self.assertEqual(yaml.safe_load(config_path.read_text())["targets"], [project])
            self.assertTrue((self.root / "build" / mode / project / "monitor" / "current.json").is_file())

    def test_option_combinations_forward_source_mode(self):
        config = load_yaml(self.config_path)
        config["build"]["extra_compiler_options"] = {"pie": True}
        config["build"]["combine_extra_compiler_options"] = True
        args = argparse.Namespace(base_path=str(self.root / "build"),
                                  config=str(self.config_path), build=True,
                                  download=False, build_deps=False,
                                  auto_resolve=False, num_jobs=3,
                                  source_snapshot=None, source_mode="remote")
        project = Project("hello", str(self.root))
        project.source_type = "url"
        with mock.patch("scripts.compile.load_build_config", return_value=[project]), \
             mock.patch("scripts.compile.download_and_extract_projects") as prepare, \
             mock.patch("scripts.compile.analyze_projects"), \
             mock.patch("scripts.compile.parallel_execute_func", return_value=[True, True]):
            run_option_combinations(args, config)
        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(project.source_mode, "remote")


if __name__ == "__main__":
    unittest.main()
