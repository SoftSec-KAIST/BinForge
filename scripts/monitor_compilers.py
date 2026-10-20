"""Read-only compiler dashboard; the full boxed view fits 100 columns by 20 rows."""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from binforge import config
from binforge.monitor_input import QuitKey
from binforge.toolchain_status import clang_status, gcc_status
from binforge.utils import load_yaml
from scripts.setup_gcc import ARCH_GCC_PREFIX_MAP


class CtngStageTracker:
    """Incrementally read crosstool-NG milestones from a growing build.log."""

    def __init__(self):
        self.files = {}

    def progress(self, path):
        try:
            file_stat = path.stat()
            state = self.files.get(path)
            if state is None or state["inode"] != file_stat.st_ino or state["offset"] > file_stat.st_size:
                state = {"inode": file_stat.st_ino, "offset": 0, "current": None,
                         "substep": None, "completed": 0}
                self.files[path] = state
            with path.open("rb") as stream:
                stream.seek(state["offset"])
                while True:
                    position = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        stream.seek(position)
                        break
                    if line.startswith(b"[INFO ]  "):
                        message = line[9:].decode("utf-8", "replace").strip()
                        if ": done in " in message:
                            state["completed"] += 1
                            if message.split(": done in ", 1)[0] == state["current"]:
                                state["current"] = None
                                state["substep"] = None
                        elif message.startswith(("Installing ", "Retrieving ", "Extracting ", "Finalizing ")):
                            state["current"] = message
                            state["substep"] = None
                    elif line.startswith(b"[EXTRA]    "):
                        message = line[11:].decode("utf-8", "replace").strip()
                        if message.startswith(("Configuring ", "Building ", "Installing ", "Checking ")):
                            state["substep"] = message
                state["offset"] = stream.tell()
        except OSError:
            return None
        return state["current"], state["substep"], state["completed"]


CTNG_STAGES = CtngStageTracker()

# Order from crosstool-ng/ct-ng.in CT_STEPS. These include no-op steps for
# some configurations, so the number is an ordinal, not elapsed-work progress.
CTNG_PHASES = (
    "companion_tools_for_build", "companion_libs_for_build", "binutils_for_build",
    "companion_tools_for_host", "companion_libs_for_host", "binutils_for_host",
    "linker", "libc_headers", "kernel_headers", "cc_core", "libc_main",
    "cc_for_build", "cc_for_host", "libc_post_cc", "companion_libs_for_target",
    "binutils_for_target", "debug", "test_suite", "finish",
)


def ctng_phase_label(current):
    """Map a logged component to its containing crosstool-NG build step."""
    if not current:
        return "phase ?/19"
    if current.startswith(("Retrieving ", "Extracting ")):
        return "prep"
    component = current.removeprefix("Installing ")
    exact = {
        "C library headers": "libc_headers",
        "kernel headers": "kernel_headers",
        "core C gcc compiler": "cc_core",
        "C library": "libc_main",
        "final gcc compiler for build": "cc_for_build",
        "final gcc compiler": "cc_for_host",
        "GCC test suite": "test_suite",
    }
    phase = exact.get(component)
    if phase is None:
        if current.startswith("Finalizing the toolchain"):
            phase = "finish"
        elif component == "mold for host":
            phase = "linker"
        elif component.startswith(("cross-gdb", "native gdb", "gdb server", "strace", "ltrace", "D.U.M.A.")):
            phase = "debug"
        elif component.startswith("binutils for "):
            phase = "binutils_for_" + component.rsplit(" ", 1)[1]
        elif component.split(" ", 1)[0] in ("make", "m4", "autoconf", "automake", "libtool", "dtc", "bison") and component.endswith((" for build", " for host")):
            phase = "companion_tools_for_" + component.rsplit(" ", 1)[1]
        elif component.endswith(" for build"):
            phase = "companion_libs_for_build"
        elif component.endswith(" for host"):
            phase = "companion_libs_for_host"
        elif component.endswith((" for target", " for the target")):
            phase = "companion_libs_for_target"
    if phase not in CTNG_PHASES:
        return f"phase ?/{len(CTNG_PHASES)}"
    return f"phase {CTNG_PHASES.index(phase) + 1}/{len(CTNG_PHASES)}"


def ctng_work_detail(current, substep, completed):
    """Keep the current component and action readable in a 100-column pane."""
    if current.startswith("Retrieving needed toolchain components"):
        detail = "download components"
    elif current.startswith("Extracting and patching toolchain components"):
        detail = "extract/patch components"
    elif current.startswith("Finalizing the toolchain"):
        detail = "finalize toolchain"
    else:
        detail = current.removeprefix("Installing ")
    if substep and substep != current:
        for prefix, verb in (("Building ", "build"), ("Configuring ", "configure"),
                             ("Installing ", "install"), ("Checking ", "check")):
            if substep.startswith(prefix):
                subject = substep[len(prefix):]
                action = verb if subject in detail else f"{verb} {subject}"
                break
        else:
            action = substep
        detail += f" / {action}"
    return f"{detail} · {completed} done"


def build_processes():
    result = subprocess.run(
        ["ps", "-ww", "-eo", "pid=,etimes=,args="], capture_output=True, text=True, check=False
    )
    if result.returncode:
        return []
    rows = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        pid, elapsed, command = parts
        if int(pid) == os.getpid():
            continue
        if "scripts/setup_gcc.py" in command or "scripts/setup_clang.py" in command:
            if "--status" not in command:
                rows.append((pid, int(elapsed), command))
    return rows


def active_gcc_targets():
    """Identify running ct-ng targets from their own working directories."""
    result = subprocess.run(
        ["ps", "-ww", "-eo", "pid=,etimes=,args="], capture_output=True, text=True, check=False
    )
    if result.returncode:
        return []
    roots = (Path(config.GCC_DIR_PATH) / "build", Path(config.BOOTSTRAP_PATH) / "build")
    targets = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        pid, elapsed, command = parts
        if "/crosstool-ng/ct-ng" not in command or "build." not in command:
            continue
        try:
            work_dir = Path(f"/proc/{pid}/cwd").resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        for root in roots:
            try:
                version, arch = work_dir.relative_to(root).parts
            except ValueError:
                continue
            targets[work_dir] = (version, arch, work_dir, int(pid), int(elapsed))
    return sorted(targets.values(), key=lambda item: (item[0], item[1]))


def latest_logs(roots, count=5):
    paths = []
    for root in roots:
        if root.is_dir():
            paths.extend(root.glob("*/*/build.log"))
            paths.extend(root.glob("clang-*.log"))
            paths.extend(root.glob("clang-*.error"))
    dated = []
    for path in paths:
        try:
            dated.append((path.stat().st_mtime, path))
        except OSError:
            # A build may finish and clean up a log during this scan.
            continue
    return [path for _, path in sorted(dated, reverse=True)[:count]]


def last_line(path):
    # Read just the tail of a potentially huge build log.
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 8192))
            lines = stream.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return "(log unavailable)"
    return next((line.strip() for line in reversed(lines) if line.strip()), "(empty)")


def human_duration(seconds):
    hours, rest = divmod(int(seconds), 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def human_size(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024


def memory_available():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return human_size(int(line.split()[1]) * 1024)
    except OSError:
        pass
    return "unknown"


def bar(completed, total, width=8):
    filled = round(width * completed / total) if total else 0
    return "[cyan]" + "━" * filled + "[/][dim]" + "─" * (width - filled) + "[/]"


def clang_label(version, ready, width):
    marker = "[green]●[/]" if ready else "[dim]○[/]"
    return f"{version:<{width}}  {marker}"


def snapshot(gcc_config, clang_config):
    bootstrap = gcc_config["bootstrap"]
    bv, ba = str(bootstrap["version"]), bootstrap["arch"]
    _, _, problems = gcc_status(bv, ba, ARCH_GCC_PREFIX_MAP[ba], config.BOOTSTRAP_PATH)
    link = Path(config.BOOTSTRAP_PATH) / "bootstrap" / "bin" / f"{ARCH_GCC_PREFIX_MAP[ba]}-gcc"
    bootstrap_ready = not problems and link.is_file()

    gcc_rows = []
    for version, archs in gcc_config["toolchain_targets"].items():
        version = str(version)
        ready = sum(not gcc_status(version, arch, ARCH_GCC_PREFIX_MAP[arch], config.GCC_DIR_PATH)[2]
                    for arch in archs)
        gcc_rows.append((version, ready, len(archs)))

    clang_rows = []
    for version in clang_config["toolchain_targets"]:
        version = str(version)
        clang_rows.append((version, not clang_status(version, config.CLANG_DIR_PATH)[1]))

    logs = latest_logs([
        Path(config.BOOTSTRAP_PATH) / "build",
        Path(config.GCC_DIR_PATH) / "build",
        Path(config.CLANG_DIR_PATH) / "logs",
    ])
    return bootstrap_ready, gcc_rows, clang_rows, build_processes(), logs, active_gcc_targets()


def active_target_lines(data):
    """One live row per ct-ng work directory, plus the Clang runner."""
    _, _, _, processes, _, active_gcc = data
    lines = []
    now = time.time()
    for version, arch, work_dir, _, _ in active_gcc:
        log = work_dir / "build.log"
        stage = CTNG_STAGES.progress(log)
        phase = ctng_phase_label(stage[0]) if stage else "prep"
        if stage and stage[0]:
            current, substep, completed = stage
            detail = ctng_work_detail(current, substep, completed)
        else:
            detail = "starting; log pending" if not log.exists() else "stage not logged yet"
        try:
            age = human_duration(max(0, now - log.stat().st_mtime))
        except OSError:
            age = "--:--:--"
        lines.append(f"{'GCC ' + version + '/' + arch:<20} {f'[{age}]':<14}  {phase:<11}  {detail}")

    clang_runners = [(elapsed, command) for _, elapsed, command in processes
                     if "setup_clang.py" in command]
    if clang_runners:
        clang_logs = latest_logs([Path(config.CLANG_DIR_PATH) / "logs"], count=1)
        if clang_logs:
            log = clang_logs[0]
            match = re.match(r"clang-(.+)_(cmake|make|install)\.(?:log|error)$", log.name)
            label = f"Clang {match.group(1)}" if match else "Clang"
            detail = match.group(2) if match else "build log"
            phase = {"cmake": "phase 2/4", "make": "phase 3/4", "install": "phase 4/4"}.get(detail, "phase ?/4")
            try:
                age = human_duration(max(0, now - log.stat().st_mtime))
            except OSError:
                age = "--:--:--"
            lines.append(f"{label:<20} {f'[{age}]':<14}  {phase:<11}  {detail}")
        else:
            runtime = f"[run {human_duration(clang_runners[0][0])}]"
            lines.append(f"{'Clang setup':<20} {runtime:<14}  {'phase 1/4':<11}  LLVM source preparation")
    return lines


def active_target_panel(data, max_rows=None):
    lines = active_target_lines(data)
    if not lines:
        return None, 0
    if max_rows is None or len(lines) <= max_rows:
        visible = lines
    else:
        visible = lines[:max(0, max_rows - 1)]
    content_lines = []
    for line in visible:
        row = Text(line, style="green", no_wrap=True, overflow="ellipsis")
        for match in re.finditer(r"phase (?:\d+|\?)/\d+", line):
            row.stylize("bold cyan", match.start(), match.end())
        content_lines.append(row)
    if len(visible) < len(lines):
        content_lines.append(Text(f"+{len(lines) - len(visible)} more active targets; enlarge terminal"))
    return Panel(Group(*content_lines), title="Active compiler targets", border_style="green"), len(visible) + 2 + int(len(visible) < len(lines))


def install_matrix(gcc_rows, clang_rows):
    """Seven compact version rows for the configured GCC and Clang releases."""
    matrix = Table.grid(expand=True, padding=(0, 2))
    matrix.add_column(ratio=1)
    matrix.add_column(ratio=1)
    gcc_lines = [f"{version:<7} {ready:>2}/{target:<2} {bar(ready, target, 7)}"
                 for version, ready, target in gcc_rows]
    version_width = max((len(version) for version, _ in clang_rows), default=0)
    clang_lines = ["  ".join(clang_label(version, ready, version_width)
                            for version, ready in clang_rows[index:index + 2])
                   for index in range(0, len(clang_rows), 2)]
    for index in range(max(len(gcc_lines), len(clang_lines))):
        matrix.add_row(gcc_lines[index] if index < len(gcc_lines) else "",
                       clang_lines[index] if index < len(clang_lines) else "")
    return matrix, max(len(gcc_lines), len(clang_lines))


def quiet_active_gcc_logs(active_gcc):
    quiet = []
    now = time.time()
    for version, arch, work_dir, _, _ in active_gcc:
        try:
            if now - (work_dir / "build.log").stat().st_mtime > 600:
                quiet.append(f"{version}/{arch}")
        except OSError:
            pass
    return quiet


def render_compact(data, height):
    """Keep the live overview within a short terminal pane."""
    bootstrap_ready, gcc_rows, clang_rows, processes, logs, active_gcc = data
    width = shutil.get_terminal_size((100, 32)).columns
    gcc_done = sum(ready for _, ready, _ in gcc_rows)
    gcc_total = sum(total for _, _, total in gcc_rows)
    clang_done = sum(ready for _, ready in clang_rows)
    disk = human_size(shutil.disk_usage(config.PROJECT_PATH).free)
    state = "[green]READY[/]" if bootstrap_ready else "[yellow]PENDING[/]"
    status = (
        f"Bootstrap {state}  •  GCC [cyan]{gcc_done}/{gcc_total}[/]  •  "
        f"Clang [magenta]{clang_done}/{len(clang_rows)}[/]\n"
        f"Runners {len(processes)}  •  load {os.getloadavg()[0]:.1f}/{os.cpu_count()}  •  "
        f"RAM {memory_available()}  •  disk {disk}"
    )
    if height < 14:
        brief = Text.from_markup(
            f"Bootstrap {state}  •  GCC {gcc_done}/{gcc_total}  •  "
            f"Clang {clang_done}/{len(clang_rows)}\n"
            f"Runners {len(processes)}  •  newest log: {logs[0].name if logs else 'none'}"
        )
        brief.no_wrap = True
        brief.overflow = "ellipsis"
        return Panel(brief, title="BinForge / Compiler Build", border_style="cyan")

    if active_gcc or any("setup_clang.py" in command for _, _, command in processes):
        overall = gcc_done + clang_done + int(bootstrap_ready)
        total = gcc_total + len(clang_rows) + 1
        header = Text("BinForge / COMPILER BUILD", style="bold cyan")
        header.append("  " + time.strftime("%H:%M:%S"), style="dim")
        matrix, matrix_rows = install_matrix(gcc_rows, clang_rows)
        if height >= 20 and width >= 80 and matrix_rows <= 7:
            summary = Text.from_markup(
                f"[green]RUNNING[/]  •  {overall}/{total} verified  •  "
                f"GCC [cyan]{gcc_done}/{gcc_total}[/]  •  Clang [magenta]{clang_done}/{len(clang_rows)}[/]  •  "
                f"Bootstrap {state}  •  {len(processes)} runners"
            )
            summary.no_wrap = True
            summary.overflow = "ellipsis"
            active_panel, _ = active_target_panel(data, max_rows=max(5, height - 15))
            quiet = quiet_active_gcc_logs(active_gcc)
            attention_lines = [
                Text("Log quiet >10m: " + ", ".join(quiet) if quiet else "No stale active GCC logs.",
                     style="yellow" if quiet else "green", no_wrap=True, overflow="ellipsis"),
                Text("[age] = log update", style="dim"),
                Text(f"load {os.getloadavg()[0]:.1f}/{os.cpu_count()}", style="dim"),
                Text(f"RAM {memory_available()}", style="dim"),
                Text(f"disk {disk}", style="dim"),
                Text("--once: full logs", style="dim"),
                Text("q / Ctrl-C to exit", style="dim"),
            ]
            middle = Table.grid(expand=True, padding=(0, 1))
            middle.add_column(ratio=2)
            middle.add_column(ratio=1)
            middle.add_row(Panel(matrix, title="Install matrix / GCC · Clang", border_style="cyan"),
                           Panel(Group(*attention_lines), title="Attention / host", border_style="yellow"))
            return Group(header, Panel(summary, title="Total", border_style="cyan"), middle, active_panel)
        if 18 <= height < 20 and width >= 70 and matrix_rows <= height - 7:
            summary = Text.from_markup(
                f"[green]RUN[/] {overall}/{total} verified  •  "
                f"GCC [cyan]{gcc_done}/{gcc_total}[/]  •  Clang [magenta]{clang_done}/{len(clang_rows)}[/]  •  "
                f"bootstrap {state}  •  {len(processes)} runners"
            )
            summary.no_wrap = True
            summary.overflow = "ellipsis"
            target_lines = active_target_lines(data)
            slots = height - 6 - matrix_rows
            visible = target_lines if len(target_lines) <= slots else target_lines[:slots - 1]
            active_rows = []
            for line in visible:
                row = Text(line, style="green", no_wrap=True, overflow="ellipsis")
                for match in re.finditer(r"phase (?:\d+|\?)/\d+", line):
                    row.stylize("bold cyan", match.start(), match.end())
                active_rows.append(row)
            if len(visible) < len(target_lines):
                active_rows.append(Text(f"+{len(target_lines) - len(visible)} more active targets", style="yellow"))
            quiet = quiet_active_gcc_logs(active_gcc)
            attention = Text("Attention: " + ("log quiet >10m: " + ", ".join(quiet) if quiet else "no stale GCC logs"),
                             style="yellow" if quiet else "dim", no_wrap=True, overflow="ellipsis")
            host = Text(f"load {os.getloadavg()[0]:.1f}/{os.cpu_count()}  •  RAM {memory_available()}  •  disk {disk}  •  q / Ctrl-C",
                        style="dim", no_wrap=True, overflow="ellipsis")
            parts = [header, summary, Text("Install matrix / GCC · Clang", style="bold cyan"), matrix]
            parts.append(Text("Active compiler targets", style="bold green"))
            parts.extend(active_rows)
            parts.extend((attention, host))
            return Group(*parts)
        total_text = Text.from_markup(
            f"[green]RUNNING[/]  •  {overall}/{total} verified  •  "
            f"Bootstrap {state}  •  {len(processes)} runners\n"
            f"GCC [cyan]{gcc_done}/{gcc_total}[/]  •  Clang [magenta]{clang_done}/{len(clang_rows)}[/]"
        )
        total_text.no_wrap = True
        total_text.overflow = "ellipsis"
        show_matrix = height >= 26 and width >= 70
        matrix_panel = None
        if show_matrix:
            matrix_panel = Panel(matrix, title="Install matrix  /  GCC progress · Clang releases", border_style="cyan")
        available = max(1, height - (20 if show_matrix else 11))
        active_panel, _ = active_target_panel(data, max_rows=available)
        quiet = quiet_active_gcc_logs(active_gcc)
        attention = ("Log quiet >10m: " + ", ".join(quiet)) if quiet else "No stale active GCC logs."
        attention_text = Text(attention, style="yellow" if quiet else "dim",
                              no_wrap=True, overflow="ellipsis")
        host = Text(f"load {os.getloadavg()[0]:.1f}/{os.cpu_count()}  •  RAM {memory_available()}  •  disk {disk}  •  age=log update  •  q / Ctrl-C",
                    style="dim", no_wrap=True, overflow="ellipsis")
        parts = [header, Panel(total_text, title="Total", border_style="cyan")]
        if matrix_panel:
            parts.append(matrix_panel)
        parts.extend((active_panel, Panel(attention_text, title="Attention", border_style="yellow"), host))
        return Group(*parts)

    activity = Text(no_wrap=True, overflow="ellipsis")
    if processes:
        runners = []
        for pid, elapsed, command in processes[:2]:
            kind = "GCC" if "setup_gcc.py" in command else "Clang"
            runners.append(f"{kind} PID {pid} {human_duration(elapsed)}")
        activity.append("RUNNING  " + "  •  ".join(runners), style="green")
    else:
        activity.append("No build runner", style="yellow")
    if logs:
        path = logs[0]
        try:
            age = human_duration(max(0, time.time() - path.stat().st_mtime))
            stage = CTNG_STAGES.progress(path) if path.name == "build.log" else None
            if stage and stage[0]:
                current, _, completed = stage
                activity.append(f"\n{current}  •  {completed} log milestones  •  log {age} ago")
            else:
                activity.append(f"\n{path.name}  •  {age} since update  •  {last_line(path)[:65]}")
        except OSError:
            activity.append("\nNewest log unavailable")
    else:
        activity.append("\nNo build log yet")

    # Two compiler columns, with Clang releases paired to fit seven rows.
    gcc_lines = [f"{version:<7} {ready:>2}/{total:<2} {bar(ready, total, 6)}"
                 for version, ready, total in gcc_rows]
    clang_lines = []
    step = 2 if width >= 78 else 1
    version_width = max((len(version) for version, _ in clang_rows), default=0)
    for index in range(0, len(clang_rows), step):
        pair = clang_rows[index:index + step]
        clang_lines.append("   ".join(
            clang_label(version, ready, version_width)
            for version, ready in pair
        ))
    # Status and activity take eight lines, compiler panel takes two border
    # lines, and the footer takes one. Never rely on Live's crop for sizing.
    visible_rows = max(0, height - 11)
    matrix = Table.grid(expand=True, padding=(0, 1))
    if width < 50:
        matrix.add_column()
        for line in gcc_lines[:visible_rows]:
            matrix.add_row(line)
    else:
        matrix.add_column(ratio=1)
        matrix.add_column(ratio=1)
        for index in range(min(max(len(gcc_lines), len(clang_lines)), visible_rows)):
            matrix.add_row(
                gcc_lines[index] if index < len(gcc_lines) else "",
                clang_lines[index] if index < len(clang_lines) else "",
            )
    footer = "  ● ready   ○ pending  •  --once for full scrollable snapshot  •  q / Ctrl-C"
    if width < 70:
        footer = "  --once: full list  •  q / Ctrl-C"
    elif visible_rows < len(clang_lines):
        footer = "  More releases: --once  •  q / Ctrl-C"
    status_text = Text.from_markup(status)
    status_text.no_wrap = True
    status_text.overflow = "ellipsis"
    return Group(
        Panel(status_text, title="BinForge / Compiler Build", border_style="cyan"),
        Panel(activity, title="Live activity", border_style="green"),
        Panel(matrix, title="GCC progress" if width < 50 else "GCC progress  /  Clang releases",
              border_style="magenta") if visible_rows else Text(""),
        Text(footer, style="dim"),
    )


def render(data, *, height=None):
    bootstrap_ready, gcc_rows, clang_rows, processes, logs, active_gcc = data
    terminal = shutil.get_terminal_size((100, 32))
    console_width = terminal.columns
    height = terminal.lines if height is None else height
    # Five active logs can make the full layout more than 40 rows tall.
    # Narrow terminals also wrap full-width tables, so keep them compact.
    if height < 60 or console_width < 70:
        return render_compact(data, height)
    completed_gcc = sum(row[1] for row in gcc_rows)
    total_gcc = sum(row[2] for row in gcc_rows)
    completed_clang = sum(ready for _, ready in clang_rows)
    total_clang = len(clang_rows)
    load = os.getloadavg()[0]

    header = Text("  BinForge  /  COMPILER BUILD MONITOR", style="bold cyan")
    header.append(f"     {time.strftime('%Y-%m-%d %H:%M:%S')}   •   read only", style="dim")
    summary = Table.grid(padding=(0, 3), expand=True)
    summary.add_column(ratio=1)
    summary.add_column(ratio=1)
    summary.add_column(ratio=1)
    summary.add_row(
        f"Bootstrap  [{'green' if bootstrap_ready else 'yellow'}]{'READY' if bootstrap_ready else 'PENDING'}[/]",
        f"GCC  [cyan]{completed_gcc}/{total_gcc}[/] installed",
        f"Clang  [magenta]{completed_clang}/{total_clang}[/] installed",
    )
    summary.add_row(
        f"Run  [{'green' if processes else 'dim'}]{len(processes)}[/]  •  load {load:.1f}/{os.cpu_count()}",
        f"Mem free  {memory_available()}",
        f"Disk free  {human_size(shutil.disk_usage(config.PROJECT_PATH).free)}",
    )
    overall = completed_gcc + completed_clang + int(bootstrap_ready)
    total = total_gcc + total_clang + 1
    summary.add_row(bar(overall, total), f"{overall}/{total} verified installs", "No estimated finish time")

    gcc_table = Table(box=box.SIMPLE, expand=True, show_header=True, header_style="bold cyan")
    gcc_table.add_column("Version")
    gcc_table.add_column("Done", justify="right")
    gcc_table.add_column("Progress")
    for version, ready, target in gcc_rows:
        gcc_table.add_row(version, f"{ready}/{target}", bar(ready, target))

    clang_table = Table(box=box.SIMPLE, expand=True, show_header=False)
    clang_table.add_column("Release")
    clang_table.add_column("Release")
    version_width = max((len(version) for version, _ in clang_rows), default=0)
    for index in range(0, len(clang_rows), 2):
        pair = clang_rows[index:index + 2]
        cells = [clang_label(version, ready, version_width) for version, ready in pair]
        clang_table.add_row(*cells, *([""] if len(cells) == 1 else []))

    activity = Table(box=box.SIMPLE, expand=True, show_header=True)
    activity.add_column("Activity", ratio=1)
    activity.add_column("Age / runtime", justify="right")
    for pid, elapsed, command in processes[:3]:
        activity.add_row(Text(f"RUNNING  PID {pid}  {command[-min(75, console_width - 30):]}", style="green"), human_duration(elapsed))
    now = time.time()
    for path in logs:
        try:
            age = max(0, now - path.stat().st_mtime)
        except OSError:
            continue
        style = "green" if age < 30 and processes else "dim"
        relative = path.relative_to(config.PROJECT_PATH)
        message = Text(str(relative), style=style)
        stage = CTNG_STAGES.progress(path) if path.name == "build.log" else None
        if stage and stage[0]:
            current, substep, completed = stage
            message.append(f"\n{current}  •  {completed} log milestones")
            if substep:
                message.append(f"\n{substep}")
        else:
            message.append("\n" + last_line(path)[:max(30, console_width - 22)])
        activity.add_row(message,
                         human_duration(age) + " ago")
    if not processes and not logs:
        activity.add_row("No build process or log yet. Start the bootstrap in another terminal.", "")

    compilers = Table.grid(expand=True, padding=(0, 1))
    compilers.add_column(ratio=1)
    compilers.add_column(ratio=1)
    compilers.add_row(
        Panel(gcc_table, title="GCC toolchains", border_style="cyan"),
        Panel(clang_table, title="Clang toolchains", border_style="magenta"),
    )

    active_panel, _ = active_target_panel(data)
    return Group(
        header,
        Panel(summary, title="Status", border_style="cyan"),
        active_panel or Text(""),
        Panel(activity, title="Live activity / newest log lines", border_style="green"),
        compilers,
        Text("  ● ready   ○ pending  •  log age reveals stalls  •  q / Ctrl-C to exit", style="dim"),
    )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Recommended terminal: at least 100 columns by 20 rows. The monitor does not resize the terminal.",
    )
    parser.add_argument("--once", action="store_true", help="Print one snapshot and exit")
    parser.add_argument("--interval", type=float, default=1, help="Refresh interval in seconds (default: 1)")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be at least 1 second")
    gcc_config = load_yaml(config.GCC_TOOLCHAIN_TARGETS_YAML)
    clang_config = load_yaml(config.CLANG_TOOLCHAIN_TARGETS_YAML)
    console = Console()
    if args.once:
        console.print(render(snapshot(gcc_config, clang_config), height=1000))
        return
    try:
        with QuitKey() as keys, Live(console=console, screen=True, refresh_per_second=1,
                                    vertical_overflow="crop") as live:
            while True:
                live.update(render(snapshot(gcc_config, clang_config)))
                if keys.wait(args.interval):
                    break
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
