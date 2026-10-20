"""Read-only, separate-terminal dashboard for config-driven project builds."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from itertools import product
import os
from pathlib import Path
import shutil
import sys
import time
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from binforge.build_monitor_state import current_run, current_suite, process_alive, read_json
from binforge.build_monitor_discovery import active_builds, discover_base_path, latest_completed_record
from binforge.compiler_options import option_set_label
from binforge.monitor_input import QuitKey
from binforge import config


_MANIFEST_CACHE: dict[tuple[str, int], dict] = {}
_TERMINAL_ROWS_CACHE: dict[tuple[str, float], list[dict]] = {}


def _manifest(path: Path) -> dict | None:
    """Cache immutable run manifests so large command scopes stay cheap to refresh."""
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    cached = _MANIFEST_CACHE.get(key)
    if cached is not None:
        return cached
    value = read_json(path)
    if value is not None:
        if len(_MANIFEST_CACHE) >= 64:
            _MANIFEST_CACHE.pop(next(iter(_MANIFEST_CACHE)))
        _MANIFEST_CACHE[key] = value
    return value


def snapshot(base_path: Path) -> dict | None:
    suite_dir = current_suite(base_path)
    if suite_dir is not None:
        return snapshot_record(suite_dir / "suite.json")
    run_dir = current_run(base_path)
    if run_dir is None:
        return legacy_dependency_snapshot(base_path)
    return snapshot_record(run_dir / "run.json")


def legacy_dependency_snapshot(base_path: Path) -> dict | None:
    """Read an in-flight dependency build started before run status was added."""
    if base_path.resolve() != Path(config.EXTRA_DEP_PATH).resolve():
        return None
    for pid, discovered in active_builds():
        if discovered != base_path.resolve():
            continue
        try:
            argv = [part.decode("utf-8", "surrogateescape") for part in
                    Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]
            if "--build-deps" not in argv:
                continue
            option = next((arg.split("=", 1)[1] for arg in argv
                           if arg.startswith("--config=")), None)
            if option is None:
                option = argv[argv.index("--config") + 1]
            config_path = Path(option).expanduser()
            if not config_path.is_absolute():
                config_path = Path(f"/proc/{pid}/cwd").resolve() / config_path
            config_path = config_path.resolve()
            cfg = yaml.safe_load(config_path.read_text())
            projects = cfg["projects"]
            needed, pending = set(), list(cfg["targets"])
            while pending:
                parent = pending.pop()
                for name in (projects[parent] or {}).get("deps", []):
                    if name not in needed:
                        needed.add(name)
                        pending.append(name)
            started = Path(f"/proc/{pid}").stat().st_ctime
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            continue
        rows = []
        for name, arch in product(sorted(needed), cfg["build"]["arch"]):
            comb = ("gcc-13.3.0", arch, "O2")
            prefix = base_path / "install" / arch / name
            marker = read_json(prefix / ".binforge-complete.json")
            row = {"project": name, "compiler": comb[0], "arch": arch,
                   "opti": comb[2], "state": "queued", "phase": "queued"}
            if marker and marker.get("project") == name and marker.get("arch") == arch:
                row.update(state="succeeded", phase="installed")
            else:
                log_dir = base_path / "log" / name
                files = list(log_dir.glob(f"{name}_gcc-13.3.0_{arch}_O2_*"))
                recent = [file for file in files if file.stat().st_mtime >= started - 2]
                if recent:
                    latest = max(recent, key=lambda file: file.stat().st_mtime)
                    phase = next((phase for phase in ("install", "make", "configure", "cmake")
                                  if f"_{phase}." in latest.name), "prepare")
                    row.update(state="running", phase=phase,
                               started_at=min(file.stat().st_mtime for file in recent))
            rows.append(row)
        manifest = {"kind": "dependencies", "run_id": f"live{pid:08d}",
                    "base_path": str(base_path), "config_path": str(config_path),
                    "options": {}, "variants": rows}
        run = {"state": "running", "phase": "build (legacy status)",
               "pid": pid, "started_at": started}
        return {"manifest": manifest, "run": run, "variants": rows}
    return None


def snapshot_record(record_path: Path) -> dict | None:
    if record_path.name == "suite.json":
        suite_dir = record_path.parent
        suite = read_json(record_path)
        if not suite:
            return None
        base_path = Path(suite["base_path"])
        children = {}
        for mode in suite["modes"]:
            if suite.get("project_mode"):
                combined = []
                for index, project_name in enumerate(suite["targets"]):
                    child_base = base_path / mode / project_name
                    config_name = f"{mode}.yaml" if index == 0 else f"{mode}-{index}.yaml"
                    expected = (suite_dir / config_name).resolve()
                    run_dir = current_run(child_base)
                    manifest = read_json(run_dir / "manifest.json") if run_dir else None
                    if not manifest or Path(manifest.get("config_path", "")).resolve() != expected:
                        run_dir = None
                        for candidate in (child_base / "monitor").glob("*/manifest.json"):
                            item = read_json(candidate)
                            if item and Path(item.get("config_path", "")).resolve() == expected:
                                run_dir = candidate.parent
                                break
                    record = snapshot_record(run_dir / "run.json") if run_dir else None
                    if record:
                        combined.extend({**row, "_base_path": record["manifest"]["base_path"]}
                                        for row in record["variants"])
                children[mode] = {"manifest": {"base_path": str(base_path)},
                                  "variants": combined}
                continue
            child_base = base_path / mode
            run_dir = current_run(child_base)
            expected = (suite_dir / f"{mode}.yaml").resolve()
            manifest = read_json(run_dir / "manifest.json") if run_dir else None
            if not manifest or Path(manifest.get("config_path", "")).resolve() != expected:
                run_dir = None
                for candidate in (child_base / "monitor").glob("*/manifest.json"):
                    item = read_json(candidate)
                    if item and Path(item.get("config_path", "")).resolve() == expected:
                        run_dir = candidate.parent
                        break
            children[mode] = snapshot_record(run_dir / "run.json") if run_dir else None
        return {"suite": suite, "suite_dir": suite_dir, "children": children}
    if record_path.name != "run.json":
        return None
    run_dir = record_path.parent
    manifest = _manifest(run_dir / "manifest.json")
    run = read_json(record_path)
    if not manifest or not run:
        return None
    rows = _run_rows(run_dir, manifest)
    result = {"run_dir": run_dir, "manifest": manifest, "run": run, "variants": rows}
    batch = runner_batch(manifest)
    if batch is not None:
        result["batch"] = batch
    else:
        scope = command_scope(run_dir, manifest, rows)
        if scope is not None:
            result["command_scope"] = scope
    return result


def _run_rows(run_dir: Path, manifest: dict) -> list[dict]:
    run = read_json(run_dir / "run.json") or {}
    terminal = run.get("state") in ("succeeded", "failed")
    cache_key = (str(run_dir), float(run.get("updated_at", 0)))
    if terminal and cache_key in _TERMINAL_ROWS_CACHE:
        return _TERMINAL_ROWS_CACHE[cache_key]
    rows = []
    for variant in manifest.get("variants", []):
        state = read_json(run_dir / "variants" / f"{variant['id']}.json") or {
            "state": "unknown", "phase": "unknown",
        }
        rows.append({**variant, **state})
    if terminal:
        if len(_TERMINAL_ROWS_CACHE) >= 16:
            _TERMINAL_ROWS_CACHE.pop(next(iter(_TERMINAL_ROWS_CACHE)))
        _TERMINAL_ROWS_CACHE[cache_key] = rows
    return rows


def command_scope(run_dir: Path, manifest: dict,
                  current_rows: list[dict]) -> dict | None:
    """Merge subset retries into the largest command manifest for one base path.

    A base path represents one logical output set.  If a command initially
    registers several projects and a later recovery command retries only a
    subset in the same base path, keep the original command's complete variant
    set visible and overlay the newest state for every retried variant.
    """
    if manifest.get("kind") != "projects":
        return None
    current_ids = {row.get("id") for row in manifest.get("variants", [])}
    if not current_ids:
        return None
    options = manifest.get("options") or {}
    candidates = []
    for path in run_dir.parent.glob("*/manifest.json"):
        candidate = _manifest(path)
        if (not candidate or candidate.get("kind") != "projects" or
                candidate.get("base_path") != manifest.get("base_path") or
                (candidate.get("options") or {}) != options):
            continue
        ids = {row.get("id") for row in candidate.get("variants", [])}
        if current_ids <= ids:
            candidates.append((len(ids), path.parent.name, path.parent, candidate, ids))
    if not candidates:
        return None
    _, _, scope_dir, scope_manifest, scope_ids = max(candidates,
                                                     key=lambda item: (item[0], item[1]))
    if len(scope_ids) == len(current_ids):
        return None

    merged = {row["id"]: row for row in _run_rows(scope_dir, scope_manifest)}
    contributing = 1
    for path in sorted(run_dir.parent.glob("*/manifest.json")):
        retry_dir = path.parent
        if retry_dir.name <= scope_dir.name:
            continue
        retry_manifest = _manifest(path)
        if (not retry_manifest or retry_manifest.get("kind") != "projects" or
                retry_manifest.get("base_path") != manifest.get("base_path") or
                (retry_manifest.get("options") or {}) != options):
            continue
        retry_ids = {row.get("id") for row in retry_manifest.get("variants", [])}
        if retry_ids and retry_ids <= scope_ids:
            retry_rows = (current_rows if retry_dir == run_dir else
                          _run_rows(retry_dir, retry_manifest))
            merged.update((row["id"], row) for row in retry_rows)
            contributing += 1
    rows = [merged[row["id"]] for row in scope_manifest["variants"]]
    return {"manifest": scope_manifest, "variants": rows,
            "run": read_json(scope_dir / "run.json") or {},
            "run_count": contributing}


def runner_batch(manifest: dict) -> dict | None:
    """Recognize a project run owned by the complete binkit configuration-variant runner."""
    base = Path(manifest.get("base_path", ""))
    if base.parent.name != "projects":
        return None
    root = base.parent.parent
    name = base.name
    if Path(manifest.get("config_path", "")) != root / "config" / f"{name}.yaml":
        return None
    batch = read_json(root / "status.json")
    if (not batch or not isinstance(batch.get("targets"), list) or
            not isinstance(batch.get("projects"), dict) or
            name not in batch["targets"] or
            batch.get("variants_per_project") != len(manifest.get("variants", [])) or
            any(row.get("project") != name for row in manifest["variants"])):
        return None
    return batch


def _status_after_exit(status: dict, record_path: Path | None) -> dict:
    """Re-read a final marker when a process exits after the snapshot was read."""
    if status.get("state") != "running" or process_alive(
            status.get("pid"), status.get("pid_start_ticks")):
        return status
    latest = read_json(record_path) if record_path else None
    if latest and latest.get("state") in ("succeeded", "failed"):
        return latest
    return {**status, "state": "process missing / outcome unknown"}


def select_snapshot(previous_base: Path | None,
                    explicit_base: Path | None = None) -> tuple[dict | None, Path | None, str | None]:
    """Keep following the selected build after its process exits."""
    base_path, message = (explicit_base, None) if explicit_base else discover_base_path()
    if base_path is not None:
        return snapshot(base_path), base_path, None
    if message and message.startswith("Waiting for"):
        if previous_base is not None:
            data = snapshot(previous_base)
            if data is not None:
                return data, previous_base, None
        record = latest_completed_record()
        if record is not None:
            data = snapshot_record(record)
            if data is not None:
                return data, previous_base, None
    return None, previous_base, message or "No running build found."


def duration(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, rest = divmod(value, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def timestamp(value, fallback: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            pass
    return fallback


def batch_counts(batch: dict) -> Counter:
    return Counter((batch.get("projects", {}).get(name) or {}).get("state", "queued")
                   for name in batch.get("targets", []))


def batch_variant_progress(batch: dict, rows: list[dict]) -> tuple[int, int, int, int]:
    """Combine completed projects with the active project's variant progress."""
    per_project = int(batch.get("variants_per_project") or 0)
    targets = batch.get("targets", [])
    total = per_project * len(targets)
    project_counts = batch_counts(batch)
    done = (project_counts["succeeded"] + project_counts["failed"]) * per_project
    running = 0
    if rows:
        current = rows[0].get("project")
        current_state = (batch.get("projects", {}).get(current) or {}).get("state", "queued")
        if current_state not in ("succeeded", "failed"):
            done += sum(row.get("state") in ("succeeded", "failed") for row in rows)
            running = sum(row.get("state") == "running" for row in rows)
    done = min(done, total)
    pending = max(0, total - done - running)
    return done, total, running, pending


def failed_project_attention(names: list[str]) -> str:
    visible = names[:2]
    remainder = len(names) - len(visible)
    detail = ", ".join(visible)
    if remainder:
        detail += f"  +{remainder} more"
    return f"{len(names)} failed projects\n{detail}"


def memory_free() -> str:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return f"{int(line.split()[1]) / (1024 * 1024):.1f} GiB"
    except OSError:
        pass
    return "?"


def option_mode(options: dict) -> str:
    values = [str(options.get(key, "false")).strip().lower() in ("true", "yes", "on", "1")
              for key in ("pie", "no_inline", "stack_protector")]
    return "/".join("T" if value else "F" for value in values)


def suite_option_mode(mode: str) -> str:
    return option_set_label(mode)


def last_log(row: dict, base_path: Path) -> tuple[str, float | None]:
    phase = row.get("phase", "")
    suffix = {"configure": "configure.log", "cmake": "cmake.log",
              "make": "make.log", "install": "install.log"}.get(phase)
    if suffix is None:
        return "", None
    prefix = f"{row['project']}_{row['compiler']}_{row['arch']}_{row['opti']}"
    path = base_path / "log" / row["project"] / f"{prefix}_{suffix}"
    try:
        modified = path.stat().st_mtime
        if modified < row.get("started_at", 0) - 2:
            return str(path), None
        age = time.time() - modified
    except OSError:
        return str(path), None
    return str(path), age


def failed_log(row: dict, base_path: Path) -> Path | None:
    prefix = f"{row['project']}_{row['compiler']}_{row['arch']}_{row['opti']}"
    directory = base_path / "log" / row["project"]
    for phase in ("configure", "cmake", "make", "install"):
        code = directory / f"{prefix}_{phase}.returncode"
        try:
            if code.stat().st_mtime < row.get("started_at", 0) - 2:
                continue
            if code.read_text().strip() != "0":
                suffix = "make.error" if phase == "make" else (
                    "install.error" if phase == "install" else f"{phase}.log")
                return directory / f"{prefix}_{suffix}"
        except OSError:
            continue
    return None


def _progress(done: int, total: int, width: int) -> Text:
    filled = round(width * done / total) if total else 0
    result = Text()
    result.append("━" * filled, style="bold cyan")
    result.append("─" * (width - filled), style="dim")
    return result


def _configuration_progress(rows: list[dict], *, width: int, height: int,
                            dependencies: bool = False, batch: dict | None = None,
                            show_header: bool = False) -> Text:
    """Show configuration-variant progress for each varied component."""
    fields = (("Project", "project"), ("Compiler", "compiler"),
              ("Target arch.", "arch"), ("Opt. level", "opti"),
              ("Flag set", "mode"))
    if dependencies:
        fields = (fields[0], fields[2])
    if height < 16:
        fields = (fields[0], fields[-1])
    result = Text()
    if not rows:
        result.append("Configuration-variant status is not available yet.", style="dim")
        return result
    aligned = show_header
    label_width = 12
    groups_width = 11
    variants_width = 13
    bar_width = 10 if width >= 80 else (8 if width >= 50 else 6)
    current_width = max(8, min(24, width - label_width - groups_width
                               - variants_width - bar_width - 4))
    if aligned:
        result.append(f"{'Varied by':<{label_width}} {'Completed':<{groups_width}} "
                      f"{'Current':<{current_width}} {'Variants done':>{variants_width}} ",
                      style="bold cyan")
        result.append("Progress", style="bold cyan")
    for label, key in fields:
        if key == "project" and batch:
            values = batch["targets"]
            project_counts = batch_counts(batch)
            completed = project_counts["succeeded"] + project_counts["failed"]
            name = rows[0]["project"]
            finished = sum(row.get("state") in ("succeeded", "failed") for row in rows)
            if completed < len(values):
                description = name
                variant_text = f"{finished:,}/{len(rows):,}"
            elif project_counts["failed"]:
                description = (f"{project_counts['succeeded']} passed / "
                               f"{project_counts['failed']} failed")
                variant_text = "-"
            else:
                description = "all succeeded"
                variant_text = "-"
            bar_done, bar_total = finished, len(rows)
        else:
            values = list(dict.fromkeys(row.get(key, "?") for row in rows))
            groups = []
            for value in values:
                subset = [row for row in rows if row.get(key, "?") == value]
                finished = sum(row.get("state") in ("succeeded", "failed") for row in subset)
                running = sum(row.get("state") == "running" for row in subset)
                groups.append((value, finished, len(subset), running))
            completed = sum(done == total for _, done, total, _ in groups)
            remaining = [item for item in groups if item[1] < item[2]]
            if remaining:
                focus = max(remaining, key=lambda item: (item[3] > 0, item[1]))
                name = str(focus[0]).upper() if key == "mode" else str(focus[0])
                description = name
                variant_text = f"{focus[1]:,}/{focus[2]:,}"
                bar_done, bar_total = focus[1], focus[2]
            else:
                description = "all finished"
                variant_text = "-"
                bar_done = bar_total = 1
        if result:
            result.append("\n")
        groups_text = f"{completed}/{len(values)}"
        if aligned:
            result.append(f"{label:<{label_width}} {groups_text:<{groups_width}} ", style="dim")
            result.append(f"{description:<{current_width}.{current_width}} ", style="white")
            result.append(f"{variant_text:>{variants_width}} ", style="white")
        elif width >= 60:
            description_width = max(4, width - bar_width - 50)
            result.append(f"{label:<{label_width}} {groups_text:>5} complete | ", style="dim")
            result.append(f"{description:<{description_width}.{description_width}} ", style="white")
            result.append(f"{variant_text:>9} variants ", style="dim")
        else:
            compact_label = 8
            description_width = max(4, width - compact_label - bar_width - 16)
            result.append(f"{label:<{compact_label}.{compact_label}} {groups_text:>5} ", style="dim")
            result.append(f"{description:<{description_width}.{description_width}} ", style="white")
            result.append(f"{variant_text:>7} ", style="dim")
        result.append_text(_progress(bar_done, bar_total, bar_width))
    return result


def _active_capacity(*, width: int, height: int, dependencies: bool = False,
                     attention_lines: int = 1) -> int:
    """Reserve fixed overview rows before assigning the rest to active work."""
    configuration_rows = 2 if dependencies or height < 16 else 5
    if width >= 100:
        configuration_rows += 1
    if height < 20:
        fixed = 1 + 2 + 1 + configuration_rows + 1 + 1 + 1
    else:
        configuration_panel = configuration_rows + 2
        attention_panel = min(attention_lines, 2) + 2
        middle = (max(configuration_panel, attention_panel) if width >= 100
                  else configuration_panel + attention_panel)
        fixed = 1 + 4 + middle + 2 + 1
    return max(1, height - fixed)


def _active_widths(rows: list[dict], active: list[dict], *, full_terms: bool) -> dict[str, int]:
    """Keep variant columns steady as the active subset changes."""
    return {
        "project": max(7, max((len(str(row["project"])) for row in active), default=0)),
        "compiler": max(8, max((len(str(row["compiler"])) for row in rows), default=0)),
        "arch": max(12 if full_terms else 4,
                    max((len(str(row["arch"])) for row in rows), default=0)),
        "opti": max(10 if full_terms else 3,
                    max((len(str(row["opti"])) for row in rows), default=0)),
        "phase": max(5, max((len(str(row.get("phase", "?"))) for row in active), default=0)),
    }


def _active_identity(row: dict, widths: dict[str, int], *, padded: bool) -> str:
    fields = ("project", "compiler", "arch", "opti")
    if padded:
        return " ".join(f"{row[key]:<{widths[key]}}" for key in fields)
    return " ".join(str(row[key]) for key in fields)


def _active_header(widths: dict[str, int], *, padded: bool,
                   dependencies: bool = False, full_terms: bool = False) -> str:
    labels = {"project": "Project", "compiler": "Compiler",
              "arch": "Target arch." if full_terms else "Arch",
              "opti": "Opt. level" if full_terms else "Opt"}
    identity = (" ".join(f"{labels[key]:<{widths[key]}}" for key in labels)
                if padded else "Project Compiler Arch Opt")
    flag_label = "Flag set" if full_terms else "Flags"
    flag_width = 8 if full_terms else 5
    options = "" if dependencies else f" {flag_label:<{flag_width}}"
    return f"{identity}{options} | {'Phase':<{widths['phase']}} Elapsed"


def _overview(header: Text, summary: Text, rows: list[dict], active: Text,
              attention: Text, footer: Text, *, width: int, height: int,
              active_count: int, urgent: bool, dependencies: bool = False,
              batch: dict | None = None) -> Group:
    """Keep the overview visible at 20 rows and place Attention beside progress."""
    if height < 20:
        return Group(header, summary, Text("Configuration variants", style="bold cyan"),
                     _configuration_progress(rows, width=width, height=height,
                                             dependencies=dependencies, batch=batch),
                     Text(f"Active ({active_count})", style="bold green"), active,
                     Text("Attention: " + attention.plain.splitlines()[0],
                          style="red" if urgent else "dim"), footer)

    side_by_side = width >= 100
    configuration_width = (width - 8) * 2 // 3 if side_by_side else width - 4
    configuration_panel = Panel(_configuration_progress(
        rows, width=configuration_width, height=height,
        dependencies=dependencies, batch=batch, show_header=side_by_side),
        title="Configuration variants", border_style="cyan")
    attention_brief = Text("\n".join(attention.plain.splitlines()[:2]),
                           style="red" if urgent else "yellow",
                           no_wrap=True, overflow="ellipsis")
    attention_panel = Panel(attention_brief, title="Attention",
                            border_style="red" if urgent else "yellow")
    if side_by_side:
        middle = Table.grid(expand=True, padding=(0, 1))
        middle.add_column(ratio=2)
        middle.add_column(ratio=1)
        middle.add_row(configuration_panel, attention_panel)
    else:
        middle = configuration_panel
    blocks = [header, Panel(summary, title="Overall progress",
                            border_style="cyan"), middle,
              Panel(active, title=f"Active ({active_count})", border_style="green")]
    if not side_by_side:
        blocks.append(attention_panel)
    blocks.append(footer)
    return Group(*blocks)


def _suite_rows(data: dict) -> list[dict]:
    suite = data["suite"]
    axes = suite.get("axes") or {}
    targets = suite.get("targets") or []
    if not targets or not all(axes.get(key) for key in ("compiler", "arch", "opti")):
        return [{**row, "mode": suite_option_mode(mode)}
                for mode, child in data["children"].items()
                if child for row in child["variants"]]
    rows = []
    for mode in suite["modes"]:
        child = data["children"][mode]
        statuses = ({(row["project"], row["compiler"], row["arch"], row["opti"]): row
                     for row in child["variants"]} if child else {})
        for project_name, compiler, arch, opti in product(
                targets, axes["compiler"], axes["arch"], axes["opti"]):
            row = statuses.get((project_name, compiler, arch, opti))
            rows.append(row if row is not None else {
                "project": project_name, "compiler": compiler, "arch": arch,
                "opti": opti, "state": "queued", "phase": "queued",
            })
            rows[-1] = {**rows[-1], "mode": suite_option_mode(mode)}
    return rows


def render_suite(data: dict, *, width: int, height: int, details: bool):
    suite = data["suite"]
    children = data["children"]
    per_mode = suite["variants_per_mode"]
    total = per_mode * len(children)
    done = passed = failed = running = 0
    all_rows = _suite_rows(data)
    active_lines = Text()
    active_rows = []
    failure_lines = Text()
    detail_rows = []
    for mode, child in children.items():
        rows = child["variants"] if child else []
        counts = Counter(row.get("state", "unknown") for row in rows)
        finished = counts["succeeded"] + counts["failed"]
        done += finished
        passed += counts["succeeded"]
        failed += counts["failed"]
        running += counts["running"]
        for row in rows:
            if row.get("state") == "running":
                active_rows.append((mode, row))
            if row.get("state") == "failed":
                path = failed_log(row, Path(row.get("_base_path", child["manifest"]["base_path"])))
                failure_lines.append(f"{suite_option_mode(mode)} {row['project']} {row['compiler']} "
                                     f"{row['arch']} {row['opti']} "
                                     f"{str(path) if path else row.get('error', '')}\n",
                                     style="red")
            if details:
                detail_rows.append((mode, row))
    active_widths = _active_widths(all_rows, [row for _, row in active_rows],
                                   full_terms=width >= 100)
    if active_rows:
        active_lines.append(_active_header(active_widths, padded=width >= 70,
                                           full_terms=width >= 100), style="dim")
    for mode, row in active_rows:
        identity = _active_identity(row, active_widths, padded=width >= 70)
        row_elapsed = duration(time.time() - row.get("started_at", time.time()))
        phase = str(row.get("phase", "?"))
        if width >= 70:
            phase = f"{phase:<{active_widths['phase']}}"
        active_lines.append(f"\n{identity} {suite_option_mode(mode):<7} | "
                            f"{phase} {row_elapsed}", style="green")
    suite = _status_after_exit(suite, data["suite_dir"] / "suite.json")
    state = suite["state"]
    now = time.time()
    elapsed = duration((suite.get("ended_at", now) if state in ("succeeded", "failed") else now)
                       - suite.get("started_at", now))
    if width < 50 or height < 14:
        compact = Text(f"BinForge / BUILD  {elapsed}\n")
        compact.append(f"{state.upper()} {done}/{total} ✓{passed} ✗{failed} ●{running}\n")
        compact.append_text(_progress(done, total, max(8, width - 12)))
        compact.append("\n")
        compact.append_text(_configuration_progress(all_rows, width=width, height=height))
        return compact
    header = Text(f" BinForge / PROJECT BUILD  {elapsed}", style="bold cyan")
    summary = Text()
    summary.append(f"{state.upper()}  {done}/{total} complete ({done / total:.0%})  "
                   f"✓{passed}  ✗{failed}  ●{running}\n",
                   style="red" if failed or state.startswith("process missing") else "green")
    summary.append_text(_progress(done, total, max(10, min(width - 20, 45))))
    summary.append(f"  stage: {suite.get('phase', '?')}", style="dim")
    summary.no_wrap = True
    summary.overflow = "ellipsis"
    if not active_lines:
        active_lines.append("No active variants.", style="dim")
    elif not details:
        capacity = max(2, _active_capacity(width=width, height=height,
                                           attention_lines=2 if failure_lines else 1))
        lines = active_lines.plain.splitlines()
        variants = lines[1:]
        data_capacity = capacity - 1
        show_remainder = len(variants) > data_capacity and data_capacity >= 2
        shown = min(len(variants), data_capacity - 1 if show_remainder else data_capacity)
        active_lines = Text(lines[0][:max(20, width - 6)], style="dim")
        if shown:
            active_lines.append("\n" + "\n".join(
                line[:max(20, width - 6)] for line in variants[:shown]), style="green")
        if show_remainder:
            active_lines.append(f"\n+{len(variants) - shown} more active variants", style="dim")
    urgent = bool(failure_lines) or state.startswith("process missing") or state == "failed"
    if failure_lines:
        lines = failure_lines.plain.splitlines()
        attention = failure_lines if details else Text("\n".join(lines[:2 if height >= 20 else 1]), style="red")
    elif urgent:
        attention = Text(suite.get("error", state), style="red")
    else:
        attention = Text("No failed variants.", style="green")
    try:
        load = f"{os.getloadavg()[0]:.1f}"
        disk = f"{shutil.disk_usage(suite['base_path']).free / (1024 ** 3):.1f} GiB"
    except OSError:
        load, disk = "?", "?"
    footer = Text(f" Load {load} • RAM available {memory_free()} • disk free {disk} • q / Ctrl-C to exit",
                  style="dim", no_wrap=True, overflow="ellipsis")
    overview = _overview(header, summary, all_rows, active_lines, attention, footer,
                         width=width, height=height, active_count=running, urgent=urgent)
    blocks = [overview]
    if details:
        table = Table(title="All variants", expand=True)
        for column in ("Flag set", "Project", "Compiler", "Target arch.",
                       "Opt. level", "State", "Phase"):
            table.add_column(column)
        for mode, row in detail_rows:
            table.add_row(suite_option_mode(mode), *(str(row.get(key, "")) for key in
                                  ("project", "compiler", "arch", "opti", "state", "phase")))
        blocks.append(table)
    return Group(*blocks)


def render(data: dict | None, *, width: int = 80, height: int = 24,
           details: bool = False):
    if data is None:
        return Panel("No project build run found yet. Start compile.py in another terminal.",
                     title="BinForge / PROJECT BUILD", border_style="yellow")
    if "suite" in data:
        return render_suite(data, width=width, height=height, details=details)
    now = time.time()
    manifest, run, rows = data["manifest"], data["run"], data["variants"]
    batch = data.get("batch")
    command_scope_data = data.get("command_scope")
    overall_rows = command_scope_data["variants"] if command_scope_data else rows
    dependencies = manifest.get("kind") == "dependencies"
    counts = Counter(row.get("state", "unknown") for row in overall_rows)
    total = len(overall_rows)
    done = counts["succeeded"] + counts["failed"]
    running = counts["running"]
    pending = total - done - running
    run = _status_after_exit(run, data["run_dir"] / "run.json" if "run_dir" in data else None)
    state = run["state"]
    scope_run = command_scope_data.get("run", {}) if command_scope_data else {}
    elapsed_start = timestamp(scope_run.get("started_at", run.get("started_at")), now)
    elapsed_end = timestamp(run.get("ended_at"), now) if state in ("succeeded", "failed") else now
    if batch:
        elapsed_start = timestamp(batch.get("started_at"), elapsed_start)
        if batch.get("state") in ("succeeded", "failed"):
            elapsed_end = timestamp(batch.get("ended_at"), now)
    elapsed = duration(elapsed_end - elapsed_start)
    label = ", ".join(dict.fromkeys(row["project"] for row in rows))
    options = option_mode(manifest.get("options", {}))
    configuration_rows = [{**row, "mode": options} for row in overall_rows]
    title = "DEPENDENCY BUILD" if dependencies else "PROJECT BUILD"
    option_suffix = "" if dependencies else f" {options}"
    project_counts = batch_counts(batch) if batch else Counter()
    batch_done = project_counts["succeeded"] + project_counts["failed"]
    batch_running = project_counts["running"]
    batch_pending = len(batch["targets"]) - batch_done - batch_running if batch else 0
    batch_state = batch.get("state", state) if batch else state
    batch_variants = batch_variant_progress(batch, rows) if batch else None
    if width < 50 or height < 14:
        compact_view = Text("BinForge / BUILD\n", style="bold cyan")
        if batch:
            variant_done, variant_total, variant_running, variant_pending = batch_variants
            compact_view.append(f"{batch_state.upper()} variants {variant_done}/{variant_total} "
                                f"running {variant_running} queued {variant_pending}\n")
            compact_view.append_text(_progress(variant_done, variant_total,
                                               max(8, width - 12)))
            compact_view.append(f"\nprojects {batch_done}/{len(batch['targets'])} "
                                f"passed {project_counts['succeeded']} "
                                f"failed {project_counts['failed']} "
                                f"running {batch_running} queued {batch_pending}\n")
        else:
            scope_label = "command " if command_scope_data else ""
            compact_view.append(f"{state.upper()}  {scope_label}{done}/{total}  ✓{counts['succeeded']} "
                                f"✗{counts['failed']} ●{running} ·{pending}\n")
            compact_view.append_text(_progress(done, total, max(8, width - 12)))
        compact_view.append(f"\n{label[:max(8, width - 15)]}  {elapsed}{option_suffix}\n")
        active_row = next((row for row in rows if row.get("state") == "running"), None)
        if active_row:
            compact_view.append(f"active {active_row['project']} {active_row['compiler']} "
                                f"{active_row['arch']} {active_row['opti']}{option_suffix} | "
                                f"{active_row.get('phase', '?')} "
                                f"{duration(now - active_row.get('started_at', now))}\n",
                                style="green")
        failed_row = next((row for row in rows if row.get("state") == "failed"), None)
        if failed_row:
            compact_view.append(f"failed {failed_row['compiler']} {failed_row['arch']} "
                                f"{failed_row['opti']}", style="red")
        return compact_view
    if width < 70:
        header = Text(f" BinForge / {title}", style="bold cyan")
        header.append(f"   {label[:15]}   {elapsed}", style="white")
    else:
        header = Text(f" BinForge / {title}", style="bold cyan")
        header.append(f"   {label[:max(8, width - 60)]}   {elapsed} elapsed", style="white")
        header.append(f"   #{manifest['run_id'][-8:]}", style="dim")

    summary = Text()
    if batch:
        style = "red" if project_counts["failed"] or batch_state == "failed" else (
            "green" if batch_state == "succeeded" else "yellow")
        variant_done, variant_total, variant_running, variant_pending = batch_variants
        bar_width = 24 if width >= 130 else 0
        summary.append(f"{batch_state.upper():<10}", style=style)
        complete_word = " complete" if width >= 100 else ""
        summary.append(f"Variants  {variant_done:,}/{variant_total:,}{complete_word} "
                       f"({variant_done / variant_total:.1%})  ")
        if bar_width:
            summary.append_text(_progress(variant_done, variant_total,
                                          bar_width))
        summary.append(f"  running {variant_running:,}  queued {variant_pending:,}\n",
                       style="dim")
        project_total = len(batch["targets"])
        summary.append(" " * 10)
        summary.append(f"Projects  {batch_done:,}/{project_total:,}{complete_word} "
                       f"({batch_done / project_total:.1%})  ")
        if bar_width:
            summary.append_text(_progress(batch_done, project_total, bar_width))
        summary.append(f"  passed {project_counts['succeeded']}  ", style="green")
        summary.append(f"failed {project_counts['failed']}  ",
                       style="red" if project_counts["failed"] else "dim")
        summary.append(f"running {batch_running}  queued {batch_pending}", style="dim")
    else:
        style = "red" if counts["failed"] or state.startswith("process missing") else (
            "green" if state == "succeeded" else "yellow")
        summary.append(f"{state.upper()}  ", style=style)
        if command_scope_data:
            summary.append(f"Command  {done:,}/{total:,} complete  "
                           f"({done / total:.1%})  ")
            summary.append(f"✓{counts['succeeded']:,}  ", style="green")
            summary.append(f"✗{counts['failed']:,}  ",
                           style="red" if counts["failed"] else "dim")
        else:
            summary.append(f"{done}/{total} complete  ({done / total:.0%})  ")
            summary.append(f"✓ {counts['succeeded']}  ", style="green")
            summary.append(f"✗ {counts['failed']}  ",
                           style="red" if counts["failed"] else "dim")
        if width < 70:
            summary.append(f"● {running}  · {pending}\n")
        elif command_scope_data:
            summary.append(f"●{running:,}  ·{pending:,}\n")
        else:
            summary.append(f"● {running} running  · {pending} queued\n")
        summary.append_text(_progress(done, total, max(10, min(width - 20, 45))))
        summary.append(f"  stage: {run.get('phase', '?')}", style="dim")
    summary.no_wrap = True
    summary.overflow = "ellipsis"

    active = [row for row in rows if row.get("state") == "running"]
    active.sort(key=lambda row: row.get("started_at", now))
    active_widths = _active_widths(rows, active, full_terms=width >= 100)
    capacity = max(2, _active_capacity(
        width=width, height=height, dependencies=dependencies,
        attention_lines=2 if (counts["failed"] or project_counts["failed"]) else 1))
    data_capacity = capacity - 1
    show_remainder = len(active) > data_capacity and data_capacity >= 2
    shown = min(len(active), data_capacity - 1 if show_remainder else data_capacity)
    active_lines = Text()
    if active:
        active_lines.append(_active_header(active_widths, padded=width >= 70,
                                           dependencies=dependencies,
                                           full_terms=width >= 100), style="dim")
    for index, row in enumerate(active[:shown]):
        identity = _active_identity(row, active_widths, padded=width >= 70)
        phase = str(row.get("phase", "?"))
        if width >= 70:
            phase = f"{phase:<{active_widths['phase']}}"
        flag_width = 8 if width >= 100 else 5
        options_column = ("" if dependencies else
                          f" {options:<{flag_width}}" if width >= 70 else f" {options}")
        text = (f"{identity}{options_column} | {phase} "
                f"{duration(now - row.get('started_at', now))}")
        active_lines.append("\n" + text[:max(20, width - 5)], style="green")
    if not active:
        active_lines.append("No active variants.", style="dim")
    elif show_remainder:
        active_lines.append(f"\n+{len(active) - shown} more active variants", style="dim")

    failed = [row for row in overall_rows if row.get("state") == "failed"]
    failed.sort(key=lambda row: row.get("ended_at", 0), reverse=True)
    failed_projects = ([name for name in batch.get("targets", [])
                        if (batch.get("projects", {}).get(name) or {}).get("state") == "failed"]
                       if batch else [])
    attention = Text()
    if failed_projects:
        attention.append(failed_project_attention(failed_projects), style="bold red")
    elif failed:
        row = failed[0]
        attention.append(f"{len(failed)} failed: {row['project']} / {row['compiler']} / "
                         f"{row['arch']} / {row['opti']}{' / ' + options if not dependencies else ''} • "
                         f"{row.get('error') or row.get('phase')}",
                         style="bold red")
        if height >= 20:
            path = failed_log(row, Path(manifest["base_path"]))
            if path is not None:
                attention.append("\n" + str(path.relative_to(manifest["base_path"]))[:max(20, width - 6)],
                                 style="dim")
    elif state.startswith("process missing"):
        attention.append("Build process is missing; inspect the build terminal and logs.", style="bold red")
    elif state == "failed":
        attention.append(run.get("error", "Build failed before a variant finished.")[:max(20, width - 5)],
                         style="bold red")
    else:
        stale = next((row for row in active if (last_log(row, Path(manifest["base_path"]))[1] or 0) > 60), None)
        if stale:
            attention.append(f"No log update for over 60s: {stale['compiler']} / {stale['arch']} / {stale['opti']}",
                             style="yellow")
        else:
            attention.append("No failed variants.", style="green")

    try:
        load = f"{os.getloadavg()[0]:.1f}"
    except OSError:
        load = "?"
    try:
        disk = f"{shutil.disk_usage(manifest['base_path']).free / (1024 ** 3):.1f} GiB"
    except OSError:
        disk = "?"
    if width < 70:
        footer = Text(f" load {load} • RAM {memory_free()} • disk {disk}",
                      style="dim", no_wrap=True, overflow="ellipsis")
    else:
        footer = Text(f" Load {load} • RAM available {memory_free()} • disk free {disk} • q / Ctrl-C to exit",
                      style="dim", no_wrap=True, overflow="ellipsis")
    urgent = (bool(failed_projects) or bool(failed) or state.startswith("process missing")
              or state == "failed" or batch_state == "failed")
    overview = _overview(header, summary, configuration_rows, active_lines, attention, footer,
                         width=width, height=height, active_count=len(active),
                         urgent=urgent, dependencies=dependencies, batch=batch)
    blocks = [overview]
    if details:
        table = Table(title="All variants", expand=True)
        for column in (("Project", "Compiler", "Target arch.", "Opt. level", "State", "Phase")
                       if dependencies else
                       ("Project", "Compiler", "Target arch.", "Opt. level",
                        "Flag set", "State", "Phase")):
            table.add_column(column)
        for row in overall_rows:
            values = [str(row.get(key, "")) for key in
                      ("project", "compiler", "arch", "opti")]
            if not dependencies:
                values.append(options)
            table.add_row(*values, str(row.get("state", "")),
                          str(row.get("phase", "")))
        blocks.append(table)
    return Group(*blocks)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
        epilog="Recommended terminal: at least 100 columns by 20 rows. The monitor does not resize the terminal.")
    parser.add_argument("--base-path", type=Path,
                        help="Build output base path; omitted: find the one running compile.py")
    parser.add_argument("--once", action="store_true", help="Print a snapshot and exit")
    parser.add_argument("--details", action="store_true", help="Show every variant with --once")
    parser.add_argument("--interval", type=float, default=1, help="Refresh interval in seconds (default: 1)")
    args = parser.parse_args()
    if args.interval < 0.5:
        parser.error("--interval must be at least 0.5 seconds")
    if args.details and not args.once:
        parser.error("--details requires --once")
    console = Console()
    previous_base = None

    def display():
        nonlocal previous_base
        data, previous_base, message = select_snapshot(previous_base, args.base_path)
        if data is None and message is not None:
            return Panel(message,
                         title="BinForge / PROJECT BUILD", border_style="yellow")
        return render(data, width=console.width,
                      height=console.height, details=args.details)

    if args.once:
        console.print(display())
        return
    try:
        with QuitKey() as keys, Live(console=console, screen=True, refresh_per_second=2,
                                    vertical_overflow="crop") as live:
            while True:
                live.update(display())
                if keys.wait(args.interval):
                    break
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
