"""URL acquisition choices should make network and cache behavior predictable."""

import hashlib
from io import BytesIO
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from binforge.models import Project


class SourceModeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root / "fixture" / "spell-1.1"
        source.mkdir(parents=True)
        (source / "configure").write_text("original\n")
        self.payload = self.root / "fixture.tar.gz"
        with tarfile.open(self.payload, "w:gz") as archive:
            archive.add(source, arcname=source.name)
        self.md5 = hashlib.md5(self.payload.read_bytes()).hexdigest()

    def project(self, base: str, mode: str = "auto") -> Project:
        project = Project("spell", str(self.root / base))
        project.source_type = "url"
        project.source_value = "https://ftp.gnu.org/gnu/spell/spell-1.1.tar.gz"
        project.version = "1.1"
        project.archive_group = "gnu"
        project.archive_name = "spell.tar.gz"
        project.archive_md5 = self.md5
        project.archive_root = str(self.root / "archive")
        project.source_mode = mode
        return project

    def test_auto_populates_cache_then_reuses_it(self):
        first = self.project("first")
        def fetch(url, dst):
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.payload, dst)
        first.download_file = Mock(side_effect=fetch)
        first.download_source_file()
        first.extract_source_file()
        self.assertEqual(first.download_file.call_count, 1)
        self.assertTrue(Path(first.get_archive_local_path()).is_file())

        second = self.project("second")
        second.download_file = Mock(side_effect=AssertionError("network used"))
        second.download_source_file()
        second.extract_source_file()
        second.download_file.assert_not_called()
        self.assertEqual((Path(second.source_path) / "spell" / "configure").read_text(),
                         "original\n")

    def test_archive_mode_never_downloads_and_reextracts(self):
        project = self.project("archive-only", "archive")
        project.download_file = Mock(side_effect=AssertionError("network used"))
        with self.assertRaises(FileNotFoundError):
            project.download_source_file()
        cache = Path(project.get_archive_local_path())
        cache.parent.mkdir(parents=True)
        shutil.copy2(self.payload, cache)
        project.download_source_file()
        project.extract_source_file()
        extracted = Path(project.source_path) / "spell" / "configure"
        extracted.write_text("modified\n")
        project.download_source_file()
        project.extract_source_file()
        self.assertEqual(extracted.read_text(), "original\n")
        project.download_file.assert_not_called()
        cache.write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "Invalid local archive"):
            project.download_source_file()
        project.download_file.assert_not_called()

    def test_remote_mode_ignores_cache_and_reextracts(self):
        project = self.project("force-remote", "remote")
        cache = Path(project.get_archive_local_path())
        cache.parent.mkdir(parents=True)
        shutil.copy2(self.payload, cache)
        project.download_file = Mock(side_effect=lambda url, dst: shutil.copy2(self.payload, dst))
        project.download_source_file()
        project.extract_source_file()
        extracted = Path(project.source_path) / "spell" / "configure"
        extracted.write_text("modified\n")
        project.download_source_file()
        project.extract_source_file()
        self.assertEqual(project.download_file.call_count, 2)
        self.assertEqual(extracted.read_text(), "original\n")

    def test_gnu_download_falls_back_and_checks_digest(self):
        project = self.project("mirror", "remote")
        content = self.payload.read_bytes()
        seen = []
        def response(request, timeout):
            seen.append(request.full_url)
            if "ftp.kaist.ac.kr" in request.full_url:
                raise HTTPError(request.full_url, 503, "busy", None, None)
            return BytesIO(content)
        destination = self.root / "download.tar.gz"
        with patch("binforge.models.urlopen", side_effect=response):
            project.download_file(project.source_value, str(destination))
        self.assertEqual(len(seen), 2)
        self.assertIn("ftpmirror.gnu.org", seen[1])
        self.assertEqual(destination.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
