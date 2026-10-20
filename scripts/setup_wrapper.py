import sys
import os
import argparse

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import re
import shutil
import stat
from enum import Enum, auto

from binforge import config
from binforge.types_helper import *
from binforge.utils import load_yaml
from scripts.setup_gcc import ARCH_GCC_PREFIX_MAP


class Compiler(Enum):
    GCC   = auto()
    CLANG = auto()


def is_shell_script(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    with open(path, "rb") as file:
        return file.read(2) == b"#!"


def is_executable(path: str) -> bool:
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False
    with open(path, "rb") as file:
        return file.read(4) == b"\x7fELF"


def get_compiler_dirs(compiler: Compiler, compiler_dir: str) -> list[str]:
    if not os.path.isdir(compiler_dir):
        return []

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

        if not os.access(bin_dir, os.W_OK):
            raise PermissionError(f"Compiler bin directory is not writable: {bin_dir}")

        # Rename the existing compiler file
        if os.path.lexists(underbar_compiler_path):
            raise RuntimeError(f"Refusing to overwrite an existing underlying compiler: {underbar_compiler_path}")
        print(f"[DEBUG] Renamed: {original_compiler_path} -> {underbar_compiler_path}")
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
            elif line.startswith("CONFTEST_SOURCE="):
                line = f'CONFTEST_SOURCE="{os.path.join(config.PROJECT_PATH, "patches", "conftest", "conftest.c")}"\n'
            new_wrapper_content.append(line)

        with open(wrapper_dest, "w") as file:
            file.writelines(new_wrapper_content)

        print(f"[*] Wrap: {original_compiler_path} -> {underbar_compiler_path}")


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

        if not os.path.lexists(underbar_compiler_path):
            if is_executable(original_compiler_path):
                print(f"[*] Already unwrapped: {original_compiler_path}")
                continue
            raise RuntimeError(f"No usable compiler to restore: {original_compiler_path}")
        if not is_executable(underbar_compiler_path):
            raise RuntimeError(f"Underlying compiler is not an executable binary: {underbar_compiler_path}")
        if os.path.lexists(original_compiler_path):
            if not is_shell_script(original_compiler_path):
                raise RuntimeError(f"Refusing to replace a non-wrapper compiler: {original_compiler_path}")
            with open(original_compiler_path, "rb") as file:
                header = file.read(1024)
            binary_setting = re.search(rb'^COMPILER_BINARY="([^"]+)"$', header, re.MULTILINE)
            if (not binary_setting or
                    os.path.realpath(os.fsdecode(binary_setting.group(1))) != os.path.realpath(underbar_compiler_path)):
                raise RuntimeError(f"Refusing to replace an unrecognized wrapper: {original_compiler_path}")
        print(f"[+] Unwrap: {underbar_compiler_path} -> {original_compiler_path}")
        os.replace(underbar_compiler_path, original_compiler_path)


def get_group_names() -> tuple[list[str], list[str]]:
    return ["gcc"], ["clang"]


def ensure_writable_bins(compiler: Compiler, compiler_dir: str) -> None:
    for toolchain in get_compiler_dirs(compiler, compiler_dir):
        bin_dir = os.path.join(compiler_dir, toolchain, "bin")
        if not os.path.isdir(bin_dir):
            raise FileNotFoundError(f"Compiler bin directory is missing: {bin_dir}")
        if not os.access(bin_dir, os.W_OK):
            info = os.stat(bin_dir)
            if info.st_uid == os.geteuid():
                os.chmod(bin_dir, stat.S_IMODE(info.st_mode) | stat.S_IWUSR)
        if not os.access(bin_dir, os.W_OK):
            raise PermissionError(f"Compiler bin directory is not writable: {bin_dir}")


def wrapper_status(tool_path: str = config.TOOL_PATH, expected_state: str = "wrapped") -> bool:
    if expected_state not in ("wrapped", "unwrapped"):
        raise ValueError(f"Unknown wrapper state: {expected_state}")
    gcc_targets = load_yaml(config.GCC_TOOLCHAIN_TARGETS_YAML)["toolchain_targets"]
    clang_versions = load_yaml(config.CLANG_TOOLCHAIN_TARGETS_YAML)["toolchain_targets"]
    groups = {"GCC": [], "Clang": []}

    for version, archs in gcc_targets.items():
        for arch in archs:
            prefix = ARCH_GCC_PREFIX_MAP[arch]
            bin_dir = os.path.join(tool_path, "gcc", f"{prefix}-{version}", "bin")
            groups["GCC"].append((f"{version}/{arch}", os.path.join(bin_dir, f"{prefix}-gcc")))
    for version in clang_versions:
        groups["Clang"].append((version, os.path.join(tool_path, "clang", f"clang-{version}", "bin", "clang")))

    complete = True
    for group, targets in groups.items():
        problems = []
        wrapped = 0
        unwrapped = 0
        for label, wrapper in targets:
            underlying = os.path.join(os.path.dirname(wrapper), "_" + os.path.basename(wrapper))
            if is_executable(wrapper) and not os.path.lexists(underlying):
                unwrapped += 1
                continue
            if not os.path.isfile(wrapper) or not os.access(wrapper, os.X_OK):
                problems.append(f"{label}: compiler frontend missing")
                continue
            if not is_executable(underlying):
                problems.append(f"{label}: underlying compiler is missing or not an executable binary")
                continue
            with open(wrapper, "rb") as file:
                header = file.read(1024)
            binary_setting = re.search(rb'^COMPILER_BINARY="([^"]+)"$', header, re.MULTILINE)
            if (not header.startswith(b"#!/bin/bash\n") or not binary_setting or
                    os.path.realpath(os.fsdecode(binary_setting.group(1))) != os.path.realpath(underlying)):
                problems.append(f"{label}: wrapper target does not match")
                continue
            elif group == "Clang":
                configured_source = os.path.join(config.PROJECT_PATH, "patches", "conftest", "conftest.c")
                physical_source = os.path.join(os.path.dirname(os.path.realpath(tool_path)), "patches", "conftest", "conftest.c")
                source_setting = re.search(rb'^CONFTEST_SOURCE="([^"]+)"$', header, re.MULTILINE)
                if not source_setting or os.fsdecode(source_setting.group(1)) not in (configured_source, physical_source):
                    problems.append(f"{label}: multiarch source path does not match")
                    continue
            wrapped += 1
        print(f"{group}: {wrapped}/{len(targets)} wrapped, {unwrapped}/{len(targets)} unwrapped")
        for problem in problems:
            print(f"  {problem}")
        count = wrapped if expected_state == "wrapped" else unwrapped
        complete &= not problems and count == len(targets)
    return complete


def main():
    parser = argparse.ArgumentParser(description="Set up compiler wrappers.")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--wrap", action="store_true", help="Install compiler wrappers (default)")
    actions.add_argument("--unwrap", action="store_true", help="Restore original compiler frontends")
    actions.add_argument("--status", action="store_true", help="Show wrapper state without changing files")
    parser.add_argument("--expect-unwrapped", action="store_true", help="With --status, succeed when all compilers are unwrapped")
    args = parser.parse_args()
    if args.expect_unwrapped and not args.status:
        parser.error("--expect-unwrapped requires --status")
    if args.status:
        expected = "unwrapped" if args.expect_unwrapped else "wrapped"
        raise SystemExit(0 if wrapper_status(expected_state=expected) else 1)

    gcc_groups, clang_groups = get_group_names()

    COMPILER_WRAPPER_TEMPLATE_DIR = os.path.join(config.PROJECT_PATH, "patches", "compiler_wrapper_template")
    GCC_WRAPPER_TEMPLATE = os.path.join(COMPILER_WRAPPER_TEMPLATE_DIR, "gcc_wrapper_template")
    CLANG_WRAPPER_TEMPLATE = os.path.join(COMPILER_WRAPPER_TEMPLATE_DIR, "clang_wrapper_template")

    for gcc_group_name in gcc_groups:
        GCC_DIR = os.path.join(config.TOOL_PATH, gcc_group_name)
        ensure_writable_bins(Compiler.GCC, GCC_DIR)
    for clang_group_name in clang_groups:
        CLANG_DIR = os.path.join(config.TOOL_PATH, clang_group_name)
        ensure_writable_bins(Compiler.CLANG, CLANG_DIR)

    for gcc_group_name in gcc_groups:
        GCC_DIR = os.path.join(config.TOOL_PATH, gcc_group_name)
        if not os.path.isdir(GCC_DIR):
            print(f"[*] Skipping {gcc_group_name}: directory not found.")
            continue

        unwrap_compiler(Compiler.GCC, GCC_DIR)
        if not args.unwrap:
            wrap_compiler(Compiler.GCC, GCC_DIR, GCC_WRAPPER_TEMPLATE)

    for clang_group_name in clang_groups:
        CLANG_DIR = os.path.join(config.TOOL_PATH, clang_group_name)
        if not os.path.isdir(CLANG_DIR):
            print(f"[*] Skipping {clang_group_name}: directory not found.")
            continue

        unwrap_compiler(Compiler.CLANG, CLANG_DIR)
        if not args.unwrap:
            wrap_compiler(Compiler.CLANG, CLANG_DIR, CLANG_WRAPPER_TEMPLATE)

    expected = "unwrapped" if args.unwrap else "wrapped"
    if not wrapper_status(expected_state=expected):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

# EOF
