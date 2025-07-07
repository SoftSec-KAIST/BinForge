import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


from binforge.utils import *
from binforge import config
from binforge.logging_helper import logger

TOOLCHAIN_VERSION = "13.3.0" # Test "14.2.0"

arch_prefix_list = [
    "i686-unknown-linux-gnu",
    "x86_64-unknown-linux-gnu",
    "arm-unknown-linux-gnueabi",
    "aarch64-unknown-linux-gnu",
    "mipsel-unknown-linux-gnu",
    "mips64el-unknown-linux-gnu",
    "mips-unknown-linux-gnu",
    "mips64-unknown-linux-gnu",
    "powerpc-unknown-linux-gnu",
    "powerpc64le-unknown-linux-gnu",
    "powerpc64-unknown-linux-gnu",
    # "riscv32-unknown-linux-gnu",
    # "riscv64-unknown-linux-gnu",
]

toolchain_list = [
    "addr2line",
    "ar",
    "as",
    "c++",
    "cc",
    "c++filt",
    "cpp",
    # "ct-ng.config",
    "elfedit",
    "g++",
    f"gcc-{TOOLCHAIN_VERSION}",
    "gcc-ar",
    "gcc-nm",
    "gcc-ranlib",
    "gcov",
    "gcov-dump",
    "gcov-tool",
    "gprof",
    "ld",
    "ld.bfd",
    "ldd",
    "nm",
    "objcopy",
    "objdump",
    "populate",
    "ranlib",
    "readelf",
    "size",
    "strings",
    "strip"
]

def link_toolchains() -> None:
    #! for libmvec gcc-v{6, 7, 8, 9}
    no_libmvec_versions = ["8.5.0", "9.5.0"]
    aarch64_arch_prefix = "aarch64-unknown-linux-gnu"
    TOOL_PATH = config.TOOL_PATH
    for version in no_libmvec_versions:
        print(version)
        path1 = os.path.join(TOOL_PATH, "gcc", f"{aarch64_arch_prefix}-{version}", aarch64_arch_prefix, "lib")
        path2 = os.path.join(TOOL_PATH, "gcc", f"{aarch64_arch_prefix}-{version}", aarch64_arch_prefix, "sysroot", "lib")
        path3 = os.path.join(TOOL_PATH, "gcc", f"{aarch64_arch_prefix}-{version}", aarch64_arch_prefix, "sysroot", "usr", "lib")

        path1_ = os.path.join(TOOL_PATH, "gcc", f"{aarch64_arch_prefix}-{TOOLCHAIN_VERSION}", aarch64_arch_prefix, "lib")
        path2_ = os.path.join(TOOL_PATH, "gcc", f"{aarch64_arch_prefix}-{TOOLCHAIN_VERSION}", aarch64_arch_prefix, "sysroot", "lib")
        path3_ = os.path.join(TOOL_PATH, "gcc", f"{aarch64_arch_prefix}-{TOOLCHAIN_VERSION}", aarch64_arch_prefix, "sysroot", "usr", "lib")

        command1_1 = f"sudo ln -sf {path1_}/libmvec.a {path1}/libmvec.a"
        command1_2 = f"sudo ln -sf {path1_}/libmvec.a {path1}/libmvec.so.1"
        command1_3 = f"sudo ln -sf {path1_}/libmvec.a {path1}/libmvec.so"
        command2_1 = f"sudo ln -sf {path2_}/libmvec.so.1 {path2}/libmvec.so.1"
        command3_1 = f"sudo ln -sf {path3_}/libmvec.a {path3}/libmvec.a"
        command3_2 = f"sudo ln -sf {path3_}/libmvec.so {path3}/libmvec.so"

        execute_command(command=command1_1, DEBUG_LEVEL=0)
        execute_command(command=command1_2, DEBUG_LEVEL=-1)
        execute_command(command=command1_3, DEBUG_LEVEL=-1)
        execute_command(command=command2_1, DEBUG_LEVEL=-1)
        execute_command(command=command3_1, DEBUG_LEVEL=-1)
        execute_command(command=command3_2, DEBUG_LEVEL=-1)

    #! link
    for arch_prefix in arch_prefix_list:
        TOOLCHAIN_PATH = os.path.join(TOOL_PATH, "gcc", f"{arch_prefix}-{TOOLCHAIN_VERSION}", "bin", arch_prefix)
        if not file_exists(TOOLCHAIN_PATH + "-gcc"):
            logger.info(f"{arch_prefix}-gcc not found.")
            continue
        logger.info(f"{arch_prefix}-gcc Found.")

        TOOLCHAIN_PATH = os.path.join(TOOL_PATH, "gcc", f"{arch_prefix}-{TOOLCHAIN_VERSION}", "bin", arch_prefix)
        SYSROOT = os.path.join(TOOL_PATH, "gcc", f"{arch_prefix}-{TOOLCHAIN_VERSION}", arch_prefix, "sysroot")

        command = f"sudo update-alternatives --force --install /usr/bin/{arch_prefix}-gcc {arch_prefix}-gcc {TOOLCHAIN_PATH}-gcc 100 "
        for toolchain in toolchain_list:
            command += f"--slave /usr/bin/{arch_prefix}-{toolchain} {arch_prefix}-{toolchain} {TOOLCHAIN_PATH}-{toolchain} "
        execute_command(command=command, DEBUG_LEVEL=0)

        # give write permission to set symbolic link
        command = f"chmod ug+w -R \"{TOOL_PATH}/gcc/{arch_prefix}-{TOOLCHAIN_VERSION}\""
        execute_command(command, DEBUG_LEVEL=0)

        # some compilation environment do not search sysroot.
        command = f"ln -sf ../sysroot/lib/* ./; ln -sf ../sysroot/usr/lib/* ./"
        directory = os.path.join(SYSROOT, "..", "lib")
        execute_command(command=command, directory=directory, DEBUG_LEVEL=0)

        if arch_prefix == "powerpc-unknown-linux-gnu":
            lib32_dir = os.path.join(TOOL_PATH, "gcc", f"{arch_prefix}-{TOOLCHAIN_VERSION}", arch_prefix, "lib32")
            mkdir(lib32_dir)
            execute_command(f"pushd {lib32_dir} > /dev/null; pwd", DEBUG_LEVEL=0)
            execute_command(f"ln -sf ../sysroot/lib/* ./", directory=lib32_dir, DEBUG_LEVEL=0)
            execute_command(f"ln -sf ../sysroot/usr/lib/* ./", directory=lib32_dir, DEBUG_LEVEL=0)
        elif arch_prefix == "powerpc64-unknown-linux-gnu":
            lib32_dir = os.path.join(TOOL_PATH, "gcc", f"{arch_prefix}-{TOOLCHAIN_VERSION}", arch_prefix, "lib64")
            mkdir(lib32_dir)
            execute_command(f"pushd {lib32_dir} > /dev/null; pwd", DEBUG_LEVEL=0)
            execute_command(f"ln -sf ../sysroot/lib/* ./", directory=lib32_dir, DEBUG_LEVEL=0)
            execute_command(f"ln -sf ../sysroot/usr/lib/* ./", directory=lib32_dir, DEBUG_LEVEL=0)
        elif arch_prefix == "powerpc64le-unknown-linux-gnu":
            lib32_dir = os.path.join(TOOL_PATH, "gcc", f"{arch_prefix}-{TOOLCHAIN_VERSION}", arch_prefix, "lib64")
            mkdir(lib32_dir)
            execute_command(f"pushd {lib32_dir} > /dev/null; pwd", DEBUG_LEVEL=0)
            execute_command(f"ln -sf ../sysroot/lib/* ./", directory=lib32_dir, DEBUG_LEVEL=0)
            execute_command(f"ln -sf ../sysroot/usr/lib/* ./", directory=lib32_dir, DEBUG_LEVEL=0)

if __name__ == "__main__":
    link_toolchains()

# EOF
