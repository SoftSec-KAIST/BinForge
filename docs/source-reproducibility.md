# Project sources

The normal source mode uses the projects and versions selected by the YAML configuration. BinForge can download URL sources or obtain APT source packages; these inputs can change as upstream repositories change.

Paper mode uses the fixed `paper-sources.tar` bundle instead. The files prepared for the Zenodo deposit include the tar, its manifest, checksum, and source provenance and license notices. Its [manifest](../config/source-snapshots/paper-sources.json) and [SHA-256 checksum](../config/source-snapshots/paper-sources.tar.sha256) are also tracked in Git.

Zenodo record: ZENODO_RECORD_URL

Download `paper-sources.tar` from that record and place it at:

```text
~/archive_binforge/snapshots/paper-sources.tar
```

From the repository root, you can check the tar before building:

```bash
repo_root="$PWD"
(cd "$HOME/archive_binforge/snapshots" && \
  sha256sum -c "$repo_root/config/source-snapshots/paper-sources.tar.sha256")
```

`--source-mode paper --source-dataset binkit` or `popcon` selects the matching sources. BinForge checks the bundle and source trees, keeps a verified working copy under `~/archive_binforge/cache/snapshots/`, and does not download project sources in this mode. The same mode works with `--build-deps` and `--build`; it does not apply to `--auto-resolve`.

For ordinary URL projects, `--source-mode auto` (the default) reuses a valid local archive or downloads it; `remote` forces a new download; `archive` requires a valid local per-project archive. The latter two modes apply to URL projects only. The local download cache is under `~/archive_binforge/cache/downloads/`.
