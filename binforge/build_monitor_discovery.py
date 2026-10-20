"""Find the one active config build for the separate-terminal monitor."""

from __future__ import annotations

import os
from pathlib import Path

from binforge import build_monitor_state
from binforge.build_monitor_state import (
    COMPLETED_POINTER,
    current_suite,
    process_alive,
    read_json,
)
from binforge import config


def _base_from_argv(argv: list[str], cwd: Path, script: Path) -> Path | None:
    is_deps = "--build-deps" in argv
    if (("--build" not in argv and not is_deps) or
            "--download" in argv or "--auto-resolve" in argv):
        return None
    found_script = False
    for arg in argv:
        if arg.endswith("compile.py"):
            candidate = Path(arg)
            if (candidate if candidate.is_absolute() else cwd / candidate).resolve() == script:
                found_script = True
                break
    if not found_script:
        return None
    if is_deps:
        return Path(config.EXTRA_DEP_PATH).resolve()
    for index, arg in enumerate(argv):
        if arg.startswith("--base-path="):
            value = arg.split("=", 1)[1]
            break
        if arg == "--base-path" and index + 1 < len(argv):
            value = argv[index + 1]
            break
    else:
        value = "~/base"
    path = Path(value).expanduser()
    return (path if path.is_absolute() else cwd / path).resolve()


def active_builds(proc_root: Path = Path("/proc")) -> list[tuple[int, Path]]:
    script = (Path(__file__).resolve().parent.parent / "scripts" / "compile.py").resolve()
    found = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            argv = [part.decode("utf-8", "surrogateescape") for part in
                    (entry / "cmdline").read_bytes().split(b"\0") if part]
            cwd = (entry / "cwd").resolve(strict=True)
            base = _base_from_argv(argv, cwd, script)
        except (OSError, UnicodeError, ValueError):
            continue
        if base is not None:
            found.append((int(entry.name), base))
    return found


def indexed_active_builds(active_root: Path | None = None) -> list[tuple[int, Path]]:
    """Read live builds registered by the status writer, including wrappers."""
    root = active_root or build_monitor_state.ACTIVE_ROOT
    found = []
    if not root.is_dir():
        return found
    for pointer in root.glob("*.json"):
        item = read_json(pointer)
        if not item or not process_alive(item.get("pid"), item.get("pid_start_ticks")):
            continue
        record_path = item.get("record_path")
        base_path = item.get("base_path")
        if not isinstance(record_path, str) or not isinstance(base_path, str):
            continue
        record = read_json(Path(record_path))
        if not record or record.get("state") != "running":
            continue
        found.append((item["pid"], Path(base_path).resolve()))
    return found


def discover_base_path(proc_root: Path = Path("/proc")) -> tuple[Path | None, str | None]:
    builds = indexed_active_builds() + active_builds(proc_root)
    builds = list(dict.fromkeys(builds))
    if not builds:
        return None, "Waiting for a running BinForge compile.py build."
    # A two-mode suite runs a child compile.py for each mode. Its controller is
    # the one logical build and owns the combined monitor path.
    controllers = []
    for pid, base in builds:
        suite_dir = current_suite(base)
        suite = read_json(suite_dir / "suite.json") if suite_dir else None
        if suite and suite.get("pid") == pid:
            controllers.append((pid, base, set(suite.get("modes", []))))
    ignored = {(pid, base) for pid, base in builds
               if any(base.is_relative_to(root / mode)
                      for _, root, modes in controllers for mode in modes)}
    logical = [(pid, base) for pid, base in builds if (pid, base) not in ignored]
    unique = list(dict.fromkeys(base for _, base in logical))
    if len(unique) == 1:
        return unique[0], None
    paths = ", ".join(str(path) for path in unique)
    return None, f"Multiple builds are running ({paths}). Pass --base-path to select one."


def latest_completed_record(search_roots: list[Path] | None = None,
                            pointer: Path | None = None) -> Path | None:
    """Locate the newest finalized status, including runs before the index existed."""
    pointer = pointer or COMPLETED_POINTER
    roots = search_roots if search_roots is not None else [
        Path(__file__).resolve().parent.parent / "out", Path.home() / "base",
    ]
    candidates = set()
    selected = read_json(pointer)
    if selected and isinstance(selected.get("record_path"), str):
        candidates.add(Path(selected["record_path"]))
    # The fallback covers existing local runs made before last_completed.json. Follow
    # only bounded current pointers: recursively globbing run.json would walk source,
    # binary, log, and per-variant trees that can contain millions of files.
    # New completed runs are found through the index even if --base-path is elsewhere.
    if not any((state := read_json(path)) and state.get("state") in ("succeeded", "failed")
               for path in candidates):
        for root in roots:
            if not root.is_dir():
                continue
            for pattern in ("monitor/current.json", "*/monitor/current.json",
                            "*/*/monitor/current.json", "*/*/*/monitor/current.json"):
                for current_path in root.glob(pattern):
                    current = read_json(current_path)
                    run_id = current.get("run_id") if current else None
                    if (not isinstance(run_id, str) or Path(run_id).name != run_id or
                            run_id in (".", "..")):
                        continue
                    name = "suite.json" if current.get("kind") == "suite" else "run.json"
                    candidates.add(current_path.parent / run_id / name)
    latest = None
    for path in candidates:
        state = read_json(path)
        if not state or state.get("state") not in ("succeeded", "failed"):
            continue
        ended = state.get("ended_at")
        if isinstance(ended, (int, float)) and (latest is None or ended > latest[0]):
            latest = (ended, path)
    return latest[1] if latest else None
