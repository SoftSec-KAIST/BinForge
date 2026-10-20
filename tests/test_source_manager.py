"""Source preparation must fail visibly and touch only selected projects."""

import unittest

from binforge.source_manager import download_and_extract_projects


class FakeProject:
    def __init__(self, name, failure=None):
        self.name = name
        self.failure = failure
        self.calls = []

    def download_source_file(self):
        self.calls.append("download")
        if self.failure == "download":
            raise AssertionError("download failed")

    def extract_source_file(self):
        self.calls.append("extract")
        if self.failure == "extract":
            raise ValueError("extract failed")


class SourceManagerTests(unittest.TestCase):
    def test_only_target_is_downloaded_and_extracted(self):
        selected = FakeProject("selected")
        other = FakeProject("other")
        download_and_extract_projects([selected, other], ["selected"])
        self.assertEqual(selected.calls, ["download", "extract"])
        self.assertEqual(other.calls, [])

    def test_download_failure_propagates(self):
        with self.assertRaisesRegex(AssertionError, "download failed"):
            download_and_extract_projects([FakeProject("broken", "download")])

    def test_extraction_failure_propagates(self):
        with self.assertRaisesRegex(ValueError, "extract failed"):
            download_and_extract_projects([FakeProject("broken", "extract")])


if __name__ == "__main__":
    unittest.main()
