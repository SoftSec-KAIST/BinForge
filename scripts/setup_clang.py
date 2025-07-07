import sys
import os

from setup_gcc import check_toolchain

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from binforge import config
from binforge.utils import CommandResult, mkdir, execute_command, load_yaml
from binforge.types_helper import *
from binforge.logging_helper import logger


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
        execute_command(
            command=apply_command,
            directory=llvm_project_root,
            DEBUG_LEVEL=DEBUG_LEVEL,
        )
    else:
        logger.info("[*] Already patched")


def clone_llvm_project(
    llvm_project_root: str,
    repo_url: str
) -> CommandResult | None:
    # Check if the directory exists and is non-empty
    if not os.path.isdir(llvm_project_root) or not os.listdir(llvm_project_root):
        logger.info("Cloning llvm-project...")
        #! Since output of git clone is stderr
        command_clone = f"git clone {repo_url} {llvm_project_root} 2>&1"
        execute_command(command_clone, exit_on_error=True, DEBUG_LEVEL=-1)
    else:
        logger.info(f"Directory '{llvm_project_root}' already exists and is not empty.")


def find_bootstrap_dir(target_path:str) -> str | None:
    if not os.path.exists(target_path):
        print(f"The path '{target_path}' does not exist.")
        return None

    if not os.path.isdir(target_path):
        print(f"The path '{target_path}' is not a directory.")
        return None

    for item in os.listdir(target_path):
        item_path = os.path.join(target_path, item)
        if os.path.isdir(item_path) and item.startswith('bootstrap'):
            return item_path

    return None


def main() -> None:
    TOOL_PATH = config.TOOL_PATH

    GCC_PATH = os.path.join(TOOL_PATH, "gcc")
    bootstrap_dir_path = find_bootstrap_dir(GCC_PATH)
    if bootstrap_dir_path == None:
        logger.error("Build bootstrap toolchain first.")
        exit()

    bootstrap_bin_path = os.path.join(bootstrap_dir_path, "bin")
    logger.info(f"[DEBUG] Bootstrap toolchain path: {bootstrap_bin_path}")
    env_vars = {"PATH": bootstrap_bin_path}

    PROJECT_PATH = config.PROJECT_PATH
    CLANG_ROOT_PATH = os.path.join(TOOL_PATH, "clang")

    LLVM_PROJECT_ROOT = os.path.join(TOOL_PATH, "llvm-project")
    LOG_PATH = os.path.join(CLANG_ROOT_PATH, "logs")
    PATCH_CLANG= os.path.join(PROJECT_PATH, "patches", "setup_clang")

    NUM_JOBS = config.NUM_JOBS
    MAX_JOBS = config.MAX_JOBS

    mkdir(CLANG_ROOT_PATH)
    mkdir(LOG_PATH)

    LLVM_REPO_URL = "https://github.com/llvm/llvm-project.git"
    clone_llvm_project(LLVM_PROJECT_ROOT, LLVM_REPO_URL)
    all_clang_installed = True
    target_versions = load_yaml(config.CLANG_TOOLCHAIN_TARGETS_YAML)

    for version in target_versions["toolchain_targets"]:
        git_tag = f"llvmorg-{version}"
        CLANG_PATH= os.path.join(CLANG_ROOT_PATH, f"clang-{version}")

        clang_bin_path = os.path.join(CLANG_PATH, "bin", "clang")
        command = f"{clang_bin_path} --version | grep {version}"
        result_check = execute_command(command, DEBUG_LEVEL=-1, exit_on_error=False)
        if result_check.returncode == 0:
            logger.info(f"clang-{version} already built.")
            continue

        gcc_toolchain_targets = load_yaml(config.GCC_TOOLCHAIN_TARGETS_YAML)

        bootstrap = gcc_toolchain_targets['bootstrap']

        bootstrap_version, bootstrap_arch, = bootstrap['version'], bootstrap['arch']
        logger.info(f"[DEBUG] bootstrap")
        logger.info(f"[DEBUG]   - version: {bootstrap_version}")
        logger.info(f"[DEBUG]   - arch: {bootstrap_arch}")

        is_bootstrapper_built = check_toolchain(bootstrap_version, bootstrap_arch)
        if not is_bootstrapper_built:
            logger.error("[-] Build bootstrap first.")
            logger.error("[-] python3 setup_gcc.py")
            exit(-1)

        GCC_PATH = os.path.join(config.TOOL_PATH, "gcc")
        bootstrap_bin_path = os.path.join(GCC_PATH, f"bootstrap", "bin")
        bootstrap_gcc_bin_path = os.path.join(bootstrap_bin_path, "x86_64-unknown-linux-gnu-gcc")
        bootstrap_gpp_bin_path = os.path.join(bootstrap_bin_path, "x86_64-unknown-linux-gnu-g++")

        command_git_checkout = f"git reset --hard && git clean -fd && git checkout {git_tag} 2>&1"
        execute_command(command_git_checkout, directory=LLVM_PROJECT_ROOT, DEBUG_LEVEL=-1)

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
                all_clang_installed &= (True if result_install.returncode == 0 else False)

    if all_clang_installed:
        logger.info("All CLANGs are set up.")
    else:
        logger.error("Some CLANGs failed to set up.")


if __name__ == "__main__":
    main()

# EOF