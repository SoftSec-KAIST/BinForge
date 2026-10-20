import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from binforge.build_monitor_state import (
    current_run, current_suite, read_json, start_run, start_suite, update_run,
    update_suite, update_variant, variant_id,
)
from scripts.monitor_build import (failed_log, last_log, render, select_snapshot,
                                   snapshot, snapshot_record)
from binforge.build_monitor_discovery import (
    _base_from_argv, discover_base_path, indexed_active_builds,
    latest_completed_record,
)


class BuildMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        pointer_patch = patch("binforge.build_monitor_state.COMPLETED_POINTER",
                              self.base / "last_completed.json")
        pointer_patch.start()
        self.addCleanup(pointer_patch.stop)
        active_patch = patch("binforge.build_monitor_state.ACTIVE_ROOT",
                             self.base / "active")
        active_patch.start()
        self.addCleanup(active_patch.stop)
        self.config = self.base / "config.yaml"
        self.config.write_text("build: fixture\n")
        self.cfg = {
            "targets": ["which", "ccd2cue"],
            "build": {
                "compiler": ["gcc-8.5.0"],
                "arch": ["x86_64"],
                "opti": ["O0"],
                "extra_compiler_options": {"pie": False},
            },
        }

    def test_run_status_is_scoped_and_atomic(self):
        first = start_run(str(self.base), str(self.config), self.cfg)
        self.assertEqual(current_run(self.base), first)
        self.assertEqual(len(read_json(first / "manifest.json")["variants"]), 2)
        comb = ("gcc-8.5.0", "x86_64", "O0")
        update_variant(first, "which", comb, state="running", phase="make")
        update_variant(first, "which", comb, state="failed", phase="failed", error="make exit 2")
        update_run(first, state="failed", phase="failed")
        status = read_json(first / "variants" / f"{variant_id('which', comb)}.json")
        self.assertEqual(status["error"], "make exit 2")
        self.assertIn("ended_at", status)

        second = start_run(str(self.base), str(self.config), self.cfg)
        self.assertNotEqual(first, second)
        self.assertEqual(current_run(self.base), second)
        self.assertEqual(read_json(second / "variants" / f"{variant_id('which', comb)}.json")["state"], "queued")
        self.assertEqual(read_json(first / "run.json")["state"], "failed")

    def test_dependency_run_uses_existing_dashboard(self):
        dependency_cfg = {"targets": ["libatomic", "ncurses"],
                          "build": {"compiler": ["gcc-13.3.0"],
                                    "arch": ["x86_64"], "opti": ["O2"]}}
        run_dir = start_run(str(self.base), str(self.config), dependency_cfg,
                            kind="dependencies")
        update_variant(run_dir, "libatomic", ("gcc-13.3.0", "x86_64", "O2"),
                       state="succeeded", phase="cached")
        data = snapshot(self.base)
        self.assertEqual(data["manifest"]["kind"], "dependencies")
        console = Console(width=80, height=18, force_terminal=False)
        with console.capture() as output:
            console.print(render(data, width=80, height=18))
        self.assertIn("DEPENDENCY BUILD", output.get())
        self.assertIn("1/2 complete", output.get())
        self.assertNotIn("Options", output.get())

    def test_overall_progress_reads_runner_status(self):
        import json
        root = self.base / "batch"
        child_base = root / "projects" / "which"
        child_config = root / "config" / "which.yaml"
        child_config.parent.mkdir(parents=True)
        child_config.write_text(self.config.read_text())
        child_cfg = {**self.cfg, "targets": ["which"]}
        run_dir = start_run(str(child_base), str(child_config), child_cfg)
        (root / "status.json").write_text(json.dumps({
            "targets": ["which", "ccd2cue"], "variants_per_project": 1,
            "projects": {"which": {"state": "running"}},
        }))
        update_variant(run_dir, "which", ("gcc-8.5.0", "x86_64", "O0"),
                       state="running", phase="make")
        data = snapshot(child_base)
        self.assertEqual(len(data["batch"]["targets"]), 2)
        console = Console(width=80, height=20, force_terminal=False)
        with console.capture() as output:
            console.print(render(data, width=80, height=20))
        self.assertIn("Overall progress", output.get())
        self.assertIn("Variants  0/2", output.get())
        self.assertRegex(output.get(), r"Project\s+0/2\s+complete")
        (root / "status.json").write_text(json.dumps({
            "targets": ["which", "ccd2cue"], "variants_per_project": 1,
            "projects": {"which": {"state": "succeeded"}},
        }))
        with console.capture() as output:
            console.print(render(snapshot(child_base), width=80, height=20))
        self.assertIn("Variants  1/2", output.get())
        self.assertRegex(output.get(), r"Project\s+1/2\s+complete")

        update_variant(run_dir, "which", ("gcc-8.5.0", "x86_64", "O0"),
                       state="succeeded", phase="done")
        update_run(run_dir, state="succeeded", phase="done")
        (root / "status.json").write_text(json.dumps({
            "targets": ["which", "ccd2cue"], "variants_per_project": 1,
            "state": "failed", "started_at": "2026-09-18T00:00:00+00:00",
            "ended_at": "2026-09-18T01:00:00+00:00",
            "failed_projects": ["ccd2cue"],
            "projects": {"which": {"state": "succeeded"},
                         "ccd2cue": {"state": "failed"}},
        }))
        with console.capture() as output:
            console.print(render(snapshot(child_base), width=80, height=20))
        screen = output.get()
        self.assertIn("FAILED", screen)
        self.assertIn("Variants  2/2", screen)
        self.assertIn("passed 1", screen)
        self.assertIn("failed 1", screen)
        self.assertIn("1 failed projects", screen)
        self.assertIn("ccd2cue", screen)
        self.assertNotIn("No failed variants", screen)

        failed_names = ["libattr1", "libcaca0", "libevdev2", "libpam-modules",
                        "libtheora0", "libtwolame0", "libxslt1.1", "parted", "sudo"]
        (root / "status.json").write_text(json.dumps({
            "targets": ["which", *failed_names], "variants_per_project": 1,
            "state": "failed", "failed_projects": failed_names,
            "projects": {"which": {"state": "succeeded"},
                         **{name: {"state": "failed"} for name in failed_names}},
        }))
        wide = Console(width=100, height=20, force_terminal=False)
        with wide.capture() as output:
            wide.print(render(snapshot(child_base), width=100, height=20))
        screen = output.get()
        self.assertIn("9 failed projects", screen)
        self.assertIn("libattr1, libcaca0  +7 more", screen)
        self.assertLessEqual(len(screen.splitlines()), 20)

    def test_subset_retry_keeps_original_command_total(self):
        broad = start_run(str(self.base), str(self.config), self.cfg)
        comb = ("gcc-8.5.0", "x86_64", "O0")
        update_variant(broad, "which", comb, state="succeeded", phase="done")
        update_run(broad, state="failed", phase="failed", error="one project failed")

        retry_config = self.base / "retry.yaml"
        retry_config.write_text("build: retry\n")
        retry_cfg = {**self.cfg, "targets": ["ccd2cue"]}
        retry = start_run(str(self.base), str(retry_config), retry_cfg)
        update_variant(retry, "ccd2cue", comb, state="running", phase="make")

        data = snapshot(self.base)
        self.assertEqual(len(data["variants"]), 1)
        self.assertEqual(len(data["command_scope"]["variants"]), 2)
        console = Console(width=100, height=20, force_terminal=False)
        with console.capture() as output:
            console.print(render(data, width=100, height=20))
        screen = output.get()
        self.assertIn("Command  1/2 complete", screen)
        self.assertRegex(screen, r"Project\s+1/2\s+ccd2cue\s+0/1")
        self.assertIn("Active (1)", screen)

    def test_legacy_dependency_config_uses_build_process_directory(self):
        base = self.base / "extra_deps"
        base.mkdir()
        config_path = self.base / "dependency.yaml"
        config_path.write_text("""\
targets: [hello]
projects:
  hello:
    deps: [ncurses]
  ncurses: {}
build:
  arch: [x86_64]
""")
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)",
             "--build-deps", "--config", config_path.name], cwd=self.base)
        self.addCleanup(lambda: (process.terminate(), process.wait()))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if b"--build-deps" in Path(f"/proc/{process.pid}/cmdline").read_bytes():
                break
            time.sleep(0.01)
        with patch("scripts.monitor_build.config.EXTRA_DEP_PATH", str(base)), \
             patch("scripts.monitor_build.active_builds", return_value=[(process.pid, base)]):
            data = snapshot(base)
        self.assertIsNotNone(data)
        self.assertEqual(data["manifest"]["config_path"], str(config_path))
        self.assertEqual(data["variants"][0]["project"], "ncurses")

    def test_screen_fits_common_terminal_sizes(self):
        run_dir = start_run(str(self.base), str(self.config), self.cfg)
        comb = ("gcc-8.5.0", "x86_64", "O0")
        update_variant(run_dir, "which", comb, state="running", phase="make")
        data = snapshot(self.base)
        for width, height in ((100, 20), (80, 24), (80, 20), (80, 18),
                              (80, 16), (60, 14), (40, 10)):
            with self.subTest(width=width, height=height):
                console = Console(width=width, height=height, force_terminal=False)
                with console.capture() as output:
                    console.print(render(data, width=width, height=height))
                screen = output.get()
                self.assertLessEqual(len(screen.splitlines()), height)
                marker = "1 running" if width >= 70 else ("● 1" if width >= 50 else "●1")
                self.assertIn(marker, screen)
                if width == 100 and height == 20:
                    variants_line = next(line for line in screen.splitlines()
                                       if "Configuration variants" in line)
                    self.assertIn("Attention", variants_line)
                    self.assertIn("Active (1)", screen)
                if height < 20:
                    self.assertNotIn("╭", screen)

    def test_missing_process_and_early_failure_are_not_reported_as_success(self):
        run_dir = start_run(str(self.base), str(self.config), self.cfg)
        data = snapshot(self.base)
        data["run"]["pid"] = 999999999
        screen = render(data, width=80, height=16)
        console = Console(width=80, height=16, force_terminal=False)
        with console.capture() as output:
            console.print(screen)
        self.assertIn("PROCESS MISSING / OUTCOME UNKNOWN", output.get())

        update_run(run_dir, state="failed", phase="source", error="archive checksum failed")
        with console.capture() as output:
            console.print(render(snapshot(self.base), width=80, height=16))
        self.assertIn("archive checksum failed", output.get())
        wide = Console(width=100, height=20, force_terminal=False)
        with wide.capture() as output:
            wide.print(render(snapshot(self.base), width=100, height=20))
        self.assertLessEqual(len(output.get().splitlines()), 20)
        self.assertIn("archive checksum failed", output.get())

    def test_completed_run_does_not_flash_process_missing_from_old_snapshot(self):
        run_dir = start_run(str(self.base), str(self.config), self.cfg)
        old_snapshot = snapshot(self.base)
        update_run(run_dir, state="succeeded", phase="done")
        console = Console(width=100, height=20, force_terminal=False)
        with patch("scripts.monitor_build.process_alive", return_value=False), \
             console.capture() as output:
            console.print(render(old_snapshot, width=100, height=20))
        self.assertIn("SUCCEEDED", output.get())
        self.assertNotIn("PROCESS MISSING", output.get())

    def test_completed_suite_does_not_flash_process_missing_from_old_snapshot(self):
        suite_dir = start_suite(self.base, self.config, ["off", "on"], 2,
                                self.cfg, project_mode=True)
        old_snapshot = snapshot(self.base)
        update_suite(suite_dir, state="succeeded", phase="done")
        console = Console(width=100, height=20, force_terminal=False)
        with patch("scripts.monitor_build.process_alive", return_value=False), \
             console.capture() as output:
            console.print(render(old_snapshot, width=100, height=20))
        self.assertIn("SUCCEEDED", output.get())
        self.assertNotIn("PROCESS MISSING", output.get())

    def test_old_logs_are_not_current_activity(self):
        run_dir = start_run(str(self.base), str(self.config), self.cfg)
        directory = self.base / "log" / "which"
        directory.mkdir(parents=True)
        prefix = "which_gcc-8.5.0_x86_64_O0"
        log = directory / f"{prefix}_make.log"
        code = directory / f"{prefix}_make.returncode"
        log.write_text("old build output\n")
        code.write_text("2\n")
        old = time.time() - 3600
        for path in (log, code):
            os.utime(path, (old, old))
        update_variant(run_dir, "which", ("gcc-8.5.0", "x86_64", "O0"),
                       state="running", phase="make")
        row = next(row for row in snapshot(self.base)["variants"] if row["project"] == "which")
        self.assertIsNone(last_log(row, self.base)[1])
        self.assertIsNone(failed_log(row, self.base))

    def test_two_option_modes_have_separate_runs_and_combined_progress(self):
        stale = start_run(str(self.base / "on"), str(self.config), self.cfg)
        update_run(stale, state="succeeded", phase="done")
        suite_dir = start_suite(self.base, self.config, ["off", "on"], 2, self.cfg)
        self.assertEqual(current_suite(self.base), suite_dir)
        self.assertIsNone(current_run(self.base))
        self.assertIsNone(current_run(self.base / "on"))
        off_config = suite_dir / "off.yaml"
        off_config.write_text(self.config.read_text())
        off = start_run(str(self.base / "off"), str(off_config), self.cfg)
        comb = ("gcc-8.5.0", "x86_64", "O0")
        update_variant(off, "which", comb, state="succeeded", phase="done")
        data = snapshot(self.base)
        console = Console(width=80, height=18, force_terminal=False)
        with console.capture() as output:
            console.print(render(data, width=80, height=18))
        screen = output.get()
        self.assertIn("1/4 complete", screen)
        self.assertIn("F/F/F", screen)
        self.assertNotIn("T/T/T", screen)
        wide = Console(width=100, height=20, force_terminal=False)
        with wide.capture() as output:
            wide.print(render(data, width=100, height=20))
        wide_screen = output.get()
        self.assertLessEqual(len(wide_screen.splitlines()), 20)
        self.assertIn("Attention", next(line for line in wide_screen.splitlines()
                                        if "Configuration variants" in line))
        start_run(str(self.base / "off"), str(self.config), self.cfg)
        historical = snapshot_record(suite_dir / "suite.json")
        self.assertEqual(sum(row["state"] == "succeeded" for row in historical["children"]["off"]["variants"]), 1)
        update_suite(suite_dir, state="failed", phase="done", error="on failed")
        self.assertEqual(read_json(suite_dir / "suite.json")["state"], "failed")

    def test_project_major_suite_combines_project_mode_runs(self):
        suite_dir = start_suite(self.base, self.config, ["off", "on"], 2,
                                self.cfg, project_mode=True)
        comb = ("gcc-8.5.0", "x86_64", "O0")
        for mode, project, state in (("off", "which", "succeeded"),
                                     ("on", "which", "succeeded"),
                                     ("off", "ccd2cue", "running")):
            index = self.cfg["targets"].index(project)
            name = f"{mode}.yaml" if index == 0 else f"{mode}-{index}.yaml"
            child_config = suite_dir / name
            child_config.write_text(self.config.read_text())
            child_cfg = {**self.cfg, "targets": [project]}
            run_dir = start_run(str(self.base / mode / project), str(child_config), child_cfg)
            update_variant(run_dir, project, comb, state=state, phase="make")
        data = snapshot(self.base)
        self.assertEqual(len(data["children"]["off"]["variants"]), 2)
        self.assertEqual(len(data["children"]["on"]["variants"]), 1)
        console = Console(width=100, height=20, force_terminal=False)
        with console.capture() as output:
            console.print(render(data, width=100, height=20))
        screen = output.get()
        self.assertIn("2/4 complete", screen)
        self.assertIn("ccd2cue", screen)
        self.assertRegex(screen, r"Project\s+1/2\s+ccd2cue\s+0/2")
        self.assertIn("F/F/F", screen)
        self.assertRegex(screen, r"Flag set\s+0/2\s+F/F/F\s+1/2")
        self.assertNotIn("T/T/T", screen)
        self.assertLessEqual(len(screen.splitlines()), 20)

    def test_active_variants_fill_available_height(self):
        cfg = {**self.cfg, "targets": ["which"],
               "build": {**self.cfg["build"],
                         "arch": [f"arch_{index}" for index in range(6)]}}
        suite_dir = start_suite(self.base, self.config, ["off", "on"], 6, cfg)
        child_config = suite_dir / "off.yaml"
        child_config.write_text(self.config.read_text())
        run_dir = start_run(str(self.base / "off"), str(child_config), cfg)
        for arch in cfg["build"]["arch"]:
            update_variant(run_dir, "which", ("gcc-8.5.0", arch, "O0"),
                           state="running", phase="make")
        data = snapshot(self.base)
        for width, height, visible, hidden in ((100, 20, 2, 4),
                                                (100, 24, 6, 0),
                                                (80, 20, 1, 0)):
            with self.subTest(width=width, height=height):
                console = Console(width=width, height=height, force_terminal=False)
                with console.capture() as output:
                    console.print(render(data, width=width, height=height))
                screen = output.get()
                self.assertLessEqual(len(screen.splitlines()), height)
                active_rows = [line for line in screen.splitlines()
                               if "gcc-8.5.0" in line and "F/F/F" in line and "|" in line]
                self.assertEqual(len(active_rows), visible)
                if hidden:
                    self.assertIn(f"+{hidden} more active variants", screen)
                else:
                    self.assertNotIn("more active variants", screen)
        single_base = self.base / "single"
        single_run = start_run(str(single_base), str(self.config), cfg)
        for arch in cfg["build"]["arch"]:
            update_variant(single_run, "which", ("gcc-8.5.0", arch, "O0"),
                           state="running", phase="make")
        console = Console(width=100, height=20, force_terminal=False)
        with console.capture() as output:
            console.print(render(snapshot(single_base), width=100, height=20))
        screen = output.get()
        self.assertLessEqual(len(screen.splitlines()), 20)
        active_rows = [line for line in screen.splitlines()
                       if "which" in line and "gcc-8.5.0" in line]
        self.assertEqual(len(active_rows), 2)
        self.assertIn("+4 more active variants", screen)

    def test_mixed_options_and_active_column_order_are_explicit(self):
        cfg = {**self.cfg, "targets": ["which"],
               "build": {**self.cfg["build"], "extra_compiler_options": {
                   "pie": False, "no_inline": True, "stack_protector": False,
               }}}
        run_dir = start_run(str(self.base), str(self.config), cfg)
        comb = ("gcc-8.5.0", "x86_64", "O0")
        update_variant(run_dir, "which", comb, state="running", phase="make")
        current = time.time()
        console = Console(width=100, height=20, force_terminal=False)
        with patch("scripts.monitor_build.time.time", return_value=current + 65), \
             console.capture() as output:
            console.print(render(snapshot(self.base), width=100, height=20))
        active = next(line for line in output.get().splitlines()
                      if "which" in line and "gcc-8.5.0" in line)
        self.assertRegex(active, r"F/T/F\s+\|\s+make\s+00:01:05")
        self.assertNotIn("log ", active)

    def test_active_columns_align_across_variant_names(self):
        cfg = {**self.cfg, "targets": ["which"],
               "build": {**self.cfg["build"],
                         "compiler": ["gcc-8.5.0", "clang-13.0.0"],
                         "arch": ["x86_64", "ppceb_32"],
                         "opti": ["O0", "Ofast"]}}
        run_dir = start_run(str(self.base), str(self.config), cfg)
        update_variant(run_dir, "which", ("gcc-8.5.0", "x86_64", "O0"),
                       state="running", phase="make")
        update_variant(run_dir, "which", ("clang-13.0.0", "ppceb_32", "Ofast"),
                       state="running", phase="configure")
        console = Console(width=100, height=24, force_terminal=False)
        with console.capture() as output:
            console.print(render(snapshot(self.base), width=100, height=24))
        lines = output.get().splitlines()
        gcc = next(line for line in lines if "which" in line and "gcc-8.5.0" in line)
        clang = next(line for line in lines if "which" in line and "clang-13.0.0" in line)
        for left, right in (("x86_64", "ppceb_32"), ("O0", "Ofast"),
                            ("make", "configure")):
            self.assertEqual(gcc.index(left), clang.index(right))
        self.assertEqual(gcc.index("|"), clang.index("|"))
        self.assertLessEqual(len(lines), 24)

    def test_discovery_parses_build_base_and_collapses_suite_child(self):
        script = Path(__file__).resolve().parent.parent / "scripts" / "compile.py"
        args = ["python", "scripts/compile.py", "--config=x.yaml",
                "--base-path", "out/run", "--build"]
        self.assertEqual(_base_from_argv(args, script.parent.parent, script),
                         script.parent.parent / "out" / "run")
        self.assertIsNone(_base_from_argv(args[:-1], script.parent.parent, script))
        with patch("binforge.build_monitor_discovery.config.EXTRA_DEP_PATH", str(self.base)):
            self.assertEqual(_base_from_argv(["python", "scripts/compile.py", "--build-deps"],
                                             script.parent.parent, script), self.base)
        with patch("binforge.build_monitor_discovery.active_builds",
                   return_value=[(100, self.base), (101, self.base / "off")]), \
             patch("binforge.build_monitor_discovery.current_suite",
                   side_effect=lambda path: self.base / "suite" if path == self.base else None), \
             patch("binforge.build_monitor_discovery.read_json",
                   return_value={"pid": 100, "modes": ["off", "on"]}):
            self.assertEqual(discover_base_path(), (self.base, None))
        with patch("binforge.build_monitor_discovery.active_builds",
                   return_value=[(100, self.base), (101, self.base / "off" / "which")]), \
             patch("binforge.build_monitor_discovery.current_suite",
                   side_effect=lambda path: self.base / "suite" if path == self.base else None), \
             patch("binforge.build_monitor_discovery.read_json",
                   return_value={"pid": 100, "modes": ["off", "on"]}):
            self.assertEqual(discover_base_path(), (self.base, None))
        with patch("binforge.build_monitor_discovery.active_builds",
                   return_value=[(100, self.base), (200, self.base / "other")]), \
             patch("binforge.build_monitor_discovery.current_suite", return_value=None):
            selected, message = discover_base_path()
            self.assertIsNone(selected)
            self.assertIn("Multiple builds", message)

    def test_status_index_discovers_in_process_build_and_removes_it_on_finish(self):
        run_dir = start_run(str(self.base), str(self.config), self.cfg)
        self.assertEqual(indexed_active_builds(), [(os.getpid(), self.base.resolve())])
        with patch("binforge.build_monitor_discovery.active_builds", return_value=[]):
            self.assertEqual(discover_base_path(), (self.base.resolve(), None))
        update_run(run_dir, state="succeeded", phase="done")
        self.assertEqual(indexed_active_builds(), [])

    def test_no_process_uses_newest_completed_status(self):
        first = start_run(str(self.base / "first"), str(self.config), self.cfg)
        update_run(first, state="succeeded", phase="done")
        second = start_run(str(self.base / "second"), str(self.config), self.cfg)
        update_run(second, state="failed", phase="failed", error="make failed")
        third = start_run(str(self.base / "third"), str(self.config), self.cfg)
        pointer = self.base / "last_completed.json"
        self.assertEqual(latest_completed_record([self.base], pointer), second / "run.json")
        self.assertEqual(snapshot_record(second / "run.json")["run"]["state"], "failed")
        pointer.unlink()
        self.assertEqual(latest_completed_record([self.base], pointer), second / "run.json")
        self.assertEqual(read_json(third / "run.json")["state"], "running")

    def test_completed_fallback_does_not_recursively_scan_output_files(self):
        completed = start_run(str(self.base / "completed"), str(self.config), self.cfg)
        update_run(completed, state="succeeded", phase="done")
        stale_pointer = self.base / "stale.json"
        stale_pointer.write_text('{"record_path": "/missing/run.json"}\n')
        with patch.object(Path, "rglob", side_effect=AssertionError("recursive scan")):
            self.assertEqual(latest_completed_record([self.base], stale_pointer),
                             completed / "run.json")

    def test_live_monitor_keeps_selected_build_after_process_exits(self):
        run_dir = start_run(str(self.base), str(self.config), self.cfg)
        with patch("scripts.monitor_build.discover_base_path",
                   return_value=(self.base, None)):
            active, selected, message = select_snapshot(None)
        self.assertEqual(active["run"]["state"], "running")
        self.assertEqual(selected, self.base)
        self.assertIsNone(message)

        update_run(run_dir, state="succeeded", phase="done")
        with patch("scripts.monitor_build.discover_base_path",
                   return_value=(None, "Waiting for a running BinForge compile.py --build.")), \
             patch("scripts.monitor_build.latest_completed_record", return_value=None):
            completed, selected, message = select_snapshot(selected)
        self.assertEqual(completed["run"]["state"], "succeeded")
        self.assertEqual(selected, self.base)
        self.assertIsNone(message)


if __name__ == "__main__":
    unittest.main()
