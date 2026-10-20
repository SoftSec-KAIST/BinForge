import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import time
import datetime
import argparse
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from itertools import product
from pathlib import Path
import yaml

from binforge import config
from binforge.types_helper import *
from binforge.logging_helper import logger
from binforge.models import Project
from binforge.utils import (
    file_exists,
    get_env_variable,
    load_yaml,
    mkdir,
    normalize_version,
    parallel_execute_func,
)
from binforge.source_manager import download_and_extract_projects
from binforge.compile_manager import compile_project
from binforge.conf_manager import analyze_projects
from binforge.compiler_options import compiler_option_sets, configured_compiler_options
from binforge.autotools_fallback import guarded_autotools_probe
from binforge.extra_deps_manager import DEFAULT_DEP_JOBS, compile_extra_deps, compute_dependency_layers
from binforge.build_monitor_state import (read_json, start_run, update_run,
                                          start_suite, update_suite, update_variant)
from binforge.source_snapshot import PAPER_DEFAULT_BUNDLE, PAPER_WORK_ROOT, materialize_projects, paper_snapshot_root


def compile_suite_variant(job: tuple) -> bool:
    project, comb, options, run_dir, path_env = job
    return compile_project(project=project, comb=comb, PATH=path_env,
                           extra_compiler_options=options,
                           monitor_run_dir=str(run_dir))


def run_option_combinations(args: argparse.Namespace, cfg: dict) -> None:
    option_sets = compiler_option_sets(cfg["build"])
    modes = [name for name, _ in option_sets]
    if not args.build or args.download or args.build_deps or args.auto_resolve:
        raise ValueError("combine_extra_compiler_options requires --build without other build modes")
    if args.num_jobs < 1:
        raise ValueError("--num-jobs must be >= 1")
    variants_per_mode = (len(cfg["targets"]) * len(cfg["build"]["compiler"])
                         * len(cfg["build"]["arch"]) * len(cfg["build"]["opti"]))
    suite_dir = start_suite(args.base_path, args.config, modes, variants_per_mode,
                            cfg, project_mode=True)
    groups = {}
    try:
        paper = getattr(args, "source_mode", "auto") == "paper"
        context = (paper_snapshot_root(Path(args.source_snapshot), PAPER_WORK_ROOT)
                   if paper else nullcontext(None))
        with context as snapshot_root:
            for index, project_name in enumerate(cfg["targets"]):
                for mode, options in option_sets:
                    child = deepcopy(cfg)
                    child["targets"] = [project_name]
                    child["build"]["combine_extra_compiler_options"] = False
                    child["build"]["extra_compiler_options"] = options
                    name = f"{mode}.yaml" if index == 0 else f"{mode}-{index}.yaml"
                    child_config = suite_dir / name
                    child_config.write_text(yaml.safe_dump(child, sort_keys=False), encoding="utf-8")
                    base_path = str(Path(args.base_path) / mode / project_name)
                    projects = load_build_config(child, [project_name], base_path)
                    project = next(item for item in projects if item.name == project_name)
                    mkdir(project.bins_path, exist_ok=True)
                    mkdir(project.log_path, exist_ok=True)
                    source_mode = getattr(args, "source_mode", "auto")
                    if source_mode in ("remote", "archive"):
                        if project.source_type != "url":
                            raise ValueError("--source-mode remote/archive currently supports URL sources only")
                        project.source_mode = source_mode
                    run_dir = start_run(base_path, str(child_config), child)
                    groups[(mode, project_name)] = (project, options, run_dir)
                    update_suite(suite_dir, phase=f"prepare {project_name}/{mode}")
                    if paper:
                        materialize_projects(projects, [project_name], snapshot_root,
                                             args.source_dataset)
                    else:
                        download_and_extract_projects(projects, [project_name])
                    update_run(run_dir, phase="analyze")
                    analyze_projects(projects, [project_name])
                    update_run(run_dir, phase="build")

        path_env = get_env_variable("PATH")
        failures = {}
        for project_name in cfg["targets"]:
            update_suite(suite_dir, phase=f"build {project_name}")
            first_comb = (cfg["build"]["compiler"][0], cfg["build"]["arch"][0],
                          cfg["build"]["opti"][0])
            with ExitStack() as sources:
                selected = {}
                probed = {}
                for mode in modes:
                    project, options, run_dir = groups[(mode, project_name)]
                    probe = sources.enter_context(guarded_autotools_probe(
                        project, first_comb, path_env, options, str(run_dir)))
                    selected[mode] = (probe.project if probe else project, options, run_dir)
                    if probe is not None:
                        probed[mode] = probe.success

                combinations = list(product(cfg["build"]["compiler"],
                                            cfg["build"]["arch"],
                                            cfg["build"]["opti"], modes))
                jobs = []
                job_keys = []
                for compiler, arch, opti, mode in combinations:
                    comb = (compiler, arch, opti)
                    if comb == first_comb and mode in probed:
                        continue
                    project, options, run_dir = selected[mode]
                    jobs.append((project, comb, options, run_dir, path_env))
                    job_keys.append((comb, mode))
                results = parallel_execute_func(compile_suite_variant, jobs,
                                                dynamic_arg_name="job",
                                                max_parallel_tasks=min(args.num_jobs, 32)) if jobs else []
                outcomes = dict(zip(job_keys, results))
                outcomes.update({(first_comb, mode): success
                                 for mode, success in probed.items()})
                for compiler, arch, opti, mode in combinations:
                    key = mode, project_name
                    failures[key] = failures.get(key, 0) + (not outcomes[((compiler, arch, opti), mode)])
            for mode in modes:
                key = mode, project_name
                run_dir = groups[key][2]
                update_run(run_dir, state="failed" if failures.get(key) else "succeeded",
                           phase="done")
        if any(failures.values()):
            raise RuntimeError(f"{sum(failures.values())} suite variants failed")
        update_suite(suite_dir, state="succeeded", phase="done")
    except BaseException as exc:
        for _, _, run_dir in groups.values():
            state = read_json(run_dir / "run.json") or {}
            if state.get("state") == "running":
                update_run(run_dir, state="failed", phase="failed",
                           error=f"{type(exc).__name__}: {exc}")
        update_suite(suite_dir, state="failed", phase="failed",
                     error=f"{type(exc).__name__}: {exc}")
        raise


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process options for the script.")

    parser.add_argument(
        "--base-path",
        type=str,
        default=os.path.expanduser("~/base"),
        help="Path to store output files (default: $HOME/base)"
    )
    parser.add_argument(
        "--num-jobs",
        type=int,
        default=int(config.NUM_JOBS),
        help="Maximum concurrent build variants, capped at 32 (default: config NUM_JOBS)"
    )
    parser.add_argument(
        "--config",
        type=str,
        required=True,
        help="Path to the configuration file"
    )
    parser.add_argument("--source-snapshot", type=str,
                        help=f"Paper source snapshot tar (default: {PAPER_DEFAULT_BUNDLE})")
    parser.add_argument("--source-dataset", choices=("binkit", "popcon"),
                        help="Dataset namespace in paper source snapshot")
    parser.add_argument("--source-mode", choices=("auto", "remote", "archive", "paper"),
                        default="auto",
                        help="paper uses the fixed offline snapshot; other modes use workspace, remote, or per-project archives")
    parser.add_argument(
        "--download",
        action="store_true",
        help="Only download and extract the source code without building"
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Build"
    )
    parser.add_argument(
        "--build-deps",
        action="store_true",
        help="Compile all dependencies required by the specified projects"
    )
    parser.add_argument("--deps-jobs", type=int, default=DEFAULT_DEP_JOBS,
                        help=f"Maximum concurrent dependency builds (default: {DEFAULT_DEP_JOBS})")
    parser.add_argument(
        "--auto-resolve",
        action="store_true",
        help="Automatically resolve missing dependencies using OpenAI LLM and reattempt build"
    )
    parser.add_argument(
        "--llm-api-key-path",
        type=str,
        default="",
        help="Path to a file containing the API key used by BinForge buildfixer"
    )
    parser.add_argument(
        "--llm-max-iter",
        type=int,
        default=20,
        help="Maximum LLM repair iterations per target project (default: 20)"
    )
    parser.add_argument(
        "--disable-dependency-source-lookup",
        action="store_true",
        help="Disable APT-based dependency source lookup for buildfixer ablation"
    )
    parser.add_argument(
        "--auto-resolve-use-extra-deps",
        action="store_true",
        help=(
            "Use the accumulated tools/extra_deps dependency root during "
            "auto-resolve (default: use an isolated per-target dependency root)"
        ),
    )
    parser.add_argument(
        "--expr",
        action="store_true",
        help="Experimental features"
    )

    return parser.parse_args()


def compile_projects(
    projects: list[Project],
    targets: list[str],
    config_file_path: str,
    num_jobs: int,
    monitor_run_dir: str | None = None,
) -> None:
    if not file_exists(config_file_path):
        raise FileNotFoundError(f"Config file does not exist: {config_file_path}")

    config = load_yaml(config_file_path)["build"]

    optmz_list = config["opti"]
    arch_list = config["arch"]
    compiler_list = config["compiler"]
    num_opt = len(optmz_list) * len(arch_list) * len(compiler_list)
    logger.info("Configuration variants per project: %d", num_opt)

    extra_compiler_options, _ = configured_compiler_options(config)

    logger.info("[*] Selected compiler flag set:")
    logger.info(f"    - pie:             {extra_compiler_options['pie']}")
    logger.info(f"    - no_inline:       {extra_compiler_options['no_inline']}")
    logger.info(f"    - stack_protector: {extra_compiler_options['stack_protector']}")

    if num_jobs < 1:
        raise ValueError("--num-jobs must be >= 1")
    max_parallel_tasks = min(num_jobs, 32)

    #! Set target projects and all projects
    all_projects = projects
    projects = []
    for proj in all_projects:
        if proj.name in targets:
            projects.append(proj)

    failed_projects: list[tuple[str, int, int]] = []
    for project in projects:
        logger.info(f"Project name: {project.name}")
        mkdir(project.bins_path, exist_ok=True)
        mkdir(project.log_path, exist_ok=True)
        combinations = list(product(compiler_list, arch_list, optmz_list))

        path_env = get_env_variable("PATH")

        t0 = time.time()
        first_comb = combinations[0]
        with guarded_autotools_probe(
            project,
            first_comb,
            path_env,
            extra_compiler_options,
            monitor_run_dir,
        ) as probe:
            if probe is None:
                results = parallel_execute_func(
                    func=compile_project,
                    dynamic_values_list=combinations,
                    dynamic_arg_name="comb",
                    max_parallel_tasks=max_parallel_tasks,
                    project=project,
                    PATH=path_env,
                    extra_compiler_options=extra_compiler_options,
                    monitor_run_dir=monitor_run_dir,
                )
            else:
                remaining_combinations = combinations[1:]
                if remaining_combinations:
                    remaining_results = parallel_execute_func(
                        func=compile_project,
                        dynamic_values_list=remaining_combinations,
                        dynamic_arg_name="comb",
                        max_parallel_tasks=max_parallel_tasks,
                        project=probe.project,
                        PATH=path_env,
                        extra_compiler_options=extra_compiler_options,
                        monitor_run_dir=monitor_run_dir,
                    )
                else:
                    remaining_results = []
                results = [probe.success, *remaining_results]
        elapsed_time = time.time() - t0

        failed_count = results.count(False)
        if failed_count:
            logger.error("Build variants failed. (%s: %d of %d)",
                         project.name, failed_count, len(results))
            failed_projects.append((project.name, failed_count, len(results)))
        else:
            logger.info(f"Compiled done. ({project.name})")
        logger.info(f"Elapsed time: {elapsed_time}")

        ##! log
        benchmark_dir_path = project.benchmark_dir_path
        benchmark_file_path = os.path.join(benchmark_dir_path, project.name + ".log")
        mkdir(benchmark_dir_path, exist_ok=True)
        with open(benchmark_file_path, "a") as log_file:
            log_file.write(f"{datetime.datetime.now()}\n")
            log_file.write(f"{config_file_path}\n")
            log_file.write(f"{elapsed_time}\n\n")

    if failed_projects:
        summary = ", ".join(
            f"{name}: {failed} of {total}"
            for name, failed, total in failed_projects
        )
        raise RuntimeError(f"Build variants failed ({summary})")


def validate_structure(cfg: dict[str, Any]) -> None:
    if not isinstance(cfg, dict):
        raise ValueError("Configuration must be a mapping")
    required = ["build", "targets", "projects"]
    for key in required:
        if key not in cfg:
            raise ValueError(f"Missing required top-level key: {key}")
    if not isinstance(cfg["build"], dict):
        raise ValueError("build must be a mapping")
    configured_compiler_options(cfg["build"])
    for key in ("opti", "arch", "compiler"):
        value = cfg["build"].get(key)
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError(f"build.{key} must be a nonempty list of names")
    if not isinstance(cfg["targets"], list) or not cfg["targets"] or not all(isinstance(target, str) and target.strip() for target in cfg["targets"]):
        raise ValueError("targets must be a nonempty list of project names")
    if not isinstance(cfg["projects"], dict):
        raise ValueError("projects must be a mapping of name → metadata")


def get_safe_list(meta: dict[str, Any], key: str) -> list[str]:
    value = meta.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    raise ValueError(f"{key} must be a list, got {type(value)}")


def _normalize_project_meta(name: str, meta: dict[str, Any] | None) -> dict[str, Any]:
    if meta is None or (isinstance(meta, str) and meta.strip().lower() in ("", "null", "~")):
        return {"source": {"type": "apt", "value": name}}
    elif isinstance(meta, dict):
        if "source" not in meta:
            meta["source"] = {"type": "apt", "value": name}
    else:
        raise ValueError(f"projects.{name}: unsupported meta type {type(meta)}")
    return meta


def parse_projects(cfg: dict[str, Any], base_path: str) -> list[Project]:
    projects: list[Project] = []
    raw = cfg["projects"]
    for name, raw_meta in raw.items():
        meta = _normalize_project_meta(name, raw_meta)

        src = meta.get("source", {})
        stype = src.get("type")
        svalue = src.get("value")
        if not stype or svalue in (None, ""):
            raise ValueError(f"{name}: invalid source definition")

        proj = Project(name=name, base_path=base_path)

        proj.source_type = stype
        proj.source_value = svalue
        proj.source_commit = src.get("commit") or src.get("ref")

        source_version = src.get("version")
        proj.version = normalize_version(meta.get("version", source_version or proj.source_commit))

        archive = (src.get("archive") or {})
        proj.archive_group = archive.get("group")
        proj.archive_name = archive.get("name")
        proj.archive_md5 = archive.get("md5")
        proj.archive_root = config.ARCHIVE_ROOT

        proj.conf_options = " ".join(get_safe_list(meta, "options"))
        proj.cflags      = " ".join(get_safe_list(meta, "cflags"))
        proj.ldflags     = " ".join(get_safe_list(meta, "ldflags"))
        proj.dep_list    = get_safe_list(meta, "deps") # type: ignore

        projects.append(proj)

    return projects


def load_build_config(cfg, targets: list[str], base_path: str) -> list[Project]:
    projects = parse_projects(cfg, base_path)

    project_names_list = [p.name for p in projects]

    # Reference Validation
    for t in targets:
        if t not in project_names_list:
            raise ValueError(f"Target '{t}' not defined in projects")

    for proj in projects:
        for dep in proj.dep_list or []:
            if dep not in project_names_list:
                raise ValueError(f"Project '{proj.name}' references unknown dep '{dep}'")

    return projects


def main() -> None:
    args = parse_arguments()
    if args.build and args.num_jobs < 1:
        raise ValueError("--num-jobs must be >= 1")
    args.base_path = os.path.abspath(os.path.expanduser(args.base_path))
    if args.source_mode == "paper":
        if not args.source_dataset:
            raise ValueError("--source-mode paper requires --source-dataset")
        if args.auto_resolve:
            raise ValueError("--source-mode paper does not support --auto-resolve")
        if not args.source_snapshot:
            args.source_snapshot = str(PAPER_DEFAULT_BUNDLE)
        args.source_snapshot = os.path.abspath(os.path.expanduser(args.source_snapshot))
    elif args.source_snapshot or args.source_dataset:
        raise ValueError("--source-snapshot and --source-dataset require --source-mode paper")
    cfg = load_yaml(args.config)

    validate_structure(cfg)

    _, combine = configured_compiler_options(cfg["build"])
    if combine and args.build and not args.auto_resolve and not args.build_deps:
        if args.download:
            raise ValueError("combine_extra_compiler_options requires --build without --download")
        if len(compiler_option_sets(cfg["build"])) > 1:
            load_build_config(cfg, cfg["targets"], args.base_path)
            run_option_combinations(args, cfg)
            return

    targets = cfg["targets"]
    projects = load_build_config(cfg, targets, args.base_path)
    if args.source_mode in ("remote", "archive"):
        for project in projects:
            if project.name in targets:
                if project.source_type != "url":
                    raise ValueError("--source-mode remote/archive currently supports URL sources only")
                project.source_mode = args.source_mode

    if args.auto_resolve:
        if not args.llm_api_key_path:
            raise ValueError("--auto-resolve requires --llm-api-key-path")
        if args.llm_max_iter < 1:
            raise ValueError("--llm-max-iter must be >= 1")
        key_path = os.path.abspath(os.path.expanduser(args.llm_api_key_path))
        if not os.path.isfile(key_path):
            raise ValueError(f"LLM API key file does not exist: {key_path}")
        os.environ["BINFORGE_OPENAI_API_KEY_PATH"] = key_path
        os.environ.pop("BINFORGE_OPENAI_API_KEY", None)

        from binforge.llm_manager import auto_resolve
        success = auto_resolve(
            projects,
            targets,
            source_config_path=args.config or "",
            max_iter=args.llm_max_iter,
            dependency_source_lookup=not args.disable_dependency_source_lookup,
            use_extra_deps=args.auto_resolve_use_extra_deps,
        )
        if not success:
            raise RuntimeError("Auto-resolve failed for one or more target projects")
        return

    if args.auto_resolve_use_extra_deps:
        raise ValueError("--auto-resolve-use-extra-deps requires --auto-resolve")

    if args.build_deps:
        if args.deps_jobs < 1:
            raise ValueError("--deps-jobs must be >= 1")
        layers = compute_dependency_layers(projects, targets)
        dep_cfg = {"targets": [name for layer in layers for name in layer],
                   "build": {"compiler": ["gcc-13.3.0"],
                             "arch": cfg["build"]["arch"], "opti": ["O2"]}}
        run_dir = start_run(config.EXTRA_DEP_PATH, args.config, dep_cfg,
                            kind="dependencies") if layers else None
        try:
            if args.source_mode == "paper":
                with paper_snapshot_root(Path(args.source_snapshot), PAPER_WORK_ROOT) as snapshot_root:
                    compile_extra_deps(projects, targets, cfg["build"]["arch"],
                                       snapshot_root=snapshot_root,
                                       source_dataset=args.source_dataset, jobs=args.deps_jobs,
                                       monitor_run_dir=run_dir)
            else:
                compile_extra_deps(projects, targets, cfg["build"]["arch"],
                                   jobs=args.deps_jobs, monitor_run_dir=run_dir)
        except BaseException as exc:
            if run_dir:
                update_run(run_dir, state="failed", phase="failed",
                           error=f"{type(exc).__name__}: {exc}")
            raise
        else:
            if run_dir:
                update_run(run_dir, state="succeeded", phase="done")
        return

    run_dir = start_run(args.base_path, args.config, cfg) if args.build and not args.download else None
    try:
        if args.source_mode == "paper":
            with paper_snapshot_root(Path(args.source_snapshot), PAPER_WORK_ROOT) as snapshot_root:
                materialize_projects(projects, targets, snapshot_root,
                                     args.source_dataset)
        else:
            download_and_extract_projects(projects, targets)
        if args.download:
            if run_dir:
                update_run(run_dir, state="succeeded", phase="download")
            return

        if run_dir:
            update_run(run_dir, phase="analyze")
        analyze_projects(projects, targets)

        if args.build:
            if run_dir:
                update_run(run_dir, phase="build")
            compile_projects(projects, targets, args.config, args.num_jobs,
                             str(run_dir) if run_dir else None)
    except BaseException as exc:
        if run_dir:
            update_run(run_dir, state="failed", phase="failed",
                       error=f"{type(exc).__name__}: {exc}")
        raise
    else:
        if run_dir:
            update_run(run_dir, state="succeeded", phase="done")


if __name__ == "__main__":
    main()


# EOF
