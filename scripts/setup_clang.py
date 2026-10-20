import sys
import os
import re
import subprocess

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from binforge import config
from scripts.setup_gcc import check_toolchain
from binforge.types_helper import *
from binforge.logging_helper import logger
from binforge.toolchain_status import clang_status, show_clang_status
from binforge.utils import CommandResult, execute_command, load_yaml, mkdir, git_clone

import argparse

TOOL_PATH = config.TOOL_PATH
GCC_PATH = config.GCC_DIR_PATH
BOOTSTRAP_PATH = config.BOOTSTRAP_PATH
PATCH_CLANG = config.PATCH_SETUP_CLANG_DIR_PATH
NUM_JOBS = config.NUM_JOBS
MAX_JOBS = config.MAX_JOBS


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process options for the script.")

    parser.add_argument(
        "--config",
        type=str,
        help="Configure file path. (Default: config/clang_toolchain_targets.yaml)"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check configured Clang installations without building anything."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show each configured Clang version with --status."
    )

    return parser.parse_args()


def apply_patch(
    patch_file: str,
    llvm_project_root: str,
    DEBUG_LEVEL: int = 2
) -> None:
    # Step 1: Check if the patch can be applied
    check_command = f"git apply --check {patch_file}"
    logger.info(f"Checking if patch can be applied: {patch_file}")
    result = execute_command(
        command=check_command,
        directory=llvm_project_root,
        DEBUG_LEVEL=DEBUG_LEVEL,
        exit_on_error=False,
    )

    if result.returncode == 0:
        # Step 2: Apply the patch
        apply_command = f"git apply {patch_file}"
        logger.info(f"[+] Applying patch: {patch_file}")
        applied = execute_command(
            command=apply_command,
            directory=llvm_project_root,
            DEBUG_LEVEL=DEBUG_LEVEL,
            exit_on_error=False,
        )
        if applied.returncode != 0:
            raise RuntimeError(f"Failed to apply patch: {patch_file}")
    else:
        reverse_check = execute_command(
            command=f"git apply --reverse --check {patch_file}",
            directory=llvm_project_root,
            DEBUG_LEVEL=-1,
            exit_on_error=False,
        )
        if reverse_check.returncode == 0:
            logger.info(f"[*] Patch already applied: {patch_file}")
            return
        raise RuntimeError(f"Patch does not apply to this LLVM source: {patch_file}")


def get_bootstrap(config):
    bootstrap = config['bootstrap']

    bootstrap_version, bootstrap_arch, = bootstrap['version'], bootstrap['arch']
    logger.info(f"[DEBUG] bootstrap")
    logger.info(f"[DEBUG]   - version: {bootstrap_version}")
    logger.info(f"[DEBUG]   - arch: {bootstrap_arch}")

    is_bootstrapper_built = check_toolchain(
        bootstrap_version,
        bootstrap_arch,
        compiler_group_name="bootstrap",
        compiler_group_dir=BOOTSTRAP_PATH
        )
    if not is_bootstrapper_built:
        logger.error("[-] Build bootstrap first.")
        logger.error("[-] python3 setup_gcc.py")
        exit(-1)

    bootstrap_bin_path = os.path.join(BOOTSTRAP_PATH, "bootstrap", "bin")
    bootstrap_gcc_bin_path = os.path.join(bootstrap_bin_path, "x86_64-unknown-linux-gnu-gcc")
    bootstrap_gpp_bin_path = os.path.join(bootstrap_bin_path, "x86_64-unknown-linux-gnu-g++")
    for compiler in (bootstrap_gcc_bin_path, bootstrap_gpp_bin_path):
        expected = os.path.join(
            BOOTSTRAP_PATH,
            f"x86_64-unknown-linux-gnu-{bootstrap_version}",
            "bin",
            os.path.basename(compiler),
        )
        if (not os.path.isfile(compiler) or not os.access(compiler, os.X_OK)
                or os.path.realpath(compiler) != os.path.realpath(expected)):
            logger.error(f"[-] Bootstrap compiler link is missing or points elsewhere: {compiler}")
            raise SystemExit(1)

    return bootstrap_gcc_bin_path, bootstrap_gpp_bin_path


def main() -> None:
    args = parse_arguments()
    if args.verbose and not args.status:
        logger.error("[-] --verbose requires --status.")
        exit(2)
    if args.config:
        config_file_path = args.config
    else:
        config_file_path = config.CLANG_TOOLCHAIN_TARGETS_YAML

    bootstrap_bin_dir = os.path.join(BOOTSTRAP_PATH, "bootstrap", "bin")

    clang_config = load_yaml(config_file_path)
    if args.status:
        exit(0 if show_clang_status(clang_config, args.verbose) else 1)

    logger.info(f"[DEBUG] Bootstrap toolchain path: {bootstrap_bin_dir}")

    compiler_group_name = clang_config['compiler_group_name']
    target_versions = clang_config["toolchain_targets"]
    source_commits = load_yaml(os.path.join(config.CONFIG_PATH, "clang_source_commits.yaml"))
    for version in target_versions:
        commit = source_commits.get(version)
        if not commit or not re.fullmatch(r"[0-9a-f]{40}", commit):
            logger.error(f"[-] No pinned LLVM source commit for clang-{version}.")
            raise SystemExit(1)
    CLANG_DIR_PATH = os.path.join(TOOL_PATH, compiler_group_name)
    pending_versions = []
    for version in target_versions:
        _, installation_problems = clang_status(version, CLANG_DIR_PATH)
        if installation_problems:
            logger.info(f"clang-{version} needs installation: {'; '.join(installation_problems)}")
            pending_versions.append(version)
        else:
            logger.info(f"clang-{version} already built.")
    if not pending_versions:
        logger.info("All CLANGs are set up.")
        return

    bootstrap_gcc_bin_path, bootstrap_gpp_bin_path = get_bootstrap(clang_config)

    LOG_PATH = os.path.join(CLANG_DIR_PATH, "logs")

    mkdir(CLANG_DIR_PATH)
    mkdir(LOG_PATH)

    #! TODO: Fix me
    LLVM_PROJECT_ROOT = ""
    if compiler_group_name != "clang":
        logger.error("[-] Not implemented!")
        exit(1)

    LLVM_PROJECT_ROOT = os.path.join(CLANG_DIR_PATH, "llvm-project")
    LLVM_REPO_URL = "https://github.com/llvm/llvm-project.git"
    git_clone(LLVM_REPO_URL, LLVM_PROJECT_ROOT)
    if not os.path.isdir(os.path.join(LLVM_PROJECT_ROOT, ".git")):
        logger.error(f"[-] LLVM source checkout is missing: {LLVM_PROJECT_ROOT}")
        raise SystemExit(1)

    failed_versions = []
    env_vars = {"PATH": bootstrap_bin_dir}

    for version in pending_versions:
        CLANG_PATH = os.path.join(CLANG_DIR_PATH, f"clang-{version}")

        source_commit = source_commits[version]
        command_git_checkout = f"git reset --hard && git clean -fd && git checkout --detach {source_commit} 2>&1"
        result_checkout = execute_command(command_git_checkout, directory=LLVM_PROJECT_ROOT, DEBUG_LEVEL=-1, exit_on_error=False)
        if result_checkout.returncode != 0:
            failed_versions.append((version, "source checkout", result_checkout.returncode))
            continue
        source_head = subprocess.run(
            ["git", "-C", LLVM_PROJECT_ROOT, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
        if source_head.returncode != 0 or source_head.stdout.strip() != source_commit:
            failed_versions.append((version, "source commit verification", source_head.returncode or 1))
            continue

        command_cleanup_build_dir = f"rm -rf {LLVM_PROJECT_ROOT}/build"
        execute_command(command_cleanup_build_dir, DEBUG_LEVEL=-1)
        mkdir(f"{LLVM_PROJECT_ROOT}/build")

        ##! Patch
        need_patch = ("4.0.1", "5.0.2", "8.0.1", "9.0.1", "10.0.1", "11.1.0", "12.0.1", "13.0.1")
        if version in need_patch:
            print(f"[DEBUG] PATCH_CLANG: {PATCH_CLANG}")
            patch_file_path = os.path.join(PATCH_CLANG, f"clang-{version}.patch")
            print(f"[DEBUG] patch_file_path: {patch_file_path}")
            apply_patch(patch_file_path, LLVM_PROJECT_ROOT)

        ##! Only clang
        # command_cmake = f"cmake -DLLVM_ENABLE_PROJECTS=clang -DCMAKE_BUILD_TYPE=Release -DCMAKE_EXE_LINKER_FLAGS=\"-L/usr/local/lib -ldl -lpthread\" -DCMAKE_SHARED_LINKER_FLAGS=\"-L/usr/local/lib -ldl -lpthread\" -DCMAKE_INSTALL_PREFIX={CLANG_PATH} -G \"Unix Makefiles\" ../llvm"

        command_cmake = "cmake"

        if True:
            command_cmake = f"{command_cmake} -DLLVM_ENABLE_PROJECTS=clang"
        else:
            command_cmake = f"{command_cmake} -DLLVM_ENABLE_PROJECTS=\"clang;lld\" "
            if version in (("19.1.7", "19.1.3", "18.1.8", "17.0.6", "16.0.6", "15.0.7", "14.0.6", "13.0.1", "12.0.1")):
                command_cmake = f"{command_cmake} -DLLVM_ENABLE_RUNTIMES=\"compiler-rt\" "

        command_cmake = f"{command_cmake} -G \"Unix Makefiles\""
        command_cmake = f"{command_cmake} -DCMAKE_INSTALL_PREFIX={CLANG_PATH}"

        command_cmake = f"{command_cmake} -DCMAKE_BUILD_TYPE=Release"
        command_cmake = f"{command_cmake} -DCMAKE_C_COMPILER={bootstrap_gcc_bin_path}"
        command_cmake = f"{command_cmake} -DCMAKE_CXX_COMPILER={bootstrap_gpp_bin_path}"

        command_cmake = f"{command_cmake} -DLLVM_ENABLE_BINDINGS=OFF"
        command_cmake = f"{command_cmake} -DCMAKE_C_FLAGS=\"   -Wno-implicit-function-declaration\""
        # command_cmake = f"{command_cmake} -DCMAKE_CXX_STANDARD=14" # for 9.0.1
        if version in ("6.0.1", "7.1.0"):
            #! due to old cmake
            command_cmake += " -Wno-dev"

        command_cmake = f"{command_cmake} ../llvm"

        command_cmake += f" > >(tee {LOG_PATH}/clang-{version}_cmake.log)  2> >(tee {LOG_PATH}/clang-{version}_cmake.error >&2)"
        directory = f"{LLVM_PROJECT_ROOT}/build"
        result_cmake = execute_command(
            command=command_cmake,
            directory=directory,
            exit_on_error=False,
            DEBUG_LEVEL=0,
            NO_STREAM=True,
            env_vars=env_vars,
        )

        if result_cmake.returncode == 0:
            command_make = f"make -j {NUM_JOBS} -l {MAX_JOBS}"
            command_make += f" > >(tee {LOG_PATH}/clang-{version}_make.log)    2> >(tee {LOG_PATH}/clang-{version}_make.error >&2)"
            command_install = "make install"
            command_install += f" > >(tee {LOG_PATH}/clang-{version}_install.log) 2> >(tee {LOG_PATH}/clang-{version}_install.error >&2)"
            result_make = execute_command(command=command_make, directory=directory, exit_on_error=False, DEBUG_LEVEL=1, NO_STREAM=True, env_vars=env_vars)
            if result_make.returncode == 0:
                result_install = execute_command(command=command_install, directory=directory, exit_on_error=False, DEBUG_LEVEL=0, env_vars=env_vars)
                if result_install.returncode != 0:
                    failed_versions.append((version, "install", result_install.returncode))
                elif clang_status(version, CLANG_DIR_PATH)[1]:
                    failed_versions.append((version, "installation verification", 1))
            else:
                failed_versions.append((version, "make", result_make.returncode))
        else:
            failed_versions.append((version, "cmake", result_cmake.returncode))

    if failed_versions:
        for version, stage, returncode in failed_versions:
            logger.error(f"clang-{version}: {stage} failed (exit {returncode})")
        logger.error(f"{len(failed_versions)} CLANG version(s) failed to set up.")
        raise SystemExit(1)

    logger.info("All CLANGs are set up.")


if __name__ == "__main__":
    main()

# EOF
