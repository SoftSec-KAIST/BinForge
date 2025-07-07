CMAKE_SYSTEM_PROCESSOR = {
    "x86_32"    : "x86",
    "x86_64"    : "x86_64",
    "arm_32"    : "arm",
    "arm_64"    : "aarch64",
    "mips_32"   : "mips",
    "mips_64"   : "mips64",
    "mipseb_32" : "mips",
    "mipseb_64" : "mips64",
    "ppceb_32"  : "powerpc",
    "ppceb_64"  : "ppc64",
    "ppc_64"    : "ppc64le",
    "riscv_32"  : "riscv32",
    "riscv_64"  : "riscv64"
}

def generate_cmake_toolchain_file(
    toolchain_cmake_file_path: str,
    arch: str,
    compiler: str,
    sysroot: str,
    toolchain_bin_path: str,
    arch_prefix: str,
    system: str,
    common_options: str,
    EXTRA_CFLAGS: str,
    EXTRA_LDFLAGS: str,
    TOOL_PATH: str,
    toolchain_lib_path: str,
    toolchain_version: str
) -> None:
    toolchain_cmake_content = []
    toolchain_cmake_content.append(f"# Set basic info\n")
    toolchain_cmake_content.append(f"set(CMAKE_SYSTEM_NAME Linux)\n")
    toolchain_cmake_content.append(f"set(CMAKE_SYSTEM_PROCESSOR {CMAKE_SYSTEM_PROCESSOR[arch]})\n\n")
    toolchain_cmake_content.append(f"# Set search paths\n")
    toolchain_cmake_content.append(f"set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)\n")
    toolchain_cmake_content.append(f"set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)\n")
    toolchain_cmake_content.append(f"set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)\n")
    toolchain_cmake_content.append(f"set(CMAKE_SYSROOT {sysroot})\n\n")
    if "gcc" in compiler:
        toolchain_cmake_content.append(f"# Set cross-compiler\n")
        toolchain_cmake_content.append(f"set(CMAKE_C_COMPILER {toolchain_bin_path}/{arch_prefix}-gcc)\n")
        toolchain_cmake_content.append(f"set(CMAKE_CXX_COMPILER {toolchain_bin_path}/{arch_prefix}-g++)\n")
        toolchain_cmake_content.append(f"set(CMAKE_AR {toolchain_bin_path}/{arch_prefix}-gcc-ar)\n")
        toolchain_cmake_content.append(f"set(CMAKE_RANLIB {toolchain_bin_path}/{arch_prefix}-gcc-ranlib)\n")
        toolchain_cmake_content.append(f"set(CMAKE_NM {toolchain_bin_path}/{arch_prefix}-gcc-nm)\n\n")
        toolchain_cmake_content.append(f"# Set CFLAGS and LDFLAGS\n")
        toolchain_cmake_content.append(f"set(CMAKE_C_FLAGS \"-isysroot {sysroot} -I{system} {common_options} {EXTRA_CFLAGS}\")\n")
        toolchain_cmake_content.append(f"set(CMAKE_CXX_FLAGS \"-isysroot {sysroot} -I{system} {common_options} {EXTRA_CFLAGS}\")\n")
        toolchain_cmake_content.append(f"set(CMAKE_EXE_LINKER_FLAGS \"-L{toolchain_lib_path} {common_options} {EXTRA_LDFLAGS}\")\n")
    elif "clang" in compiler:
        toolchain_cmake_content.append(f"# Set cross-compiler\n")
        toolchain_cmake_content.append(f"set(CMAKE_C_COMPILER clang)\n")
        toolchain_cmake_content.append(f"set(CMAKE_CXX_COMPILER clang++)\n")
        toolchain_cmake_content.append(f"set(CMAKE_AR llvm-ar)\n")
        toolchain_cmake_content.append(f"set(CMAKE_RANLIB llvm-ranlib)\n")
        toolchain_cmake_content.append(f"set(CMAKE_NM llvm-nm)\n\n")
        toolchain_cmake_content.append(f"# Specify target architecture and sysroot\n")
        toolchain_cmake_content.append(f"set(CMAKE_C_COMPILER_TARGET {arch_prefix})\n")
        toolchain_cmake_content.append(f"set(CMAKE_CXX_COMPILER_TARGET {arch_prefix})\n\n")
        toolchain_cmake_content.append(f"# Specify GCC toolchain for compatibility\n")
        toolchain_cmake_content.append(f"set(CMAKE_C_COMPILER_EXTERNAL_TOOLCHAIN {TOOL_PATH}/gcc/{arch_prefix}-{toolchain_version})\n")
        toolchain_cmake_content.append(f"set(CMAKE_CXX_COMPILER_EXTERNAL_TOOLCHAIN {TOOL_PATH}/gcc/{arch_prefix}-{toolchain_version})\n\n")
        toolchain_cmake_content.append(f"# Set CFLAGS and LDFLAGS\n")
        toolchain_cmake_content.append(f"set(CMAKE_C_FLAGS \"--target={arch_prefix} --sysroot={sysroot} -isystem {system} -I{system} {common_options} {EXTRA_CFLAGS}\")\n")
        toolchain_cmake_content.append(f"set(CMAKE_CXX_FLAGS \"--target={arch_prefix} --sysroot={sysroot} -isystem {system} -I{system} {common_options} {EXTRA_CFLAGS}\")\n")
        toolchain_cmake_content.append(f"set(CMAKE_EXE_LINKER_FLAGS \"--target={arch_prefix} --sysroot={sysroot} -L{toolchain_lib_path} {common_options} {EXTRA_LDFLAGS}\")\n")

    with open(toolchain_cmake_file_path, 'w') as file:
        file.writelines(toolchain_cmake_content)
    pass

# EOF
