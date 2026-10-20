"""Offline verification and extraction of captured project sources."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
from tempfile import TemporaryDirectory


PAPER_SNAPSHOT_ID = "paper-sources"
PAPER_MANIFEST = Path(__file__).resolve().parent.parent / "config" / "source-snapshots" / f"{PAPER_SNAPSHOT_ID}.json"
PAPER_TAR_SHA256 = PAPER_MANIFEST.with_suffix(".tar.sha256")
PAPER_DATA_ROOT = Path.home() / "archive_binforge"
PAPER_DEFAULT_BUNDLE = PAPER_DATA_ROOT / "snapshots" / f"{PAPER_SNAPSHOT_ID}.tar"
PAPER_WORK_ROOT = PAPER_DATA_ROOT / "cache" / "snapshots"


@contextmanager
def paper_snapshot_root(snapshot: Path, work_dir: Path):
    """Verify a paper tar and keep its checked working copy under archive."""
    snapshot = snapshot.expanduser().resolve()
    expected_manifest = PAPER_MANIFEST.read_bytes()
    if not snapshot.is_file():
        raise FileNotFoundError(f"Paper source snapshot missing: {snapshot}")
    checksum, _canonical_filename = PAPER_TAR_SHA256.read_text().strip().split(maxsplit=1)
    if file_sha256(snapshot) != checksum:
        raise ValueError(f"Paper source bundle checksum mismatch: {snapshot}")
    work_dir.mkdir(parents=True, exist_ok=True)
    cache = work_dir / PAPER_SNAPSHOT_ID
    if cache.is_symlink():
        raise ValueError(f"Refusing symlinked paper source cache: {cache}")
    if not cache.exists():
        with TemporaryDirectory(prefix=".paper-snapshot-", dir=work_dir) as temporary:
            staged = Path(temporary) / "extracted"
            staged.mkdir()
            with tarfile.open(snapshot, "r:*") as archive:
                archive.extractall(staged, filter="data")
            _verify_paper_cache(staged, expected_manifest)
            os.replace(staged, cache)
    _verify_paper_cache(cache, expected_manifest)
    yield cache


def _verify_paper_cache(root: Path, expected_manifest: bytes) -> None:
    if (root / "manifest.json").read_bytes() != expected_manifest:
        raise ValueError(f"Paper source manifest differs from {PAPER_MANIFEST}")
    manifest = json.loads(expected_manifest)
    for key, entry in manifest["projects"].items():
        dataset, name = key.split("/", 1)
        verify_artifacts(root, dataset, name, entry)


def file_sha256(path: Path) -> str:
    result = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def tree_sha256(root: Path) -> str:
    """Hash names, types, modes, file bytes, and symlink targets; ignore mtimes."""
    result = sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            kind, detail = "link", os.readlink(path)
        elif stat.S_ISDIR(mode):
            kind, detail = "dir", ""
        elif stat.S_ISREG(mode):
            kind, detail = "file", file_sha256(path)
        else:
            raise ValueError(f"Unsupported source tree entry: {path}")
        record = [relative, kind, stat.S_IMODE(mode), detail]
        result.update((json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
    return result.hexdigest()


def verify_artifacts(snapshot_root: Path, dataset: str, name: str, entry: dict) -> Path:
    if entry.get("status") != "captured" or not entry.get("artifacts"):
        raise ValueError(f"Source is not fully captured: {dataset}/{name}")
    directory = snapshot_root / "sources" / dataset / name
    for artifact in entry["artifacts"]:
        filename = artifact["name"]
        if Path(filename).name != filename or filename in (".", ".."):
            raise ValueError(f"Invalid artifact name: {filename}")
        path = directory / filename
        if (not path.is_file() or path.stat().st_size != artifact["size"]
                or file_sha256(path) != artifact["sha256"]):
            raise ValueError(f"Source artifact checksum mismatch: {path}")
    return directory


def extract_source(snapshot_root: Path, dataset: str, name: str,
                   entry: dict, destination: Path) -> None:
    directory = verify_artifacts(snapshot_root, dataset, name, entry)
    if destination.exists():
        raise FileExistsError(destination)
    if entry["type"] == "apt":
        dsc = [item["name"] for item in entry["artifacts"] if item["name"].endswith(".dsc")]
        if len(dsc) != 1:
            raise ValueError(f"Expected one .dsc: {dataset}/{name}")
        result = subprocess.run(["dpkg-source", "-x", str(directory / dsc[0]), str(destination)],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError(f"dpkg-source failed for {dataset}/{name}: {result.stderr[-500:]}")
    elif entry["type"] in ("url", "url_git"):
        if len(entry["artifacts"]) != 1:
            raise ValueError(f"Expected one URL archive: {dataset}/{name}")
        archive = directory / entry["artifacts"][0]["name"]
        with TemporaryDirectory(dir=destination.parent) as temporary:
            staging = Path(temporary)
            with tarfile.open(archive, "r:*") as source:
                source.extractall(staging, filter="data")
            roots = list(staging.iterdir())
            if len(roots) != 1 or not roots[0].is_dir():
                raise ValueError(f"Expected one source root in {archive}")
            shutil.move(str(roots[0]), destination)
    else:
        raise ValueError(f"Unsupported source type: {entry['type']}")


def materialize_source(snapshot_root: Path, dataset: str, name: str,
                       entry: dict, destination: Path) -> str:
    expected = entry.get("tree_sha256")
    if not expected:
        raise ValueError(f"Source tree digest is not sealed: {dataset}/{name}")
    if destination.exists():
        observed = tree_sha256(destination)
        if observed != expected:
            raise ValueError(f"Existing source tree differs from snapshot: {destination}")
        return observed
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as temporary:
        staged = Path(temporary) / name
        extract_source(snapshot_root, dataset, name, entry, staged)
        observed = tree_sha256(staged)
        if observed != expected:
            raise ValueError(f"Extracted source tree differs from snapshot: {dataset}/{name}")
        os.replace(staged, destination)
    return observed


def materialize_projects(projects: list, targets: list[str], snapshot_root: Path,
                         dataset: str) -> None:
    manifest_path = snapshot_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest["projects"]
    for project in projects:
        if project.name not in targets:
            continue
        key = f"{dataset}/{project.name}"
        if key not in entries:
            raise ValueError(f"Project is absent from source snapshot: {key}")
        entry = entries[key]
        if project.source_type != entry["type"]:
            raise ValueError(f"Source type differs from snapshot: {key}")
        if entry["type"] == "url":
            if (project.source_value != entry["url"] or
                    (entry.get("md5") and str(project.archive_md5).lower() != entry["md5"].lower())):
                raise ValueError(f"URL or archive checksum differs from snapshot: {key}")
        elif entry["type"] == "url_git":
            if project.source_value != entry["url"] or project.source_commit != entry["commit"]:
                raise ValueError(f"Git URL or commit differs from snapshot: {key}")
        elif entry["type"] == "apt":
            source_name = project.source_value or project.name
            version = str(project.version or "").strip()
            requested = (f"{source_name}={version}"
                         if version and version != "latest" else source_name)
            if requested != entry["requested"]:
                raise ValueError(f"APT source name differs from snapshot: {key}")
        materialize_source(snapshot_root, dataset, project.name, entry,
                           Path(project.source_path) / project.name)
