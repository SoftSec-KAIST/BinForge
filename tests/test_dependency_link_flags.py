"""Dependency search paths must resolve transitive shared-library links."""

import tempfile
import unittest
from pathlib import Path

from binforge.compile_manager import _state, add_dep
from binforge.models import Project


class DependencyLinkFlagsTests(unittest.TestCase):
    def test_transitive_library_directory_is_available_to_linker(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Project("consumer", temporary)
            project.dep_path = temporary
            libdir = Path(temporary) / "install/x86_64/libuuid/lib"
            libdir.mkdir(parents=True)
            state = _state()
            state.extra_cflags = ""
            state.extra_ldflags = ""
            state.extra_dep_pkg_config_path = ""
            add_dep(project, "libuuid", arch="x86_64")
            self.assertIn(f"-L{libdir}", state.extra_ldflags)
            self.assertIn(f"-Wl,-rpath-link,{libdir}", state.extra_ldflags)


if __name__ == "__main__":
    unittest.main()
