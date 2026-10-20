"""Shared guarded fallback for source trees with stale Autotools outputs."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterator

from binforge.build_monitor_state import update_variant
from binforge.compile_manager import compile_project
from binforge.conf_manager import (
    find_stale_autotools_outputs,
    regenerated_autotools_source,
)
from binforge.logging_helper import logger
from binforge.models import Project


@dataclass(frozen=True)
class AutotoolsProbeResult:
    project: Project
    success: bool


def variant_failure_fingerprint(
    project: Project,
    comb: tuple[str, str, str],
) -> tuple[tuple[str, int, tuple[str, ...]], ...]:
    compiler, arch, opti = comb
    prefix = f"{project.name}_{compiler}_{arch}_{opti}"
    variant_tag = getattr(project, "variant_tag", "").strip()
    if variant_tag:
        prefix = f"{prefix}_{variant_tag}"
    signature = []
    error_pattern = re.compile(
        r"error:|fatal:|undefined reference|no rule to make target|"
        r"cannot find|not found",
        re.IGNORECASE,
    )

    for phase in ("configure", "cmake", "make", "install"):
        returncode_path = Path(project.log_path) / f"{prefix}_{phase}.returncode"
        if not returncode_path.is_file():
            continue
        try:
            returncode = int(returncode_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if returncode == 0:
            continue

        error_lines = []
        for suffix in ("error", "log"):
            log_path = Path(project.log_path) / f"{prefix}_{phase}.{suffix}"
            try:
                lines = log_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                continue
            for line in lines:
                if error_pattern.search(line):
                    normalized = line.replace(project.source_path, "<SOURCE>")
                    normalized = re.sub(
                        r"\bpid[=: ]+\d+\b", "pid=<PID>", normalized,
                        flags=re.IGNORECASE,
                    )
                    error_lines.append(normalized.strip())

        signature.append(
            (phase, returncode, tuple(sorted(set(error_lines))[-8:]))
        )

    return tuple(signature)


def compile_probe_variant(
    project: Project,
    comb: tuple[str, str, str],
    path_env: str,
    extra_compiler_options: dict[str, bool],
    monitor_run_dir: str | None,
    compile_kwargs: dict[str, Any] | None = None,
) -> bool:
    kwargs = dict(compile_kwargs or {})
    return compile_project(
        project=deepcopy(project),
        comb=comb,
        PATH=path_env,
        extra_compiler_options=extra_compiler_options,
        monitor_run_dir=monitor_run_dir,
        publish_result=False,
        **kwargs,
    )


def publish_probe_result(
    monitor_run_dir: str | None,
    project: Project,
    comb: tuple[str, str, str],
    success: bool,
) -> None:
    if not monitor_run_dir:
        return
    try:
        update_variant(
            monitor_run_dir,
            project.name,
            comb,
            state="succeeded" if success else "failed",
            phase="done" if success else "failed",
            error="" if success else None,
        )
    except OSError as exc:
        logger.warning(
            "Could not update build monitor status for %s %s: %s",
            project.name,
            comb,
            exc,
        )


@contextmanager
def guarded_autotools_probe(
    project: Project,
    comb: tuple[str, str, str],
    path_env: str,
    extra_compiler_options: dict[str, bool],
    monitor_run_dir: str | None = None,
    compile_kwargs: dict[str, Any] | None = None,
) -> Iterator[AutotoolsProbeResult | None]:
    """Probe stale Autotools output and yield a usable source for this run.

    ``None`` means no fallback is needed and the caller should use its normal
    build path.  A result means this function already built ``comb``; the
    selected project remains valid until the context exits.
    """
    stale_outputs = find_stale_autotools_outputs(project)
    if not stale_outputs or project.invoke_autoreconf:
        yield None
        return

    logger.info(
        "Stale Autotools outputs detected for %s; probing %s first",
        project.name,
        comb,
    )
    success = compile_probe_variant(
        project,
        comb,
        path_env,
        extra_compiler_options,
        monitor_run_dir,
        compile_kwargs,
    )
    should_regenerate = False
    if not success:
        first_fingerprint = variant_failure_fingerprint(project, comb)
        success = compile_probe_variant(
            project,
            comb,
            path_env,
            extra_compiler_options,
            monitor_run_dir,
            compile_kwargs,
        )
        second_fingerprint = variant_failure_fingerprint(project, comb)
        should_regenerate = (
            not success
            and bool(first_fingerprint)
            and first_fingerprint == second_fingerprint
        )

    source_context = (
        regenerated_autotools_source(project)
        if should_regenerate
        else nullcontext(None)
    )
    selected_project = project
    with source_context as regenerated_project:
        if regenerated_project is not None:
            regenerated_success = compile_probe_variant(
                regenerated_project,
                comb,
                path_env,
                extra_compiler_options,
                monitor_run_dir,
                compile_kwargs,
            )
            if regenerated_success:
                selected_project = regenerated_project
                success = True
                logger.info("Using regenerated Autotools source for %s", project.name)
            else:
                logger.warning(
                    "Regenerated Autotools source did not fix %s; "
                    "continuing with the original source",
                    project.name,
                )
        elif should_regenerate:
            logger.warning(
                "Autotools regeneration failed for %s; continuing with the original source",
                project.name,
            )

        publish_probe_result(monitor_run_dir, project, comb, success)
        yield AutotoolsProbeResult(selected_project, success)
