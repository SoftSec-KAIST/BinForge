import sys
import os
import re
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import argparse
import filecmp

from binforge import config
from binforge.types_helper import *
from binforge.logging_helper import logger
from binforge.toolchain_status import gcc_status
from binforge.toolchain_status import show_gcc_status

from binforge.utils import (
    execute_command,
    file_exists,
    load_yaml,
    mkdir,
)

TOOL_PATH = config.TOOL_PATH
GCC_DIR_PATH = config.GCC_DIR_PATH

BOOTSTRAP_PATH = os.path.join(TOOL_PATH, "bootstrap")
PATCH_GCC = config.PATCH_SETUP_GCC_DIR_PATH

CTNG_CONF_PATH = config.CTNG_CONF_PATH
CTNG_BIN = config.CTNG_BIN

NUM_JOBS = config.NUM_JOBS
MAX_JOBS = config.MAX_JOBS
DEFAULT_PARALLEL_TOOLCHAINS = 4


ARCH_GCC_PREFIX_MAP = {
    "x86_32": "i686-unknown-linux-gnu",
    "x86_64": "x86_64-unknown-linux-gnu",
    "arm_32": "arm-unknown-linux-gnueabi",
    "arm_64": "aarch64-unknown-linux-gnu",
    "mips_32": "mipsel-unknown-linux-gnu",
    "mips_64": "mips64el-unknown-linux-gnu",
    "mipseb_32": "mips-unknown-linux-gnu",
    "mipseb_64": "mips64-unknown-linux-gnu",
    "ppceb_32": "powerpc-unknown-linux-gnu",
    "ppceb_64": "powerpc64-unknown-linux-gnu",
    "ppc_64": "powerpc64le-unknown-linux-gnu",
    "riscv_32": "riscv32-unknown-linux-gnu",
    "riscv_64": "riscv64-unknown-linux-gnu",
}


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process options for the script.")

    parser.add_argument(
        "--config",
        type=str,
        help="Configure file path. (Default: config/gcc_toolchain_targets.yaml)"
    )
    parser.add_argument(
        "--build-bootstrap",
        action="store_true",
        help="Build bootstrapper only."
    )
    parser.add_argument(
        "--build-toolchains",
        action="store_true",
        help="Build configured GCC toolchains."
    )
    parser.add_argument(
        "--parallel-toolchains",
        type=int,
        default=None,
        metavar="N",
        help=f"Build up to N GCC targets at once (default: {DEFAULT_PARALLEL_TOOLCHAINS})."
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check configured GCC installations without building anything."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show every configured GCC target with --status."
    )

    return parser.parse_args()


def get_ctng_conf_file_path(version: str, arch: str) -> str:
    return  os.path.join(CTNG_CONF_PATH, version, arch) + ".conf"


def configure_ctng_limits(path: str) -> None:
    """Set the limits consumed by crosstool-NG's nested component builds."""
    config_path = Path(path)
    contents = config_path.read_text()
    for key, value in (("CT_PARALLEL_JOBS", str(NUM_JOBS)),
                       ("CT_LOAD", f'"{MAX_JOBS}"')):
        contents, count = re.subn(rf"^{key}=.*$", f"{key}={value}", contents,
                                  flags=re.MULTILINE)
        if count != 1:
            raise ValueError(f"Expected one {key} setting in {path}; found {count}")
    config_path.write_text(contents)


def check_toolchain(
    version: str,
    arch: str,
    compiler_group_name: str = "gcc",
    compiler_group_dir: str = GCC_DIR_PATH
) -> bool:

    _, _, problems = gcc_status(version, arch, ARCH_GCC_PREFIX_MAP[arch], compiler_group_dir)
    is_toolchain_built = not problems
    if is_toolchain_built:
        logger.info(f"[+] Toolchain already built ({compiler_group_name}-{arch}-{version})")
    else:
        logger.info(f"[-] Toolchain not ready ({compiler_group_name}-{arch}-{version}): {'; '.join(problems)}")

    return is_toolchain_built


def link_bootstrap(version: str, arch: str) -> bool:
    bootstrap_path = os.path.join(BOOTSTRAP_PATH, ARCH_GCC_PREFIX_MAP[arch] + "-" + version)
    link_path = os.path.join(BOOTSTRAP_PATH, "bootstrap")
    if os.path.islink(link_path) and os.path.realpath(link_path) == os.path.realpath(bootstrap_path):
        return True
    if os.path.islink(link_path):
        os.unlink(link_path)
    elif os.path.lexists(link_path):
        logger.error(f"[-] Bootstrap link path is occupied: {link_path}")
        return False
    os.symlink(bootstrap_path, link_path)

    return True


def build_bootstrap(version: str, arch: str) -> bool:
    is_bootstrap_installed = build_toolchain(
        version,
        arch,
        bootstrap_bin_dir="",
        compiler_group_name="bootstrap",
        compiler_group_dir=BOOTSTRAP_PATH
    )
    return is_bootstrap_installed and link_bootstrap(version, arch)


def build_toolchain(
    version: str,
    arch: str,
    bootstrap_bin_dir: str = "",
    compiler_group_name: str = "gcc",
    compiler_group_dir: str = GCC_DIR_PATH,
) -> bool:

    BUILD_PATH = os.path.join(compiler_group_dir, "build")

    if bootstrap_bin_dir:
        # Build toolchain(s) with bootstrap
        logger.info(f"[DEBUG] bootstrap! {bootstrap_bin_dir}")
    else:
        # Build bootstrap
        logger.info(f"[DEBUG] Bootstrap toolchain not found!")

    build_path = os.path.join(BUILD_PATH, version)
    mkdir(path=build_path, exist_ok=True)

    conf_file_path = get_ctng_conf_file_path(version, arch)
    new_conf_file_path = os.path.join(build_path, arch + ".conf")
    execute_command(f"cp {conf_file_path} {new_conf_file_path}", DEBUG_LEVEL=-1)

    #! Work dir
    work_dir = new_conf_file_path.removesuffix(".conf")
    if config.SYSTEM_NAME == "fedora":
        execute_command(f"sed -i 's/CT_EXTRA_LDFLAGS_FOR_BUILD=\"\"/CT_EXTRA_LDFLAGS_FOR_BUILD=\"-L\\/usr\\/local\\/lib\"/g' {new_conf_file_path}")
    mkdir(work_dir, exist_ok=True)
    execute_command(f"cp {new_conf_file_path} {work_dir}/.config", DEBUG_LEVEL=-1)
    configure_ctng_limits(f"{work_dir}/.config")

    logger.info(f"Build.. ({version}, {arch})")

    command_build = ""
    if version in ("11.4.0"):
        patch_target_file_path = f"{work_dir}/.build/src/gcc-11.4.0/libiberty/simple-object-mach-o.c"
        command_build = f"{CTNG_BIN} -s build.{NUM_JOBS} STOP=kernel_headers && "
        command_build += f"patch {patch_target_file_path} {PATCH_GCC}/gcc-{version}.patch && "
        command_build += f"{CTNG_BIN} -s build.{NUM_JOBS} START=cc_core"
    elif version in (("7.5.0", "8.5.0", "9.5.0")) and arch in (("riscv_32", "riscv_64")):
        command_build = f"{CTNG_BIN} -s build.{NUM_JOBS} STOP=cc_for_build && "
        patch_target_file_path1 = f"{work_dir}/.build/src/gcc-{version}/libgcc/config/riscv/div.S"
        patch_target_file_path2 = f"{work_dir}/.build/src/gcc-{version}/libgcc/config/riscv/riscv-asm.h"
        command_build += f"cp {PATCH_GCC}/tmp/div.S {patch_target_file_path1} && "
        command_build += f"cp {PATCH_GCC}/tmp/riscv-asm.h {patch_target_file_path2} && "
        command_build += f"{CTNG_BIN} -s build.{NUM_JOBS} START=cc_for_build"
    else:
        command_build = f"{CTNG_BIN} -s build.{NUM_JOBS}"

    ##! Compile toolchain!
    env_vars = {
        "TOOL_PATH": TOOL_PATH,
        "TOOLCHAIN_PREFIX_NAME": compiler_group_name
    }
    if bootstrap_bin_dir:
        # Keep PATH local to this subprocess: GCC targets build in parallel.
        env_vars["PATH"] = bootstrap_bin_dir
    result = execute_command(
        command = command_build,
        directory = work_dir,
        DEBUG_LEVEL = 2,
        NO_STREAM = True,
        exit_on_error = False,
        env_vars = env_vars
    )
    if result.returncode != 0:
        logger.error(f"[-] crosstool-ng failed ({version}, {arch}, exit {result.returncode}); build files kept in {work_dir}")
        return False

    is_gcc_installed = check_toolchain(
        version,
        arch,
        compiler_group_name=compiler_group_name,
        compiler_group_dir=compiler_group_dir
    )

    if not is_gcc_installed:
        logger.error(f"[-] Built toolchain did not pass installation checks; build files kept in {work_dir}")
        return False

    #! Cleanup only after a verified installation.
    execute_command(f"rm -rf {work_dir}/.build {work_dir}/.config")
    return True


def build_all_toolchains(
    compiler_group_name: str,
    toolchain_targets: Any,
    parallel_toolchains: int = DEFAULT_PARALLEL_TOOLCHAINS,
) -> bool:

    compiler_group_dir = os.path.join(TOOL_PATH, compiler_group_name)

    bootstrap_bin_dir = os.path.join(BOOTSTRAP_PATH, "bootstrap", "bin")
    if file_exists(os.path.join(bootstrap_bin_dir, f"{ARCH_GCC_PREFIX_MAP["x86_64"]}-gcc")):
        logger.info(f"[*] Bootstrap toolchain path: {bootstrap_bin_dir}")
    else:
        logger.error("[-] Build bootstrap toolchain first.")
        return False

    failures = []
    for version, archs in toolchain_targets.items():
        print(f"[DEBUG] version: {version}")

        with ThreadPoolExecutor(max_workers=parallel_toolchains) as executor:
            futures = {}
            for arch in archs:
                result = check_toolchain(
                    version,
                    arch,
                    compiler_group_name=compiler_group_name,
                    compiler_group_dir=compiler_group_dir
                )
                print(f"[DEBUG] check_toolchain({version}, {arch}) -> {result}")

                if not result:
                    futures[
                        executor.submit(
                            build_toolchain,
                            version,
                            arch,
                            bootstrap_bin_dir=bootstrap_bin_dir,
                            compiler_group_name=compiler_group_name,
                            compiler_group_dir=compiler_group_dir,
                        )
                    ] = arch

            for future in as_completed(futures):
                arch = futures[future]
                try:
                    result  = future.result()
                    print(f"[DEBUG] build_toolchain({version}, {arch}) -> {result}")
                    if not result:
                        failures.append((version, arch))
                except Exception as e:
                    print(f"[ERROR] build_toolchain({version}, {arch}) failed with exception: {e}")
                    failures.append((version, arch))

    if failures:
        logger.error(f"[-] {len(failures)} GCC toolchain(s) failed: {', '.join(f'{version}/{arch}' for version, arch in failures)}")
        return False
    logger.info("[+] All requested GCC toolchains are ready.")
    return True


def main() -> None:
    args = parse_arguments()
    if args.verbose and not args.status:
        logger.error("[-] --verbose requires --status.")
        exit(2)
    if args.parallel_toolchains is not None and args.parallel_toolchains < 1:
        logger.error("[-] --parallel-toolchains must be a positive integer.")
        exit(2)
    if args.parallel_toolchains is not None and not args.build_toolchains:
        logger.error("[-] --parallel-toolchains requires --build-toolchains.")
        exit(2)
    if args.config:
        config_file_path = args.config
    else:
        config_file_path = config.GCC_TOOLCHAIN_TARGETS_YAML

    if not file_exists(config_file_path):
        logger.error(f"[-] No {config_file_path} found.")
        exit(1)

    gcc_config = load_yaml(config_file_path)
    if args.status:
        if args.build_bootstrap or args.build_toolchains:
            logger.error("[-] --status cannot be combined with build options.")
            exit(2)
        exit(0 if show_gcc_status(gcc_config, ARCH_GCC_PREFIX_MAP, args.verbose) else 1)

    if not file_exists(config.CTNG_BIN):
        logger.error(f"[-] No {config.CTNG_BIN} found!")
        logger.error(f"[-] Run 'python scripts/setup_ctng.py'")
        exit(1)

    logger.info(f"[*] Configure file: {config_file_path}")

    bootstrap = gcc_config['bootstrap']
    compiler_group_name = gcc_config['compiler_group_name']
    logger.info(f"[*] compiler group name: {compiler_group_name}")

    bootstrap_version, bootstrap_arch, = bootstrap['version'], bootstrap['arch']
    logger.info(f"[*] bootstrap:")
    logger.info(f"[*]   arch:    {bootstrap_arch}")
    logger.info(f"[*]   version: {bootstrap_version}")

    is_bootstrapper_built = check_toolchain(
        bootstrap_version,
        bootstrap_arch,
        compiler_group_name=compiler_group_name,
        compiler_group_dir=BOOTSTRAP_PATH
    )
    logger.info(f"[*] Bootstrapper build status: {is_bootstrapper_built}")

    if args.build_bootstrap:
        if is_bootstrapper_built:
            logger.info("[*] Bootstrapper has already been built.")
            if not link_bootstrap(bootstrap_version, bootstrap_arch):
                exit(1)
        else:
            logger.info("[*] Build bootstrapper..")
            installed = build_bootstrap(bootstrap_version, bootstrap_arch)
            if installed:
                is_bootstrapper_built = True
                logger.info("[+] Bootstrapper is built successfully.")
            else:
                logger.error("[-] Failed to built bootstrap toolchain")
                exit(1)

    if args.build_toolchains:
        if not is_bootstrapper_built:
            logger.error(f"[-] Build bootstrap first.")
            exit(1)

        ##! Build all toolchains
        logger.info(f"[*] Bootstrap toolchain's info: {bootstrap_version}-{bootstrap_arch}")
        toolchain_targets = gcc_config['toolchain_targets']
        parallel_toolchains = args.parallel_toolchains or DEFAULT_PARALLEL_TOOLCHAINS
        if not build_all_toolchains(compiler_group_name, toolchain_targets, parallel_toolchains):
            exit(1)


if __name__ == "__main__":
    main()

# EOF
