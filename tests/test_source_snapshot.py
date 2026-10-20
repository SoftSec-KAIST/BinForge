import hashlib
from collections import Counter
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from binforge.source_snapshot import (PAPER_DEFAULT_BUNDLE, PAPER_MANIFEST,
                                      PAPER_WORK_ROOT, materialize_projects,
                                      materialize_source,
                                      paper_snapshot_root, tree_sha256)


class SourceSnapshotTests(unittest.TestCase):
    def test_snapshot_storage_uses_single_data_root(self):
        data_root = Path.home() / "archive_binforge"
        self.assertEqual(PAPER_DEFAULT_BUNDLE,
                         data_root / "snapshots" / "paper-sources.tar")
        self.assertEqual(PAPER_WORK_ROOT, data_root / "cache" / "snapshots")

    def test_active_snapshot_matches_promoted_dataset_closure(self):
        manifest = json.loads(PAPER_MANIFEST.read_text())
        self.assertEqual(manifest["schema"], 2)
        tools = manifest["source_preparation"]["tools"]
        self.assertEqual(tools["autoconf"]["package_version"], "2.71-3")
        self.assertEqual(tools["automake"]["package_version"],
                         "1:1.16.5-1.3ubuntu1")
        self.assertEqual(tools["libtool"]["package_version"], "2.4.7-7build1")
        self.assertEqual(tools["m4"]["package_version"], "1.4.19-4build1")
        self.assertEqual(tools["autopoint"]["package_version"],
                         "0.21-14ubuntu2")
        self.assertEqual(tools["gettext"]["package_version"],
                         "0.21-14ubuntu2")
        self.assertEqual(tools["cmake"]["package_version"],
                         "3.28.3-1build7")
        self.assertEqual(tools["dpkg-source"]["package_version"],
                         "1.22.6ubuntu6.6")
        self.assertEqual(tools["apt"]["package_version"], "2.8.3")
        apt_repository = manifest["source_preparation"]["apt_repository"]
        self.assertEqual(apt_repository["os_release"]["version_id"], "24.04")
        self.assertEqual(apt_repository["os_release"]["version_codename"],
                         "noble")
        self.assertEqual(apt_repository["architecture"], "amd64")
        self.assertIsNone(apt_repository["apt_snapshot"])
        self.assertEqual(apt_repository["base_files_package_version"],
                         "13ubuntu10.4")
        self.assertEqual(apt_repository["ubuntu_keyring_package_version"],
                         "2023.11.28.1")
        self.assertEqual(
            {release["suite"] for release in apt_repository["signed_releases"]},
            {"noble", "noble-updates", "noble-security", "noble-backports"},
        )
        binkit_zlib = manifest["projects"]["binkit/zlib"]
        popcon_zlib = manifest["projects"]["popcon/zlib"]
        self.assertEqual(binkit_zlib["version"], "1.3.1")
        self.assertEqual(binkit_zlib["md5"], "9855b6d802d7fe5b7bd5b196a2271655")
        self.assertEqual(binkit_zlib["artifacts"], popcon_zlib["artifacts"])
        self.assertEqual(binkit_zlib["tree_sha256"], popcon_zlib["tree_sha256"])
        self.assertNotIn("parent_bundle", manifest)
        self.assertEqual(len([key for key in manifest["projects"]
                              if key.startswith("binkit/")]), 58)
        self.assertEqual(len([key for key in manifest["projects"]
                              if key.startswith("popcon/")]), 130)
        self.assertNotIn("popcon/libcaca0", manifest["projects"])
        self.assertNotIn("popcon/libxml2__dep", manifest["projects"])
        preparations = [entry["source_preparation"]
                        for entry in manifest["projects"].values()]
        self.assertEqual(
            Counter(item["mode"] for item in preparations),
            Counter({"distributed-autoconf": 149,
                     "runtime-autoreconf": 25,
                     "cmake": 14}),
        )
        self.assertEqual(
            sum(item["guarded_autoreconf_fallback_candidate"]
                for item in preparations),
            18,
        )
        self.assertTrue(all(item["scope_analysis"]["in_scope"]
                            for item in preparations))
        libvo = manifest["projects"]["popcon/libvo-amrwbenc0"]
        self.assertEqual(libvo["version"], "0.1.3-2build1")
        self.assertEqual(libvo["tree_sha256"],
                         "29777acac3e25c5fe4a93db01300b2bc5c8c28b992fc20b928ed68461c2b1a3b")
        diffutils = manifest["projects"]["popcon/diffutils"]
        self.assertEqual(diffutils["version"], "1:3.10-1build1")
        self.assertEqual(diffutils["tree_sha256"],
                         "ee29065bcf1279a9d790360cd828d29d5651a55fea1606021ce3c025246cb93c")
        root = Path(__file__).resolve().parents[1]
        for dataset in ("binkit", "popcon"):
            config = root / "config" / f"{dataset}.yaml"
            self.assertEqual(manifest["datasets"][dataset],
                             hashlib.sha256(config.read_bytes()).hexdigest())

    def test_materialize_accepts_matching_pinned_apt_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manifest.json").write_text(json.dumps({
                "projects": {
                    "popcon/diffutils": {
                        "type": "apt",
                        "requested": "diffutils=1:3.10-1build1",
                    },
                },
            }))
            project = SimpleNamespace(
                name="diffutils",
                source_type="apt",
                source_value="diffutils",
                version="1:3.10-1build1",
                source_path=str(root / "work"),
            )
            with patch("binforge.source_snapshot.materialize_source") as materialize:
                materialize_projects([project], ["diffutils"], root, "popcon")
            materialize.assert_called_once()

    def test_materialize_rejects_different_pinned_apt_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manifest.json").write_text(json.dumps({
                "projects": {
                    "popcon/diffutils": {
                        "type": "apt",
                        "requested": "diffutils=1:3.10-1build1",
                    },
                },
            }))
            project = SimpleNamespace(
                name="diffutils",
                source_type="apt",
                source_value="diffutils",
                version="1:3.10-1ubuntu0.24.04.1",
                source_path=str(root / "work"),
            )
            with self.assertRaisesRegex(ValueError, "APT source name differs"):
                materialize_projects([project], ["diffutils"], root, "popcon")

    def test_materialize_keeps_unversioned_apt_source_compatible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manifest.json").write_text(json.dumps({
                "projects": {
                    "popcon/base-passwd": {
                        "type": "apt",
                        "requested": "base-passwd",
                    },
                },
            }))
            project = SimpleNamespace(
                name="base-passwd",
                source_type="apt",
                source_value="base-passwd",
                version="latest",
                source_path=str(root / "work"),
            )
            with patch("binforge.source_snapshot.materialize_source") as materialize:
                materialize_projects([project], ["base-passwd"], root, "popcon")
            materialize.assert_called_once()

    def test_materialization_rejects_modified_tree_and_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sources" / "binkit" / "example"
            source.mkdir(parents=True)
            archive = source / "example.tar.gz"
            pristine = root / "pristine" / "example-1"
            pristine.mkdir(parents=True)
            (pristine / "file.c").write_text("int main(void) { return 0; }\n")
            with tarfile.open(archive, "w:gz") as output:
                output.add(pristine, arcname="example-1")
            entry = {"type": "url", "status": "captured",
                     "tree_sha256": tree_sha256(pristine),
                     "artifacts": [{"name": archive.name, "size": archive.stat().st_size,
                                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}]}
            destination = root / "build" / "example"
            self.assertEqual(materialize_source(root, "binkit", "example", entry,
                                                destination), entry["tree_sha256"])
            (destination / "file.c").write_text("tampered\n")
            with self.assertRaisesRegex(ValueError, "differs from snapshot"):
                materialize_source(root, "binkit", "example", entry, destination)
            (destination / "file.c").unlink()
            destination.rmdir()
            archive.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                materialize_source(root, "binkit", "example", entry, destination)

    def test_paper_bundle_requires_matching_checksum_and_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = root / "2026-09-18.json"
            bundle = root / "2026-09-18-current.tar"
            contents = root / "contents"
            contents.mkdir()
            artifact = contents / "sources" / "binkit" / "example" / "source.tar.gz"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"source")
            manifest = {"projects": {"binkit/example": {
                "status": "captured", "artifacts": [{"name": artifact.name,
                    "size": artifact.stat().st_size,
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}]}}}
            expected.write_text(json.dumps(manifest) + "\n")
            (contents / "manifest.json").write_bytes(expected.read_bytes())
            with tarfile.open(bundle, "w") as archive:
                archive.add(contents / "manifest.json", arcname="manifest.json")
                archive.add(contents / "sources", arcname="sources")
            checksum = root / "2026-09-18.tar.sha256"
            checksum.write_text(f"{hashlib.sha256(bundle.read_bytes()).hexdigest()}  {bundle.name}\n")
            with patch("binforge.source_snapshot.PAPER_MANIFEST", expected), \
                 patch("binforge.source_snapshot.PAPER_TAR_SHA256", checksum):
                with paper_snapshot_root(bundle, root / "work") as opened:
                    self.assertEqual((opened / "manifest.json").read_bytes(),
                                     expected.read_bytes())
                    cached = opened
                self.assertTrue(cached.is_dir())
                with paper_snapshot_root(bundle, root / "work") as opened:
                    self.assertEqual(opened, cached)
                renamed_bundle = root / "renamed-copy.tar"
                shutil.copy2(bundle, renamed_bundle)
                with paper_snapshot_root(renamed_bundle, root / "renamed-work") as opened:
                    self.assertEqual((opened / "manifest.json").read_bytes(),
                                     expected.read_bytes())
                cached_artifact = cached / "sources/binkit/example/source.tar.gz"
                cached_artifact.write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    with paper_snapshot_root(bundle, root / "work"):
                        pass
                cached_artifact.write_bytes(b"source")
                (cached / "manifest.json").write_text("changed\n")
                with self.assertRaisesRegex(ValueError, "manifest differs"):
                    with paper_snapshot_root(bundle, root / "work"):
                        pass
                (cached / "manifest.json").write_bytes(expected.read_bytes())
                bundle.write_bytes(b"tampered")
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    with paper_snapshot_root(bundle, root / "work"):
                        pass


if __name__ == "__main__":
    unittest.main()
