"""Small, atomic status records shared by a build and its read-only viewer."""

from __future__ import annotations

from hashlib import sha256
from itertools import product
import json
import os
from pathlib import Path
import time
from uuid import uuid4


COMPLETED_POINTER = Path(__file__).resolve().parent.parent / "out" / "monitor" / "last_completed.json"
ACTIVE_ROOT = Path(__file__).resolve().parent.parent / "out" / "monitor" / "active"


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def variant_id(project: str, comb: tuple[str, str, str]) -> str:
    value = json.dumps([project, *comb], separators=(",", ":"))
    return sha256(value.encode()).hexdigest()[:20]


def _pid_start(pid: int) -> str | None:
    try:
        # /proc/PID/stat includes a parenthesized command, which may contain spaces.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def process_alive(pid: int, start_ticks: str | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    observed = _pid_start(pid)
    if start_ticks is not None:
        return observed == start_ticks
    return observed is not None


def _active_path(run_dir: Path) -> Path:
    return ACTIVE_ROOT / f"{run_dir.name}.json"


def _register_active(run_dir: Path, base_path: str | Path, kind: str,
                     started_at: float) -> None:
    pid = os.getpid()
    _atomic_json(_active_path(run_dir), {
        "schema": 1,
        "kind": kind,
        "record_path": str((run_dir / ("suite.json" if kind == "suite" else "run.json")).resolve()),
        "base_path": str(Path(base_path).resolve()),
        "pid": pid,
        "pid_start_ticks": _pid_start(pid),
        "started_at": started_at,
    })


def _unregister_active(run_dir: Path) -> None:
    _active_path(run_dir).unlink(missing_ok=True)


def start_run(base_path: str, config_path: str, cfg: dict,
              *, kind: str = "projects") -> Path:
    monitor_root = Path(base_path) / "monitor"
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    run_dir = monitor_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    source = Path(config_path).read_bytes()
    build = cfg["build"]
    variants = []
    for project in cfg["targets"]:
        for comb in product(build["compiler"], build["arch"], build["opti"]):
            variants.append({
                "id": variant_id(project, comb),
                "project": project,
                "compiler": comb[0],
                "arch": comb[1],
                "opti": comb[2],
            })
    if len({item["id"] for item in variants}) != len(variants):
        raise ValueError("Configuration-variant list contains duplicates")
    now = time.time()
    _atomic_json(run_dir / "manifest.json", {
        "schema": 1,
        "kind": kind,
        "run_id": run_id,
        "base_path": str(Path(base_path).resolve()),
        "config_path": str(Path(config_path).resolve()),
        "config_sha256": sha256(source).hexdigest(),
        "options": build.get("extra_compiler_options") or {},
        "variants": variants,
    })
    for item in variants:
        _atomic_json(run_dir / "variants" / f"{item['id']}.json", {
            "state": "queued", "phase": "queued", "updated_at": now,
        })
    _atomic_json(run_dir / "run.json", {
        "state": "running", "phase": "source", "started_at": now,
        "updated_at": now, "pid": os.getpid(),
        "pid_start_ticks": _pid_start(os.getpid()),
    })
    _atomic_json(monitor_root / "current.json", {"run_id": run_id})
    _register_active(run_dir, base_path, kind, now)
    return run_dir


def update_run(run_dir: Path, *, state: str = "running", phase: str | None = None,
               error: str | None = None) -> None:
    path = run_dir / "run.json"
    value = read_json(path) or {}
    value.update(state=state, updated_at=time.time())
    if phase is not None:
        value["phase"] = phase
    if error is not None:
        value["error"] = error[:500]
    if state != "running":
        value["ended_at"] = time.time()
    _atomic_json(path, value)
    if state in ("succeeded", "failed"):
        _atomic_json(COMPLETED_POINTER, {"record_path": str(path.resolve()),
                                         "ended_at": value["ended_at"]})
        _unregister_active(run_dir)


def update_variant(run_dir: str | Path, project: str, comb: tuple[str, str, str],
                   *, state: str, phase: str, error: str | None = None) -> None:
    path = Path(run_dir) / "variants" / f"{variant_id(project, comb)}.json"
    value = read_json(path) or {}
    now = time.time()
    value.update(state=state, phase=phase, updated_at=now, pid=os.getpid())
    if state == "running" and "started_at" not in value:
        value["started_at"] = now
    if state in ("succeeded", "failed"):
        value["ended_at"] = now
    if error is not None:
        value["error"] = error[:500]
    _atomic_json(path, value)


def current_run(base_path: str | Path) -> Path | None:
    root = Path(base_path) / "monitor"
    pointer = read_json(root / "current.json")
    if not pointer or pointer.get("kind") == "suite" or not isinstance(pointer.get("run_id"), str):
        return None
    run_id = pointer["run_id"]
    if Path(run_id).name != run_id or run_id in (".", ".."):
        return None
    run_dir = root / run_id
    return run_dir if run_dir.is_dir() else None


def start_suite(base_path: str | Path, config_path: str | Path, modes: list[str],
                variants_per_mode: int, cfg: dict | None = None,
                project_mode: bool = False) -> Path:
    root = Path(base_path) / "monitor"
    suite_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    suite_dir = root / suite_id
    suite_dir.mkdir(parents=True, exist_ok=False)
    now = time.time()
    _atomic_json(suite_dir / "suite.json", {
        "schema": 1, "state": "running", "phase": "prepare",
        "modes": modes, "variants_per_mode": variants_per_mode,
        "project_mode": project_mode,
        "axes": {key: cfg["build"][key] for key in ("compiler", "arch", "opti")} if cfg else {},
        "targets": cfg["targets"] if cfg else [],
        "base_path": str(Path(base_path).resolve()),
        "config_path": str(Path(config_path).resolve()),
        "config_sha256": sha256(Path(config_path).read_bytes()).hexdigest(),
        "started_at": now, "updated_at": now, "pid": os.getpid(),
        "pid_start_ticks": _pid_start(os.getpid()),
    })
    for mode in modes:
        # Do not show a previous suite's completed mode before this child starts.
        _atomic_json(Path(base_path) / mode / "monitor" / "current.json",
                     {"kind": "pending", "suite_id": suite_id})
    _atomic_json(root / "current.json", {"kind": "suite", "run_id": suite_id})
    _register_active(suite_dir, base_path, "suite", now)
    return suite_dir


def update_suite(suite_dir: Path, *, state: str = "running", phase: str | None = None,
                 error: str | None = None) -> None:
    path = suite_dir / "suite.json"
    value = read_json(path) or {}
    value.update(state=state, updated_at=time.time())
    if phase is not None:
        value["phase"] = phase
    if error is not None:
        value["error"] = error[:500]
    if state != "running":
        value["ended_at"] = time.time()
    _atomic_json(path, value)
    if state in ("succeeded", "failed"):
        _atomic_json(COMPLETED_POINTER, {"record_path": str(path.resolve()),
                                         "ended_at": value["ended_at"]})
        _unregister_active(suite_dir)


def current_suite(base_path: str | Path) -> Path | None:
    root = Path(base_path) / "monitor"
    pointer = read_json(root / "current.json")
    if not pointer or pointer.get("kind") != "suite":
        return None
    suite_id = pointer.get("run_id")
    if not isinstance(suite_id, str) or Path(suite_id).name != suite_id or suite_id in (".", ".."):
        return None
    suite_dir = root / suite_id
    return suite_dir if suite_dir.is_dir() else None
