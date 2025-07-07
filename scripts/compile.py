import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from binforge.config import NUM_JOBS
from binforge.types_helper import *
from binforge.logging_helper import logger
from binforge.source_manager import parse_project_list, download_and_extract_projects
from binforge.compile_manager import compile_project
from binforge.conf_manager import analyze_dependencies
from binforge.extra_deps_manager import compile_extra_deps
from binforge.models import Project
from binforge.utils import mkdir, get_env_variable, parallel_execute_func, file_exists, load_yaml

import time
import datetime
import argparse

from itertools import product

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process options for the script.")

    parser.add_argument(
        "--base-path",
        type=str,
        default=os.path.expanduser("~/base"),
        help="Path to store output files (default: $HOME/base)"
    )
    parser.add_argument(
        "-P", "--project-list-file",
        type=str,
        help="Path to project list file (default: config/projects.txt)"
    )
    parser.add_argument(
        "-p", "--project-list",
        type=str,
        help="Comma-separated list of projects (e.g., -p=\"proj1, proj2\")"
    )
    parser.add_argument(
        "--num-jobs",
        type=int,
        default=int(NUM_JOBS),
        help="Number of jobs to run in parallel (default: value from NUM_JOBS environment variable)"
    )
    parser.add_argument(
        "--config",
        type=str,
        help="Path to the configuration file"
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Only download and extract the source code without building"
    )
    parser.add_argument(
        "--check-deps",
        action="store_true",
        help="Analyze dependencies for the given project list without performing compilation"
    )
    parser.add_argument(
        "--build-deps",
        action="store_true",
        help="Compile all dependencies required by the specified projects"
    )

    return parser.parse_args()


def compile_projects(
        projects: list[Project],
        config_file_path: str,
        num_jobs: int
) -> None:
    if not file_exists(config_file_path):
        logger.error(f"File not exist: {config_file_path}")
        exit(0)

    config = load_yaml(config_file_path)

    optmz_list = config["opti"]
    arch_list = config["arch"]
    compiler_list = config["compiler"]
    num_opt = len(optmz_list) * len(arch_list) * len(compiler_list)
    logger.info("Total %d options!", num_opt)

    opts = config.get("extra_compiler_options") or {}
    pie = opts.get("pie", False) or False
    no_inline = opts.get("no_inline", False) or False
    stack_protector = opts.get("stack_protector", False) or False

    extra_compiler_options = {
        "pie": pie,
        "no_inline": no_inline,
        "stack_protector": stack_protector
    }

    logger.info("[*] Selected extra compiler options:")
    logger.info(f"    - pie: {pie}")
    logger.info(f"    - no_inline: {no_inline}")
    logger.info(f"    - stack_protector: {stack_protector}")

    if num_jobs >= 32:
        max_parallel_tasks = 32
    else:
        max_parallel_tasks = 16

    for project in projects:
        logger.info(f"Project name: {project.name}")
        mkdir(project.bins_path, exist_ok=True)
        mkdir(project.log_path, exist_ok=True)
        combinations = list(product(compiler_list, arch_list, optmz_list))

        path_env = get_env_variable("PATH")

        t0 = time.time()
        _ = parallel_execute_func(
            func=compile_project,
            dynamic_values_list=combinations,
            dynamic_arg_name="comb",
            max_parallel_tasks=max_parallel_tasks,
            project=project,
            PATH=path_env,
            extra_compiler_options=extra_compiler_options
        )
        elapsed_time = time.time() - t0

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


def main() -> None:
    args = parse_arguments()

    logger.info(f"Number of Jobs: {args.num_jobs}")
    ##! Build dependencies
    if args.build_deps:
        logger.info("Install extra dependencies..")
        compile_extra_deps()
        return

    if args.project_list_file and args.project_list:
        logger.error(f"Use either the -p or -P option, but not both.")
        return None
    elif args.project_list_file and not args.project_list:
        logger.info(f"Target project list file: {args.project_list_file}")
        TARGET_PROJECTS_FROM_FILE = True
    elif not args.project_list_file and args.project_list:
        logger.info(f"Target project list: {args.project_list}")
        TARGET_PROJECTS_FROM_FILE = False
    else:
        logger.error("No action was specified.")
        logger.error("Exit script.")
        return

    logger.info(f"Base Path: {args.base_path}")
    logger.info(f"Number of Jobs: {args.num_jobs}")
    logger.info(f"Config File: {args.config if args.config else 'Not specified'}")

    projects = parse_project_list(args, TARGET_PROJECTS_FROM_FILE)
    if not projects:
        logger.error("Wrong target project names..")
        logger.error("Exit script.")
        return None

    logger.info("Download option specified..")
    download_and_extract_projects(projects)

    if args.download:
        return

    if args.check_deps:
        analyze_dependencies(projects)
        return

    if not args.config:
        logger.error("No config provided.")
        return

    logger.info(f"Config provided: {args.config}")

    base_path = args.base_path
    log_path = os.path.join(base_path, "log")
    bins_path = os.path.join(base_path, "bins")
    mkdir(log_path, exist_ok=True)
    mkdir(bins_path, exist_ok=True)

    logger.info("Multiprocessing: %d jobs", args.num_jobs)

    compile_projects(projects, args.config, args.num_jobs)


if __name__ == "__main__":
    main()

# EOF
