import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from binforge.utils import execute_command
from binforge.types_helper import *
from binforge import config

import getpass
import pwd
import grp
import shutil
import re
import subprocess

from enum import Enum, auto

class Compiler(Enum):
    GCC   = auto()
    CLANG = auto()


def is_shell_script(path: str) -> bool:
    result =  execute_command(f"file {path}", DEBUG_LEVEL=0, NO_STREAM=False, exit_on_error=False)
    return True if "shell script" in result.stdout else False


def is_executable(path: str) -> bool:
    result =  execute_command(f"file {path}", DEBUG_LEVEL=0, NO_STREAM=False, exit_on_error=False)
    return True if "ELF" in result.stdout or "symbolic link to clang-" in result.stdout else False


def get_compiler_dirs(compiler: Compiler, compiler_dir: str) -> list[str]:
    if compiler == Compiler.GCC:
        return sorted([
            d for d in os.listdir(compiler_dir)
            if os.path.isdir(os.path.join(compiler_dir, d)) and "-unknown-linux-gnu" in d
        ])
    else:
        return sorted([
        d for d in os.listdir(compiler_dir)
        if os.path.isdir(os.path.join(compiler_dir, d)) and "clang-" in d
    ])


def get_compiler_name(compiler: Compiler, toolchain_path: str) -> str | NoReturn:
    if compiler == Compiler.GCC:
        # Extract arch_prefix by removing the version suffix (X.X.X)
        match = re.match(r"(.+)-\d+\.\d+\.\d+$", toolchain_path)
        if not match:
            print(f"Invalid toolchain directory: {toolchain_path}")
            exit(1)
        prefix = match.group(1)

        assert isinstance(prefix, str), "prefix is not str"

        return f"{prefix}-gcc"
    else:
        return "clang"


def wrap_compiler(compiler: Compiler, compiler_dir: str, compiler_wrapper_template: str) -> None:
    toolchain_dirs = get_compiler_dirs(compiler, compiler_dir)

    # Process each toolchain directory
    for toolchain in toolchain_dirs:
        toolchain_dir_path = os.path.join(compiler_dir, toolchain)
        # print(f"[DEBUG] toolchain_dir_path: {toolchain_dir_path}")

        # Define paths for the bin directory and compiler executables
        bin_dir = os.path.join(toolchain_dir_path, "bin")
        compiler_name = get_compiler_name(compiler, toolchain)

        original_compiler_path = os.path.join(bin_dir, compiler_name)
        underbar_compiler_path = os.path.join(bin_dir, f"_{compiler_name}")

        if not os.path.exists(original_compiler_path):
            print(f"[-] Skipping {toolchain}, no compiler found: {original_compiler_path}")
            continue

        if is_shell_script(original_compiler_path):
            print("[*] Already wrapped.")
            continue

        # Add write permission to the bin directory
        subprocess.run(["chmod", "u+w", bin_dir], check=False)

        # Add write permission to the compiler file
        subprocess.run(["chmod", "u+w", original_compiler_path], check=True)

        # Rename the existing compiler file
        if not os.path.exists(underbar_compiler_path):
            print(f"[DEBUG] Renamed: {original_compiler_path} → {underbar_compiler_path}")
            os.rename(original_compiler_path, underbar_compiler_path)

        # Copy the wrapper and set execution permissions
        wrapper_dest = original_compiler_path
        shutil.copy(compiler_wrapper_template, wrapper_dest)
        os.chmod(wrapper_dest, 0o755)

        # Modify COMPILER_BINARY in the wrapper script
        with open(wrapper_dest, "r") as file:
            wrapper_content = file.readlines()

        new_wrapper_content = []
        for line in wrapper_content:
            if line.startswith("COMPILER_BINARY="):
                line = f'COMPILER_BINARY="{underbar_compiler_path}"\n'
            new_wrapper_content.append(line)

        with open(wrapper_dest, "w") as file:
            file.writelines(new_wrapper_content)

        print(f"[*] Wrap: {original_compiler_path} → {underbar_compiler_path}")


def unwrap_compiler(compiler: Compiler, compiler_dir: str) -> None:
    toolchain_dirs = get_compiler_dirs(compiler, compiler_dir)

    # Process each toolchain directory
    for toolchain in toolchain_dirs:
        toolchain_dir_path = os.path.join(compiler_dir, toolchain)
        # print(f"[DEBUG] toolchain_dir_path: {toolchain_dir_path}")

        # Define paths for the bin directory and compiler executables
        bin_dir = os.path.join(toolchain_dir_path, "bin")
        compiler_name = get_compiler_name(compiler, toolchain)

        original_compiler_path = os.path.join(bin_dir, compiler_name)
        underbar_compiler_path = os.path.join(bin_dir, f"_{compiler_name}")

        #! Unwrap
        if is_executable(underbar_compiler_path):
            print(f"[+] Unwrap: {underbar_compiler_path} → {original_compiler_path}")
            os.rename(underbar_compiler_path, original_compiler_path)
        else:
            print("[*] Already unwrapped.")


if __name__ == "__main__":
    GCC_DIR = os.path.join(config.TOOL_PATH, "gcc")
    CLANG_DIR = os.path.join(config.TOOL_PATH, "clang")
    COMPILER_WRAPPER_TEMPLATE_DIR = os.path.join(config.PROJECT_PATH, "patches", "compiler_wrapper_template")
    GCC_WRAPPER_TEMPLATE = os.path.join(COMPILER_WRAPPER_TEMPLATE_DIR, "gcc_wrapper_template")
    CLANG_WRAPPER_TEMPLATE = os.path.join(COMPILER_WRAPPER_TEMPLATE_DIR, "clang_wrapper_template")

    username = getpass.getuser()
    user_info = pwd.getpwnam(username)
    gid = user_info.pw_gid
    groupname = grp.getgrgid(gid).gr_name

    cmd_chown = f"sudo chown {username}:{groupname} -R {GCC_DIR}"
    print(f"Executed command: {cmd_chown}")
    # os.system(cmd_chown)

    cmd_chmod_gcc = f"chmod +x {GCC_WRAPPER_TEMPLATE}"
    print(f"Executed command: {cmd_chmod_gcc}")
    os.system(cmd_chmod_gcc)

    cmd_chmod_clang = f"chmod +x {CLANG_WRAPPER_TEMPLATE}"
    print(f"Executed command: {cmd_chmod_clang}")
    os.system(cmd_chmod_clang)

    unwrap_compiler(Compiler.GCC, GCC_DIR)
    unwrap_compiler(Compiler.CLANG, CLANG_DIR)

    wrap_compiler(Compiler.GCC, GCC_DIR, GCC_WRAPPER_TEMPLATE)
    wrap_compiler(Compiler.CLANG, CLANG_DIR, CLANG_WRAPPER_TEMPLATE)


# EOF
