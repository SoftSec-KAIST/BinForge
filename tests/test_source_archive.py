"""Archive extraction reports failure before the build stage."""

import tarfile
import tempfile
import unittest
from pathlib import Path

from binforge.utils import unzip_tar


class SourceArchiveTests(unittest.TestCase):
    def test_extracts_archive_into_empty_existing_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "package-1"
            source.mkdir()
            (source / "configure").write_text("configure")
            archive = root / "package.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(source, arcname=source.name)
            destination = root / "package"
            destination.mkdir()
            result = unzip_tar(str(archive), "package")
            self.assertEqual(result, str(destination))
            self.assertEqual((destination / "configure").read_text(), "configure")

    def test_corrupt_archive_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "broken.tar.gz"
            archive.write_text("not an archive")
            with self.assertRaises(RuntimeError):
                unzip_tar(str(archive), "broken")


if __name__ == "__main__":
    unittest.main()
