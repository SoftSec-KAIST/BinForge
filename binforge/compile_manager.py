import os
import re
import inspect
import shutil
import shlex
import time
import threading
from hashlib import md5
from dataclasses import dataclass

from binforge import config
from binforge.types_helper import *
from binforge.logging_helper import logger
from binforge.models import Project
from binforge.utils import (
    add_path_env,
    execute_command,
    file_exists,
    find_files,
    mkdir,
    set_env_variable,
)
from binforge.cmake_manager import generate_cmake_toolchain_file
from binforge.build_monitor_state import update_variant
from binforge.b2r2_validator import validate_binary_paths

_COMPILE_STATE = threading.local()


def remove_exact_flag(value: str | None, flag: str) -> str:
    """Remove a whitespace-delimited compiler or linker flag."""
    if not value:
        return ""
    return re.sub(rf"(?<!\S){re.escape(flag)}(?!\S)", "", value)


def _state():
    if not hasattr(_COMPILE_STATE, "extra_cflags"):
        _COMPILE_STATE.extra_cflags = ""
        _COMPILE_STATE.extra_ldflags = ""
        _COMPILE_STATE.extra_conf_options = ""
        _COMPILE_STATE.extra_dep_pkg_config_path = ""
    return _COMPILE_STATE


def get_cmake_source_arg(project: Project, work_dir_path: str, build_path: str) -> str:
    if project.cmake_file_path:
        source_code_root = os.path.join(project.source_path, project.name)
        cmake_source_dir = os.path.dirname(project.cmake_file_path)
        cmake_source_rel = os.path.relpath(cmake_source_dir, source_code_root)
        work_cmake_source_dir = os.path.normpath(os.path.join(work_dir_path, cmake_source_rel))
        return os.path.relpath(work_cmake_source_dir, build_path)

    return ".."

@dataclass
class ArchInfo:
    arch_prefix: str
    arch_type: str
    elf_type: str
    arch_option: str

PREDEFIEND_ARCH_INFO = {
    "x86_32": ArchInfo(
        arch_prefix="i686-unknown-linux-gnu",
        elf_type="ELF 32-bit LSB",
        arch_type="Intel 80386",
        arch_option="-m32"
    ),
    "x86_64": ArchInfo(
        arch_prefix="x86_64-unknown-linux-gnu",
        elf_type="ELF 64-bit LSB",
        arch_type="x86-64",
        arch_option = ""
    ),
    "arm_32": ArchInfo(
        arch_prefix="arm-unknown-linux-gnueabi",
        elf_type="ELF 32-bit LSB",
        arch_type="ARM, EABI5",
        arch_option = ""
    ),
    "arm_64": ArchInfo(
        arch_prefix="aarch64-unknown-linux-gnu",
        elf_type="ELF 64-bit LSB",
        arch_type="ARM aarch64",
        arch_option = ""
    ),
    "mips_32": ArchInfo(
        arch_prefix="mipsel-unknown-linux-gnu",
        elf_type="ELF 32-bit LSB",
        arch_type="MIPS, MIPS", # 'MIPS, MIPS32' or 'MIPS, MIPS-I'
        arch_option="-mips32r2"
    ),
    "mips_64": ArchInfo(
        arch_prefix="mips64el-unknown-linux-gnu",
        elf_type="ELF 64-bit LSB",
        arch_type="MIPS, MIPS", # 'MIPS, MIPS64' or 'MIPS, MIPS-III'
        arch_option="-mips64r2"
    ),
    "mipseb_32": ArchInfo(
        arch_prefix="mips-unknown-linux-gnu",
        elf_type="ELF 32-bit MSB",
        arch_type="MIPS, MIPS",  # 'MIPS, MIPS32' or 'MIPS, MIPS-I'
        arch_option="-mips32r2 -EB"
    ),
    "mipseb_64": ArchInfo(
        arch_prefix="mips64-unknown-linux-gnu",
        elf_type="ELF 64-bit MSB",
        arch_type="MIPS, MIPS", # 'MIPS, MIPS64' or 'MIPS, MIPS-III'
        arch_option="-mips64r2 -EB"
    ),
    "ppceb_32": ArchInfo(
        arch_prefix="powerpc-unknown-linux-gnu",
        elf_type="ELF 32-bit MSB",
        arch_type="PowerPC or cisco 4500",
        arch_option = ""
    ),
    "ppceb_64": ArchInfo(
        arch_prefix="powerpc64-unknown-linux-gnu",
        elf_type="ELF 64-bit MSB",
        arch_type="64-bit PowerPC or cisco 7500",
        arch_option = "-mabi=elfv2"
    ),
    "ppc_64": ArchInfo(
        arch_prefix="powerpc64le-unknown-linux-gnu",
        elf_type="ELF 64-bit LSB",
        arch_type="64-bit PowerPC or cisco 7500",
        arch_option = ""
    ),
    "riscv_32": ArchInfo(
        arch_prefix="riscv32-unknown-linux-gnu",
        elf_type="ELF 32-bit LSB",
        arch_type="UCB RISC-V",
        arch_option = ""
    ),
    "riscv_64": ArchInfo(
        arch_prefix="riscv64-unknown-linux-gnu",
        elf_type="ELF 64-bit LSB",
        arch_type="UCB RISC-V",
        arch_option = ""
    ),
}



def path_distance(path: str) -> int:
    return path.count('/')


def calculate_hash(filepath: str) -> str | None:
    try:
        with open(filepath, "rb") as f:
            return md5(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def remove_dup(binaries: list[str], path: str) -> list[str]:
    unique_binaries = {}
    for binary in binaries:
        binary = os.path.join(path, binary)
        name = os.path.basename(binary)
        distance = path_distance(binary)
        file_hash = calculate_hash(binary)

        if file_hash is None:
            continue

        if name not in unique_binaries:
            unique_binaries[name] = (binary, distance, file_hash)
        else:
            current_binary, current_distance, current_hash = unique_binaries[name]
            if file_hash == current_hash:
                if distance < current_distance or (distance == current_distance and binary < current_binary):
                    unique_binaries[name] = (binary, distance, file_hash)
            else:
                unique_binaries[f"{name}_{file_hash}"] = (binary, distance, file_hash)

    result = sorted([binary[0] for binary in unique_binaries.values()])
    return result


def add_dep(
    project: Project,
    dep_name: str,
    lib_name: str | list[str] | None = None,
    arch: str = ""
) -> None:
    state = _state()

    # caller_locals = inspect.currentframe().f_back.f_locals # get callers local variables
    cur_frame = inspect.currentframe()
    assert cur_frame is not None, "cur_frame is None"
    frame_back = cur_frame.f_back
    assert frame_back is not None, "frame_back is None"
    caller_locals = frame_back.f_locals # get callers local variables

    if arch == "" and 'arch' in caller_locals:
        arch = caller_locals['arch']

    # extra_dep_path = config.EXTRA_DEP_PATH
    # EXTRADEP_PATH = os.path.join(extra_dep_path, "install", arch, dep_name)
    if project.dep_path is None:
        return None

    EXTRADEP_PATH = os.path.join(project.dep_path, "install", arch, dep_name)
    include_path = os.path.join(EXTRADEP_PATH, "include")

    # elif dep_name == "ncurses":
    #     state.extra_cflags = f"{state.extra_cflags} -I{EXTRADEP_PATH}/include"
    #     state.extra_ldflags = f"{state.extra_ldflags} -L{EXTRADEP_PATH}/lib -lncurses -lcurses -lncursesw"
    #     return None

    if os.path.isdir(include_path):
        state.extra_cflags = f"{state.extra_cflags} -I{include_path}"

    lib_dir_candidates = ("lib64", "lib") if arch.endswith("_64") else ("lib", "lib64")
    seen_pkgconfig_paths = set()
    for lib_dir in lib_dir_candidates:
        lib_path = os.path.join(EXTRADEP_PATH, lib_dir)
        if os.path.isdir(lib_path):
            state.extra_ldflags = f"{state.extra_ldflags} -L{lib_path} -Wl,-rpath-link,{lib_path}"

        pkgconfig_path = os.path.join(lib_path, "pkgconfig")
        if os.path.isdir(pkgconfig_path) and pkgconfig_path not in seen_pkgconfig_paths:
            state.extra_dep_pkg_config_path = f"{state.extra_dep_pkg_config_path}:{pkgconfig_path}"
            seen_pkgconfig_paths.add(pkgconfig_path)

    if lib_name is None:
        pass
    elif isinstance(lib_name, str):
        state.extra_ldflags = f"{state.extra_ldflags} -l{lib_name}"
    elif isinstance(lib_name, list) and all(isinstance(lib, str) for lib in lib_name):
        for lib in lib_name:
            state.extra_ldflags = f"{state.extra_ldflags} -l{lib}"
    else:
        raise TypeError("[-] add_dep()")
    return


def resolve_dep(project: Project, arch: str, compiler: str, optmz: str) -> None:
    ##! Add dep automatically
    dep_list = project.dep_list
    if dep_list is not None:
        for dep in dep_list or []:
            # print(f"[DEBUG] {project.name} add dep ({dep})")
            add_dep(project, dep)


AUTOMAKE_CLOCK_SANITY_ERROR = "newly created file is older than distributed files"


def normalize_future_timestamps(root_path: str, settle_seconds: int = 2) -> float:
    """Move a copied source tree into the past without changing mtime order.

    Archive extraction and host clock differences can leave source files newer
    than files created by configure. Applying one offset to every path preserves
    relationships such as Makefile.am being newer than Makefile.in.
    """
    paths_and_stats = []
    try:
        paths_and_stats.append((root_path, os.stat(root_path, follow_symlinks=False)))
    except FileNotFoundError:
        return 0.0

    for dirpath, dirnames, filenames in os.walk(root_path):
        for name in (*filenames, *dirnames):
            path = os.path.join(dirpath, name)
            try:
                paths_and_stats.append((path, os.stat(path, follow_symlinks=False)))
            except FileNotFoundError:
                continue

    latest_mtime = max(st.st_mtime for _, st in paths_and_stats)
    target_latest_mtime = max(time.time() - settle_seconds, 0)
    offset = latest_mtime - target_latest_mtime
    if offset <= 0:
        return 0.0

    # Write directory timestamps last because traversing their children may
    # otherwise change them again on some filesystems.
    for path, st in reversed(paths_and_stats):
        os.utime(
            path,
            (st.st_atime, max(st.st_mtime - offset, 0)),
            follow_symlinks=False,
        )
    return offset


def has_automake_clock_sanity_error(log_path: str) -> bool:
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as log_file:
            return AUTOMAKE_CLOCK_SANITY_ERROR in log_file.read()
    except FileNotFoundError:
        return False


def wait_for_automake_clock_retry(
    configure_path: str,
    minimum_delay: float = 2.0,
    maximum_delay: float = 30.0,
) -> float:
    """Wait until a new probe file can be newer than configure, with a cap."""
    delay = minimum_delay
    try:
        configure_mtime = os.stat(configure_path).st_mtime
        delay = max(delay, configure_mtime - time.time() + 1.0)
    except FileNotFoundError:
        pass

    delay = min(delay, maximum_delay)
    time.sleep(delay)
    return delay


def run_configure_with_clock_retry(
    command: str,
    work_dir_path: str,
    log_path: str,
    configure_path: str,
):
    """Run configure and retry only its exact Automake clock sanity failure."""
    result = execute_command(
        command,
        directory=work_dir_path,
        exit_on_error=False,
        DEBUG_LEVEL=0,
        NO_STREAM=False,
    )
    if result.returncode == 0 or not has_automake_clock_sanity_error(log_path):
        return result

    first_attempt_log = f"{os.path.splitext(log_path)[0]}.clock-sanity-attempt1.log"
    shutil.copy2(log_path, first_attempt_log)
    retry_delay = wait_for_automake_clock_retry(configure_path)
    logger.warning(
        "Automake clock sanity check failed; retrying configure once "
        f"after {retry_delay:.1f} seconds"
    )
    return execute_command(
        command,
        directory=work_dir_path,
        exit_on_error=False,
        DEBUG_LEVEL=0,
        NO_STREAM=False,
    )


def run_make_with_clock_retry(
    command: str,
    work_dir_path: str,
    log_path: str,
    error_path: str,
    configure_path: str,
):
    """Run make and retry only an exact Automake clock sanity failure."""
    result = execute_command(
        command,
        directory=work_dir_path,
        exit_on_error=False,
        DEBUG_LEVEL=0,
        NO_STREAM=False,
    )
    if result.returncode == 0 or not any(
        has_automake_clock_sanity_error(path) for path in (log_path, error_path)
    ):
        return result

    for path in (log_path, error_path):
        if os.path.isfile(path):
            stem, extension = os.path.splitext(path)
            shutil.copy2(path, f"{stem}.clock-sanity-attempt1{extension}")

    retry_delay = wait_for_automake_clock_retry(configure_path)
    logger.warning(
        "Automake clock sanity check failed during make; retrying make once "
        f"after {retry_delay:.1f} seconds"
    )
    return execute_command(
        command,
        directory=work_dir_path,
        exit_on_error=False,
        DEBUG_LEVEL=0,
        NO_STREAM=False,
    )


def _compile_project_impl(
    project: Project,
    comb: tuple[str, str, str],
    PATH: str,
    EXTRA_DEP: bool = False,
    cflags: str = '',
    ldflags: str = '',
    conf_options: str = '',
    extra_compiler_options: dict[str, bool] = {},
    monitor_run_dir: str | None = None,
) -> bool:
    state = _state()
    state.extra_cflags = cflags
    state.extra_ldflags = ldflags
    state.extra_conf_options = conf_options
    state.extra_dep_pkg_config_path = ""

    ##! return to original PATH
    set_env_variable("PATH", PATH)
    compiler, arch, optmz = comb
    def mark_phase(phase: str) -> None:
        if monitor_run_dir:
            _record_variant(monitor_run_dir, project.name, comb, state="running", phase=phase)

    project_name = project.name
    name = project.name
    source_path = project.source_path
    output_bins_path = project.bins_path
    log_dir_path = project.log_path
    TOOL_PATH, NUM_JOBS, MAX_JOBS = config.TOOL_PATH, config.NUM_JOBS, config.MAX_JOBS
    compiletype = f"{compiler}_{arch}_{optmz}"
    variant_tag = getattr(project, "variant_tag", "").strip()
    if variant_tag:
        compiletype = f"{compiletype}_{variant_tag}"

    gcc_group_name = "gcc"
    clang_group_name = "clang"
    gcc_base_toolchain_name = "gcc"

    logger.info(f"[DEBUG] {name}: {compiletype}")
    log_prefix = f"{name}_{compiletype}"
    work_dir_name = f"{name}_{compiletype}"
    work_dir_path = os.path.join(source_path, work_dir_name)

    if not (project.use_configure and project.use_cmake):
        # logger.info("Valid: exactly one is True")
        pass
    else:
        logger.error("Error: exactly one of use_configure/use_cmake must be True")
        return False

    arch_info = PREDEFIEND_ARCH_INFO[arch]
    arch_prefix = arch_info.arch_prefix

    ##! ========================================================================

    toolchain_version = "13.3.0"

    if "gcc" in compiler:
        toolchain_version = compiler.replace("gcc-", '')

    toolchain_bin_path = ""
    if "gcc" in compiler:
        compiler_version_ = compiler.replace("gcc-", '') # toolchain_version
        toolchain_bin_path = os.path.join(TOOL_PATH, gcc_group_name, f"{arch_prefix}-{compiler_version_}", "bin")
        add_path_env(toolchain_bin_path)
    elif "clang" in compiler:
        toolchain_bin_path = os.path.join(TOOL_PATH, gcc_base_toolchain_name, f"{arch_prefix}-{toolchain_version}", "bin")
        add_path_env(toolchain_bin_path)

        clang_path = os.path.join(TOOL_PATH, clang_group_name, compiler, "bin")
        add_path_env(clang_path)

    toolchain_lib_path_for_clang = os.path.join(TOOL_PATH, gcc_base_toolchain_name, f"{arch_prefix}-{toolchain_version}", arch_prefix, "lib")
    sysroot_for_clang = os.path.join(TOOL_PATH, gcc_base_toolchain_name, f"{arch_prefix}-{toolchain_version}", arch_prefix, "sysroot")
    system_for_clang = os.path.join(TOOL_PATH, gcc_base_toolchain_name, f"{arch_prefix}-{toolchain_version}", arch_prefix, "sysroot", "usr", "include")

    toolchain_lib_path_for_gcc = os.path.join(TOOL_PATH, gcc_group_name, f"{arch_prefix}-{toolchain_version}", arch_prefix, "lib")
    sysroot_for_gcc = os.path.join(TOOL_PATH, gcc_group_name, f"{arch_prefix}-{toolchain_version}", arch_prefix, "sysroot")
    system_for_gcc = os.path.join(TOOL_PATH, gcc_group_name, f"{arch_prefix}-{toolchain_version}", arch_prefix, "sysroot", "usr", "include")

    #! Make work directory
    execute_command(f"rm -rf {source_path}/{work_dir_name}", DEBUG_LEVEL=0, NO_STREAM=True)
    execute_command(f"cp -R --preserve=all {source_path}/{name} {source_path}/{work_dir_name}", DEBUG_LEVEL=0, NO_STREAM=True)
    normalize_future_timestamps(work_dir_path)

    ##! ========================================================================

    result_find_configure = find_files("configure", work_dir_path)
    if result_find_configure:
        configure_file_path = result_find_configure[0]
        project.configure_file_path = configure_file_path or ""
    else:
        configure_file_path = ""

    ##! ========================================================================

    # print(f"[DEBUG] project.invoke_autoreconf: {project.invoke_autoreconf}")
    # print(f"[DEBUG] project.configure_file_path: {project.configure_file_path}")
    if project.invoke_autoreconf:
        mark_phase("autoreconf")
        # execute_command(f"autoreconf -fvi; make distclean", directory=work_dir_path, DEBUG_LEVEL=2, NO_STREAM=True, exit_on_error=False)
        execute_command(f"autoreconf -fvi 2>&1", directory=work_dir_path, DEBUG_LEVEL=0, NO_STREAM=False, exit_on_error=False)

        result_find_configure = find_files("configure", work_dir_path)
        if result_find_configure:
            configure_file_path = result_find_configure[0] or ""
            project.configure_file_path = configure_file_path
        else:
            logger.error(f"configure script does not generated!")

    ##! ========================================================================

    if not project.cflags: project.cflags = ''
    if not project.ldflags: project.ldflags = ''

    flag = "-Wno-deprecated-ofast"
    if flag in project.cflags:
        if optmz == "Ofast" and compiler.startswith("clang-19."):
            pass
        else:
            project.cflags = remove_exact_flag(project.cflags, flag)

    flag = "-lmvec"
    if flag in project.ldflags:
        if arch == "arm_64":
            pass
        else:
            project.ldflags = remove_exact_flag(project.ldflags, flag)

    resolve_dep(project, arch, compiler, optmz)
    state.extra_conf_options = f"{state.extra_conf_options} {project.conf_options}"
    state.extra_cflags = f"{state.extra_cflags} {project.cflags}"
    state.extra_ldflags = f"{state.extra_ldflags} {project.ldflags}"

    state.extra_cflags = f"{state.extra_cflags} -g"
    # if not project.use_cmake:
    #     EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --enable-shared"
    common_options = f"{arch_info.arch_option} -{optmz}"

    #! ==================== Extra options ====================

    PIE = extra_compiler_options.get("pie", False)
    NO_INLINE = extra_compiler_options.get("no_inline", False)
    STACK_PROTECTOR = extra_compiler_options.get("stack_protector", False)

    ##! Note: PIE is default option for modern compilers, but crosstool-ng's
    ##! default option is no-PIE.
    ##! default no-PIE: gcc v6-v14, clang v6-v14
    ##! default PIE: clang v15-v19
    if not PIE:
        if compiler in (("clang-15.0.7", "clang-16.0.6", "clang-17.0.6", "clang-18.1.8", "clang-19.1.7")):
            #! Pay attention to the order -fPIC must come after.
            state.extra_cflags = f"{state.extra_cflags} -fno-PIE -fPIC"
            state.extra_ldflags = f"{state.extra_ldflags} -no-pie"
    else:
        state.extra_cflags = f"{state.extra_cflags} -fPIE -fPIC"
        state.extra_ldflags = f"{state.extra_ldflags} -pie" # Ordering -pie -> -shared

    if NO_INLINE:
        state.extra_cflags = f"{state.extra_cflags} -fno-inline"

    if STACK_PROTECTOR:
        state.extra_cflags = f"{state.extra_cflags} -fstack-protector-all"

    LTO = False
    if LTO:
        ##! Test LTO
        state.extra_ldflags = f"{state.extra_ldflags} -flto"
        state.extra_cflags = f"{state.extra_cflags} -flto"
        if "clang" in compiler:
            state.extra_ldflags = f"{state.extra_ldflags} -fuse-ld=lld"

    conf_options = f"--host=\"{arch_prefix}\""

    if "gcc" in compiler:
        conf_options = f"{conf_options} CC=\"{arch_prefix}-gcc\"" ##! for project 'ed'
        conf_options = f"{conf_options} CXX=\"{arch_prefix}-g++\"" ##! for project 'ddrescue'

        conf_options = f"{conf_options} CFLAGS=\"  -isysroot {sysroot_for_gcc} -I{system_for_gcc} {common_options} {state.extra_cflags}\"" # '-isystem {system} -I{system}' -> '-I{system}'
        conf_options = f"{conf_options} CXXFLAGS=\"-isysroot {sysroot_for_gcc} -I{system_for_gcc} {common_options} {state.extra_cflags}\"" # '-isystem {system} -I{system}' -> '-I{system}'

        conf_options = f"{conf_options} LDFLAGS=\"{common_options} -L{toolchain_lib_path_for_gcc} {state.extra_ldflags}\""
        conf_options = f"{conf_options} AR=\"{arch_prefix}-gcc-ar\""
        conf_options = f"{conf_options} RANLIB=\"{arch_prefix}-gcc-ranlib\""
        conf_options = f"{conf_options} NM=\"{arch_prefix}-gcc-nm\""
        conf_options = f"{conf_options} {state.extra_conf_options}"
    elif "clang" in compiler:
        ##! So far, the only projects that need to be explicitly passed to
        ##! `CC` are "help2man" and "gmp," but there have been no issues
        ##! even when passing it to all projects.

        #! Set CC
        conf_options = f"{conf_options} CC=\"clang --target={arch_prefix} --gcc-toolchain={TOOL_PATH}/{gcc_base_toolchain_name}/{arch_prefix}-{toolchain_version}"
        if arch == "ppceb_64": conf_options = f"{conf_options} {arch_info.arch_option}"
        conf_options = f"{conf_options}  \""

        #! Set CXX
        conf_options = f"{conf_options} CXX=\"clang++ --target={arch_prefix} --gcc-toolchain={TOOL_PATH}/{gcc_base_toolchain_name}/{arch_prefix}-{toolchain_version}"
        if arch == "ppceb_64": conf_options = f"{conf_options} {arch_info.arch_option}"
        conf_options = f"{conf_options}  \""

        #! '-isystem' for libxcrypt clang
        conf_options = f"{conf_options} CFLAGS=\"  -isysroot {sysroot_for_clang} -isystem {system_for_clang} -I{system_for_clang} {common_options} {state.extra_cflags}\""
        conf_options = f"{conf_options} CXXFLAGS=\"-isysroot {sysroot_for_clang} -isystem {system_for_clang} -I{system_for_clang} {common_options} {state.extra_cflags}\""

        conf_options = f"{conf_options} LDFLAGS=\"{common_options} -L{toolchain_lib_path_for_clang} {state.extra_ldflags}\""
        conf_options = f"{conf_options} AR=\"llvm-ar\""
        conf_options = f"{conf_options} RANLIB=\"llvm-ranlib\""
        conf_options = f"{conf_options} NM=\"llvm-nm\""
        conf_options = f"{conf_options} {state.extra_conf_options}"

    install_prefix = project.install_prefix if project.install_prefix else os.path.join(work_dir_path, "bb_install")

    if state.extra_dep_pkg_config_path:
        pkg_conf = f"PKG_CONFIG_PATH={state.extra_dep_pkg_config_path}:$PKG_CONFIG_PATH"
        command_conf = f"{pkg_conf} ./configure --prefix=\"{install_prefix}\" --build=x86_64-linux-gnu {conf_options}"
    else:
        command_conf = f"./configure --prefix=\"{install_prefix}\" --build=x86_64-linux-gnu {conf_options}"

    command_make = f"make -j {NUM_JOBS} -l {MAX_JOBS} --output-sync=target" # V=1 --debug=a

    # command_make = f"make V=1" # -d --debug=a
    command_install = "fakeroot make install"

    # command_conf = f"{command_conf} >{log_dir_path}/{log_prefix}_configure.log 2>{log_dir_path}/{log_prefix}_configure.error"
    tmp_log = f"{log_dir_path}/{log_prefix}_configure.log"
    command_conf = f"{command_conf} > {tmp_log} 2>&1"

    if project.use_configure:
        execute_command(f"{project.configure_file_path} --help > {project.log_path}/{project.name}.configure_help", DEBUG_LEVEL=-2, NO_STREAM=False, exit_on_error=False)
    elif project.use_cmake:
        execute_command(f"cmake {project.cmake_file_path} --help > {project.log_path}/{project.name}.cmake_help", DEBUG_LEVEL=-2, NO_STREAM=False, exit_on_error=False)


    make_log_path = f"{log_dir_path}/{log_prefix}_make.log"
    make_error_path = f"{log_dir_path}/{log_prefix}_make.error"
    command_make = f"{command_make} >{make_log_path} 2>{make_error_path}"
    command_install  = f"{command_install} >{log_dir_path}/{log_prefix}_install.log 2>{log_dir_path}/{log_prefix}_install.error"

    ##! save cmd and export here
    cur_PATH = os.environ["PATH"]
    with open(f"{log_dir_path}/{log_prefix}_cmd", 'w') as file:
        file.write(f"export PATH={cur_PATH}\n\n{command_conf}")

    mkdir(install_prefix, exist_ok=True)

    ##! ======================== Configure =====================================

    phase_returncodes: list[int] = []

    ##! Configure
    if project.use_configure:
        mark_phase("configure")
        result = run_configure_with_clock_retry(
            command_conf,
            work_dir_path,
            tmp_log,
            project.configure_file_path,
        )
        phase_returncodes.append(result.returncode)
        execute_command(f"cp {work_dir_path}/config.log {log_dir_path}/{log_prefix}_config.log", exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=True)

        returncode_file = f"{log_dir_path}/{log_prefix}_configure.returncode"
        with open(returncode_file, "w", encoding="utf-8") as f:
            f.write(str(result.returncode))

    ##! ======================== CMake (CMake Configure, Make, install) =============================

    if project.use_cmake:
        mark_phase("cmake")
        toolchain_cmake_file_path = f"{log_dir_path}/{log_prefix}.cmake"
        build_path = os.path.join(work_dir_path, f"bb_build_{arch}")
        # generate_cmake_toolchain_file(toolchain_cmake_file_path, arch, compiler, sysroot, toolchain_bin_path, arch_prefix, system, common_options, state.extra_cflags, state.extra_ldflags, TOOL_PATH, toolchain_lib_path, toolchain_version)
        if "gcc" in compiler:
            generate_cmake_toolchain_file(toolchain_cmake_file_path, arch, compiler, sysroot_for_gcc, toolchain_bin_path, arch_prefix, system_for_gcc, common_options, state.extra_cflags, state.extra_ldflags, TOOL_PATH, toolchain_lib_path_for_gcc, toolchain_version)
        elif "clang" in compiler:
            generate_cmake_toolchain_file(toolchain_cmake_file_path, arch, compiler, sysroot_for_clang, toolchain_bin_path, arch_prefix, system_for_clang, common_options, state.extra_cflags, state.extra_ldflags, TOOL_PATH, toolchain_lib_path_for_clang, toolchain_version)

        command_cmake = f"cmake -DCMAKE_INSTALL_PREFIX={install_prefix}"
        command_cmake = f"{command_cmake} -DCMAKE_BUILD_TYPE=Debug" # Debug for TikNib
        command_cmake = f"{command_cmake} -DCMAKE_TOOLCHAIN_FILE={toolchain_cmake_file_path} {state.extra_conf_options}"

        cmake_source_arg = get_cmake_source_arg(project, work_dir_path, build_path)
        command_cmake = f"{command_cmake} {shlex.quote(cmake_source_arg)}"

        tmp_log = f"{log_dir_path}/{log_prefix}_cmake.log"
        command_cmake = f"{command_cmake} > {tmp_log} 2>&1"

        mkdir(install_prefix)
        mkdir(build_path)
        result = execute_command(command_cmake, directory=build_path, exit_on_error=False, DEBUG_LEVEL=0)
        phase_returncodes.append(result.returncode)

        returncode_file = f"{log_dir_path}/{log_prefix}_cmake.returncode"
        with open(returncode_file, "w", encoding="utf-8") as f:
            f.write(str(result.returncode))

        mark_phase("make")
        result = execute_command(command_make, directory=build_path, exit_on_error=False, DEBUG_LEVEL=0)
        phase_returncodes.append(result.returncode)

        returncode_file = f"{log_dir_path}/{log_prefix}_make.returncode"
        with open(returncode_file, "w", encoding="utf-8") as f:
            f.write(str(result.returncode))

        mark_phase("install")
        result = execute_command(command_install, directory=build_path, exit_on_error=False, DEBUG_LEVEL=0)
        phase_returncodes.append(result.returncode)

        returncode_file = f"{log_dir_path}/{log_prefix}_install.returncode"
        with open(returncode_file, "w", encoding="utf-8") as f:
            f.write(str(result.returncode))

        ##! save cmake command and export here
        cur_PATH = os.environ["PATH"]
        with open(f"{log_dir_path}/{log_prefix}.cmd", 'w') as file:
            file.write(f"export PATH={cur_PATH}\n\n{command_cmake}")

    ##! ======================== Make ==========================================

    if project.use_configure:
        mark_phase("make")
        result = run_make_with_clock_retry(
            command_make,
            work_dir_path,
            make_log_path,
            make_error_path,
            project.configure_file_path,
        )
        phase_returncodes.append(result.returncode)

        returncode_file = f"{log_dir_path}/{log_prefix}_make.returncode"
        with open(returncode_file, "w", encoding="utf-8") as f:
            f.write(str(result.returncode))

        mark_phase("install")
        result = execute_command(command_install, directory=work_dir_path, exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=True)
        phase_returncodes.append(result.returncode)

        returncode_file = f"{log_dir_path}/{log_prefix}_install.returncode"
        with open(returncode_file, "w", encoding="utf-8") as f:
            f.write(str(result.returncode))

    ##! ======================== Postprocess ===================================

    execute_command(f"chmod -R +x {install_prefix}", exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=False)

    ##! ======================== Check binaries ================================

    ##! Check compiled binaries and copy
    mark_phase("collect")
    command_find = "find . -type f -executable -exec file {} \\; 2>/dev/null "
    command_find += "| grep -v \"Error\" "
    command_find += f"| grep \"{arch_info.elf_type}\" | grep \"{arch_info.arch_type}\" "
    command_find += "| cut -d \":\" -f 1"
    result = execute_command(command_find, directory=install_prefix, DEBUG_LEVEL=0)
    exec_list = result.stdout.splitlines()
    exec_list = [exec for exec in exec_list if not exec.endswith(".o")]

    pattern = r'/extension/|/gettext-tools/|/contrib/|/test/|/testsuite/|/modules/|/builtins/|/support/|/tests/|/examples/|/doc/|/po/'
    filtered_files = [b for b in exec_list if b.strip() and not re.search(pattern, b)]

    ##! remove duplicated binaries
    filtered_files = remove_dup(filtered_files, install_prefix)
    b2r2_valid = validate_binary_paths(filtered_files, arch)
    if not EXTRA_DEP and b2r2_valid:
        mkdir(output_bins_path, exist_ok=True)
        for file in filtered_files:
            command_cp = f"cp {file} {output_bins_path}/{log_prefix}_{os.path.basename(file)}"
            execute_command(command_cp, directory=install_prefix, DEBUG_LEVEL=0)
    num_compiled_bins = len(filtered_files)
    success = (bool(phase_returncodes)
               and all(code == 0 for code in phase_returncodes)
               and b2r2_valid)
    if not EXTRA_DEP:
        success = success and num_compiled_bins > 0
    if not success:
        if monitor_run_dir:
            _record_variant(monitor_run_dir, project.name, comb, state="running",
                            phase="collect", error=f"phase return codes {phase_returncodes}; ELF binaries {num_compiled_bins}")
        logger.info(f"[RESULT] {num_compiled_bins}: {project_name} {compiler} {arch} {optmz} !!!!")
    else:
        logger.info(f"[RESULT] {num_compiled_bins}: {project_name} {compiler} {arch} {optmz}")

    ##! Cleanup
    command_cleanup = f"rm -rf {work_dir_path}"
    execute_command(command_cleanup, DEBUG_LEVEL=0, NO_STREAM=True)
    return success


def _record_variant(run_dir: str, project: str, comb: tuple[str, str, str],
                    *, state: str, phase: str, error: str | None = None) -> None:
    try:
        update_variant(run_dir, project, comb, state=state, phase=phase, error=error)
    except OSError as exc:
        logger.warning("Could not update build monitor status for %s %s: %s", project, comb, exc)


def compile_project(
    project: Project,
    comb: tuple[str, str, str],
    PATH: str,
    EXTRA_DEP: bool = False,
    cflags: str = '',
    ldflags: str = '',
    conf_options: str = '',
    extra_compiler_options: dict[str, bool] = {},
    monitor_run_dir: str | None = None,
    publish_result: bool = True,
) -> bool:
    """Compile one variant and publish its status when a monitored run is active."""
    if monitor_run_dir:
        _record_variant(monitor_run_dir, project.name, comb, state="running", phase="prepare")
    try:
        result = _compile_project_impl(
            project, comb, PATH, EXTRA_DEP, cflags, ldflags, conf_options,
            extra_compiler_options, monitor_run_dir,
        )
    except BaseException as exc:
        if monitor_run_dir and publish_result:
            _record_variant(monitor_run_dir, project.name, comb, state="failed",
                            phase="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    if monitor_run_dir and publish_result:
        _record_variant(monitor_run_dir, project.name, comb,
                        state="succeeded" if result else "failed",
                        phase="done" if result else "failed")
    return result

# EOF
