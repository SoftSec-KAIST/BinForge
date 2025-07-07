import sys
import os

from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from binforge import config
from binforge.utils import mkdir, file_exists, execute_command, add_path_env, load_yaml
from binforge.types_helper import *
from binforge.logging_helper import logger


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


def get_ctng_conf_file_path(version: str, arch: str) -> str:
    CTNG_CONF_PATH = config.CTNG_CONF_PATH
    return  os.path.join(CTNG_CONF_PATH, version, arch) + ".conf"


def build_all_toolchains(toolchain_targets):
    GCC_PATH = os.path.join(config.TOOL_PATH, "gcc")

    if file_exists(os.path.join(GCC_PATH, f"bootstrap", "bin", f"{ARCH_GCC_PREFIX_MAP["x86_64"]}-gcc")):
        bootstrap_bin_path = os.path.join(GCC_PATH, f"bootstrap", "bin")
        logger.info(f"[*] Bootstrap toolchain path: {bootstrap_bin_path}")
    else:
        logger.error("Build bootstrap toolchain first.")
        exit()

    for version, archs in toolchain_targets.items():
        print(f"[DEBUG] {version}")

        with ThreadPoolExecutor() as executor:
            futures = {}
            for arch in archs:
                result = check_toolchain(version, arch)
                print(f"[DEBUG] check_toolchain({version}, {arch}) -> {result}")
                if not result:
                    futures[executor.submit(build_toolchain, version, arch, bootstrap_bin_path)] = arch

            for future in as_completed(futures):
                arch = futures[future]
                try:
                    result  = future.result()
                    print(f"[DEBUG] build_toolchain({version}, {arch}) -> {result}")
                except Exception as e:
                    print(f"[ERROR] build_toolchain({version}, {arch}) failed with exception: {e}")

    print(f"[INFO] Completed all tasks.")


def check_toolchain(version: str, arch: str) -> bool:
    TOOL_PATH = config.TOOL_PATH
    GCC_PATH = os.path.join(TOOL_PATH, "gcc")

    gcc_prefix = f"{ARCH_GCC_PREFIX_MAP[arch]}"
    gcc_bin_prefix_path = os.path.join(GCC_PATH, f"{gcc_prefix}-{version}", "bin", f"{gcc_prefix}-gcc")
    result = execute_command(f"{gcc_bin_prefix_path} --version | grep {version}", DEBUG_LEVEL=-1, exit_on_error=False)

    is_toolchain_built = True if result.returncode == 0 else False
    if is_toolchain_built:
        logger.info(f"[+] Toolchain already built ({version}-{arch})")
    else:
        logger.info(f"[-] Toolchain not yet built ({version}-{arch})")

    return is_toolchain_built


def build_toolchain(version: str, arch: str, bootstrap_bin_path: str = "") -> bool:
    PROJECT_PATH = config.PROJECT_PATH
    TOOL_PATH = config.TOOL_PATH
    GCC_PATH = os.path.join(TOOL_PATH, "gcc")
    GCC_BUILD_PATH = os.path.join(GCC_PATH, "build")
    CTNG_BIN = config.CTNG_BIN
    NUM_JOBS = config.NUM_JOBS
    MAX_JOBS = config.MAX_JOBS
    PATCH_GCC = os.path.join(PROJECT_PATH, "patches", "setup_gcc")

    if bootstrap_bin_path:
        # Build toolchain(s) with bootstrap
        logger.info(f"[DEBUG] bootstrap! {bootstrap_bin_path}")
        add_path_env(bootstrap_bin_path)
    else:
        # Build bootstrap
        logger.info(f"[DEBUG] Bootstrap toolchain not found!")

    build_path = os.path.join(GCC_BUILD_PATH, version)
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

    logger.info(f"Build.. ({version}, {arch})")

    command_build = ""
    if version in ("11.4.0"):
        patch_target_file_path = f"{work_dir}/.build/src/gcc-11.4.0/libiberty/simple-object-mach-o.c"
        command_build = f"{CTNG_BIN} -s -j {NUM_JOBS} -l {MAX_JOBS} build STOP=kernel_headers; "
        command_build += f"patch {patch_target_file_path} {PATCH_GCC}/gcc-{version}.patch; "
        command_build += f"{CTNG_BIN} -s -j {NUM_JOBS} -l {MAX_JOBS} build START=cc_core; "
    elif version in (("7.5.0", "8.5.0", "9.5.0")) and arch in (("riscv_32", "riscv_64")):
        command_build = f"{CTNG_BIN} -s -j {NUM_JOBS} -l {MAX_JOBS} build STOP=cc_for_build; "
        patch_target_file_path1 = f"{work_dir}/.build/src/gcc-{version}/libgcc/config/riscv/div.S"
        patch_target_file_path2 = f"{work_dir}/.build/src/gcc-{version}/libgcc/config/riscv/riscv-asm.h"
        command_build += f"cp {PATCH_GCC}/tmp/div.S {patch_target_file_path1}; "
        command_build += f"cp {PATCH_GCC}/tmp/riscv-asm.h {patch_target_file_path2}; "
        command_build += f"{CTNG_BIN} -s -j {NUM_JOBS} -l {MAX_JOBS} build START=cc_for_build; "
    else:
        command_build = f"{CTNG_BIN} -s -j {NUM_JOBS} -l {MAX_JOBS} build"

    ##! Compile toolchain!
    env_vars = {"TOOL_PATH": TOOL_PATH}
    result_build = execute_command(
        command = command_build,
        directory = work_dir,
        DEBUG_LEVEL = 2,
        NO_STREAM = True,
        env_vars = env_vars
    )

    #! Cleanup
    execute_command(f"rm -rf {work_dir}/.build {work_dir}/.config")
    is_gcc_installed = check_toolchain(version, arch)
    return is_gcc_installed


def build_bootstrap(version: str, arch: str) -> bool:
    is_bootstrap_installed = build_toolchain(version, arch)

    ##! Build bootstrap toolchain
    if not is_bootstrap_installed:
        return False

    GCC_PATH = os.path.join(config.TOOL_PATH, "gcc")
    bootstrap_path = os.path.join(GCC_PATH, ARCH_GCC_PREFIX_MAP[arch] + "-" + version)
    execute_command(f"unlink {GCC_PATH}/bootstrap-{version}; ln -s {bootstrap_path} {GCC_PATH}/bootstrap", DEBUG_LEVEL=-1, exit_on_error=False)
    return True


def main() -> None:
    if not file_exists(config.CTNG_BIN):
        logger.error(f"[-] No {config.CTNG_BIN} found!")
        logger.error(f"[-] Run 'python3 scripts/setup_ctng.py'")
        exit()

    gcc_toolchain_targets = load_yaml(config.GCC_TOOLCHAIN_TARGETS_YAML)

    bootstrap = gcc_toolchain_targets['bootstrap']

    bootstrap_version, bootstrap_arch, = bootstrap['version'], bootstrap['arch']
    logger.info(f"[DEBUG] bootstrap")
    logger.info(f"[DEBUG]   - version: {bootstrap_version}")
    logger.info(f"[DEBUG]   - arch: {bootstrap_arch}")

    is_bootstrapper_built = check_toolchain(bootstrap_version, bootstrap_arch)
    if not is_bootstrapper_built:
        installed = build_bootstrap(bootstrap_version, bootstrap_arch)
        if not installed:
            logger.error("[-] Failed to built bootstrap toolchain")
            exit(-1)

    ##! Build all toolchains
    logger.info(f"[*] Bootstrap toolchain's info: {bootstrap_version}-{bootstrap_arch}")
    toolchain_targets = gcc_toolchain_targets['toolchain_targets']
    build_all_toolchains(toolchain_targets)


if __name__ == "__main__":
    main()

# EOF
