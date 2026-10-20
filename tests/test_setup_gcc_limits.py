"""Checks GCC target concurrency settings without starting a build."""

import os
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import setup_gcc


class GccLimitsTests(unittest.TestCase):
    def test_nested_limits_and_subprocess_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "target.conf"
            source.write_text('CT_PARALLEL_JOBS=0\nCT_LOAD=""\nCT_OTHER=y\n')
            commands = []

            def fake_execute(command, **kwargs):
                commands.append((command, kwargs))
                if command.startswith("cp "):
                    _, origin, destination = shlex.split(command)
                    shutil.copyfile(origin, destination)
                return SimpleNamespace(returncode=1 if "build." in command else 0)

            with mock.patch.object(setup_gcc, "get_ctng_conf_file_path", return_value=str(source)), \
                    mock.patch.object(setup_gcc, "execute_command", side_effect=fake_execute), \
                    mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
                result = setup_gcc.build_toolchain(
                    "13.3.0", "x86_64", bootstrap_bin_dir="/bootstrap/bin",
                    compiler_group_dir=str(root / "gcc"),
                )
                self.assertEqual(os.environ["PATH"], "/usr/bin")

            self.assertFalse(result)
            config_text = (root / "gcc/build/13.3.0/x86_64/.config").read_text()
            self.assertIn(f"CT_PARALLEL_JOBS={setup_gcc.NUM_JOBS}", config_text)
            self.assertIn(f'CT_LOAD="{setup_gcc.MAX_JOBS}"', config_text)
            build_command, kwargs = next(item for item in commands if "build." in item[0])
            self.assertIn(f"build.{setup_gcc.NUM_JOBS}", build_command)
            self.assertEqual(kwargs["env_vars"]["PATH"], "/bootstrap/bin")

    def test_default_target_parallelism_is_four(self):
        self.assertEqual(setup_gcc.DEFAULT_PARALLEL_TOOLCHAINS, 4)
        self.assertEqual(setup_gcc.build_all_toolchains.__defaults__[-1], 4)


if __name__ == "__main__":
    unittest.main()
