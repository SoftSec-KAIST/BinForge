from binforge.utils import *
from binforge import config
from binforge.models import Project, find_files
from binforge.cmake_manager import generate_cmake_toolchain_file

import re
import inspect

EXTRA_CFLAGS = ""
EXTRA_LDFLAGS = ""
EXTRA_CONF_OPTIONS = ""
EXTRA_DEP_PKG_CONFIG_PATH = ""

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


def add_dep(
    dep_name: str,
    lib_name: str | list[str] | None = None,
    arch: str = ""
) -> None:
    global EXTRA_CFLAGS, EXTRA_LDFLAGS, EXTRA_DEP_PKG_CONFIG_PATH

    # caller_locals = inspect.currentframe().f_back.f_locals # get callers local variables
    cur_frame = inspect.currentframe()
    assert cur_frame is not None, "cur_frame is None"
    frame_back = cur_frame.f_back
    assert frame_back is not None, "frame_back is None"
    caller_locals = frame_back.f_locals # get callers local variables

    if arch == "" and 'arch' in caller_locals:
        arch = caller_locals['arch']

    extra_dep_path = config.EXTRA_DEP_PATH
    EXTRADEP_PATH = os.path.join(extra_dep_path, "install", arch, dep_name)

    if dep_name == "libffi":
        EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -I{EXTRADEP_PATH}/include"
        if arch.endswith("_64"):
            EXTRA_LDFLAGS = f"{EXTRA_LDFLAGS} -L{EXTRADEP_PATH}/lib64"
        else:
            EXTRA_LDFLAGS = f"{EXTRA_LDFLAGS} -L{EXTRADEP_PATH}/lib"
        return None
    elif dep_name == "ncurses":
        EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -I{EXTRADEP_PATH}/include"
        EXTRA_LDFLAGS = f"{EXTRA_LDFLAGS} -L{EXTRADEP_PATH}/lib -lncurses -lcurses -lncursesw"
        return None

    EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -I{EXTRADEP_PATH}/include"
    EXTRA_LDFLAGS = f"{EXTRA_LDFLAGS} -L{EXTRADEP_PATH}/lib"

    pkgconfig_path = os.path.join(EXTRADEP_PATH, "lib", "pkgconfig")
    if os.path.isdir(pkgconfig_path):
        EXTRA_DEP_PKG_CONFIG_PATH = f"{EXTRA_DEP_PKG_CONFIG_PATH}:{pkgconfig_path}"

    if lib_name is None:
        pass
    elif isinstance(lib_name, str):
        EXTRA_LDFLAGS = f"{EXTRA_LDFLAGS} -l{lib_name}"
    elif isinstance(lib_name, list) and all(isinstance(lib, str) for lib in lib_name):
        for lib in lib_name:
            EXTRA_LDFLAGS = f"{EXTRA_LDFLAGS} -l{lib}"
    else:
        raise TypeError("[-] add_dep()")
    return


def my_patch(project: Project, arch: str, compiler: str, optmz: str) -> None:
    global EXTRA_CFLAGS, EXTRA_LDFLAGS, EXTRA_CONF_OPTIONS, EXTRA_DEP_PKG_CONFIG_PATH

    ##! -fno-finite-math-only
    if project.name in (("apl", "datamash", "cfitsio", "freeipmi", "gama", "gawk", "gcal", "gettext", "glpk", "gsl", "libpython", "libxml2", "tokyocabinet", "units")):
        if optmz in (("Ofast")):
            if compiler in (("clang-7.1.0", "clang-8.0.1", "clang-9.0.1")):
                if arch in (("x86_32", "x86_64", "arm_64", "mips_32", "mips_64", "mipseb_32", "mipseb_64", "ppceb_32", "ppceb_64", "ppc_64")):
                    EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -fno-finite-math-only"

    elif project.name in (("gtypist")):
        if optmz in (("Ofast")):
            if compiler in (("clang-10.0.1", "clang-11.1.0", "clang-12.0.1", "clang-13.0.1", "clang-14.0.6")):
                if arch in (("arm_32")):
                    EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -fno-finite-math-only"

    ##! -Wno-implicit-function-declaration
    if project.name in (("bash", "dap", "libcddb", "libcdio", "libuuid", "parted", "recutils", "time")):
        if compiler in (("clang-16.0.6", "clang-17.0.6", "clang-18.1.8", "clang-19.1.7", "gcc-14.2.0")):
            EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -Wno-implicit-function-declaration"

    #! For C++ project with clang
    if project.name in (("apl", "ddrescue")):
        EXTRA_LDFLAGS = f"{EXTRA_LDFLAGS} -lstdc++"

    ##! Add dep automatically
    dep_list = project.dep_list
    if dep_list is not None:
        for dep in dep_list or []:
            # print(f"[DEBUG] {project.name} add dep ({dep})")
            add_dep(dep)

    ##! ========================================================================

    if project.name == "binutils":
        #! gprofng is a binary that can only be compiled for x86, x64,
        #! and arm64 target architectures.
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --disable-gprofng"

    elif project.name == "cfitsio":
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --without-fortran"

    elif project.name == "coreutils":
        #! 'utils_cv_stdbuf_supported=yes' for stdbuf, libstdbuf
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} utils_cv_stdbuf_supported=yes"
        if compiler in (("clang-10.0.1", "clang-11.1.0", "clang-12.0.1", "clang-13.0.1", "clang-14.0.6")):
            if arch in (("arm_32")):
                if optmz in (("Ofast")):
                    EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -lm"
        if compiler in (("clang-7.1.0")):
            if arch in (("x86_32", "x86_64")):
                EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -mno-vpclmulqdq"

    elif project.name == "curl":
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS}  --without-ssl --without-libpsl --disable-ldap"
        EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -std=gnu11"

    elif project.name == "datamash":
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} gl_cv_func_strcasecmp_works=yes"

    elif project.name == "dap":
        if compiler in (("gcc-14.2.0")):
            EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -Wno-incompatible-pointer-types"

    if project.name == "dico":
        EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -Wno-int-conversion"

    elif project.name == "diffutils":
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} gl_cv_func_strcasecmp_works=yes"

    elif project.name == "freeipmi":
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --without-encryption --without-random-device"

    elif project.name == "gettext":
        #! for clang-v7 ~ v18
        if "clang" in compiler:
            if arch == "arm_32":
                EXTRA_LDFLAGS = f"{EXTRA_LDFLAGS} -lm -latomic"

    elif project.name == "gtypist":
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} ac_cv_func_malloc_0_nonnull=yes ac_cv_func_realloc_0_nonnull=yes ac_cv_header_ncursesw_ncurses_h=yes"

    elif project.name == "inetutils":
        EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -D_PATH_PROCNET_DEV=\"/proc/net/dev\""
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --with-path-procnet-dev=\"/proc/net/dev\""

    elif project.name == "libcddb":
        EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -Wno-int-conversion -Wno-incompatible-pointer-types"
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} ac_cv_func_malloc_0_nonnull=yes ac_cv_func_realloc_0_nonnull=yes"

    elif project.name == "libexpat":
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --without-docbook"

    elif project.name == "libpaper":
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --enable-relocatable"

    elif project.name == "libpython":
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --with-build-python --disable-ipv6 ac_cv_file__dev_ptmx=yes ac_cv_file__dev_ptc=no"

    elif project.name == "libxcrypt":
        if optmz in (("O3", "Ofast")):
            if "gcc" in compiler:
                EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -Wno-error=null-dereference"
            if compiler in (("gcc-12.4.0", "gcc-13.3.0")):
                EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -Wno-error=stringop-overread"

    # elif project.name == "nano":
    #     add_dep("ncurses", ["ncurses", "curses", "ncursesw", "tinfo"])

    elif project.name == "ncurses":
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --disable-stripping --with-shared --with-normal --with-cxx --with-cxx-binding --enable-widec --enable-overwrite --with-curses-h --enable-symlinks"

    elif project.name == "openssl":
        EXTRA_LDFLAGS = f"{EXTRA_LDFLAGS} -lm -latomic"
        if arch in (("mipseb_32")):
            if "clang" in compiler:
                EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -D__STDC_NO_ATOMICS__"

    elif project.name == "parted":
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --disable-device-mapper"

    elif project.name == "recutils":
        add_dep("libuuid", "uuid") #! need -luuid

    elif project.name == "screen":
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --disable-pam"

    elif project.name == "sharutils":
        EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -fcommon"

    elif project.name == "texinfo":
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --disable-rpath --disable-nls"

    elif project.name == "wcslib":
        #! failed passing dep cfitsio. just disable
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --disable-fortran --without-cfitsio --without-pgplot"

    elif project.name == "xz":
        if compiler == "clang-19.1.7" and optmz == "Ofast":
            #! Ofast deprecated from clang v19 (CFLAGS+=-Weverything or SKIP_WERROR_CHECK=yes)
            EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} SKIP_WERROR_CHECK=yes"
        #! failed passing dep cfitsio. just disable
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --disable-fortran --without-cfitsio --without-pgplot"

    elif project.name == "zstd":
        if compiler in (("clang-12.0.1", "clang-13.0.1", "clang-14.0.6")):
            if arch in (("arm_32")):
                EXTRA_LDFLAGS = f"{EXTRA_LDFLAGS} -lm"

    ##! ============================ Test for Dale =============================

    if project.name == "libming":
        EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -fcommon"
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --disable-freetype"

    ##! ========================================================================

def compile_project(
    project: Project,
    comb: tuple[str, str, str],
    PATH: str,
    EXTRA_DEP: bool = False,
    cflags: str = '',
    ldflags: str = '',
    conf_options: str = '',
    extra_compiler_options: dict[str, bool] = {}
) -> None:
    ##! return to original PATH
    set_env_variable("PATH", PATH)
    compiler, arch, optmz = comb
    project_name = project.name
    full_name = project.full_name
    source_path = project.source_path
    output_bins_path = project.bins_path
    log_dir_path = project.log_path
    TOOL_PATH, NUM_JOBS, MAX_JOBS = config.TOOL_PATH, config.NUM_JOBS, config.MAX_JOBS
    compiletype = f"{compiler}_{arch}_{optmz}"

    logger.info(f"[DEBUG] {full_name}: {compiletype}")
    log_prefix = f"{full_name}_{compiletype}"
    work_dir_name = f"{full_name}_{compiletype}"
    work_dir_path = os.path.join(source_path, work_dir_name)
    config_file_path = os.path.join(work_dir_path, "configure")
    arch_info = PREDEFIEND_ARCH_INFO[arch]
    arch_prefix = arch_info.arch_prefix

    global EXTRA_CFLAGS, EXTRA_LDFLAGS, EXTRA_CONF_OPTIONS, EXTRA_DEP_PKG_CONFIG_PATH

    ##! ========================================================================

    toolchain_version = "13.3.0"

    if "gcc" in compiler:
        toolchain_version = compiler.replace("gcc-", '')

    toolchain_bin_path = ""
    if "gcc" in compiler:
        compiler_version_ = compiler.replace("gcc-", '') # toolchain_version
        toolchain_bin_path = os.path.join(TOOL_PATH, "gcc", f"{arch_prefix}-{compiler_version_}", "bin")
        add_path_env(toolchain_bin_path)
    elif "clang" in compiler:
        toolchain_bin_path = os.path.join(TOOL_PATH, "gcc", f"{arch_prefix}-{toolchain_version}", "bin")
        add_path_env(toolchain_bin_path)

        clang_path = os.path.join(TOOL_PATH, "clang", compiler, "bin")
        add_path_env(clang_path)

    toolchain_lib_path = os.path.join(TOOL_PATH, "gcc", f"{arch_prefix}-{toolchain_version}", arch_prefix, "lib")
    sysroot = os.path.join(TOOL_PATH, "gcc", f"{arch_prefix}-{toolchain_version}", arch_prefix, "sysroot")
    system = os.path.join(TOOL_PATH, "gcc", f"{arch_prefix}-{toolchain_version}", arch_prefix, "sysroot", "usr", "include")

    ##! ========================================================================

    #! Make work directory
    execute_command(f"rm -rf {source_path}/{work_dir_name}", DEBUG_LEVEL=0, NO_STREAM=True)
    execute_command(f"cp -R --preserve=all {source_path}/{full_name} {source_path}/{work_dir_name}", DEBUG_LEVEL=0, NO_STREAM=True)

    if project.name in (("zstd", "bzip2", "libcapstone")):
        project.use_cmake = True
        project.skip_configure = True

    #! Test for Dale
    if project.name in (("exiv2")):
        project.use_cmake = True
        project.skip_configure = True
    if project.name in (("libming", "tcpdump", "libpcap")):
        execute_command(f"./autogen.sh", directory=work_dir_path, DEBUG_LEVEL=0, NO_STREAM=True, exit_on_error=False)

    ##! check whether configure file exists
    if not file_exists(config_file_path):
        if project.skip_configure or project.use_cmake:
            pass
        #! TODO: Fix me as rename Configure to configure of openssl
        elif project.name in (("openssl")):
            pass
        elif project.name in (("fribidi", "libxml2")):
            execute_command(f"./autogen.sh", directory=work_dir_path, DEBUG_LEVEL=0, NO_STREAM=True, exit_on_error=False)
        elif project.name in (("libxslt", "libredwg")):
            execute_command(f"autoreconf -fvi; make distclean", directory=work_dir_path, DEBUG_LEVEL=0, NO_STREAM=True, exit_on_error=False)
        elif project.name in (("")):
            execute_command(f"autoconf", directory=work_dir_path, DEBUG_LEVEL=0, NO_STREAM=True, exit_on_error=False)
        elif project.name in (("libexpat")):
            execute_command(f"./buildconf.sh", directory=work_dir_path, DEBUG_LEVEL=0, NO_STREAM=True, exit_on_error=False)
            pass
        else:
            logger.error("Not implemented!")
            logger.error("Execute autoconf here!")

    ##! ========================================================================

    EXTRA_CFLAGS = cflags
    EXTRA_LDFLAGS = ldflags
    EXTRA_CONF_OPTIONS = conf_options
    EXTRA_DEP_PKG_CONFIG_PATH = ""

    my_patch(project, arch, compiler, optmz)

    EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -g"
    if not project.use_cmake:
        EXTRA_CONF_OPTIONS = f"{EXTRA_CONF_OPTIONS} --enable-shared"
    common_options = f"{arch_info.arch_option} -{optmz}"

    #! ==================== Extra options ====================

    PIE = extra_compiler_options.get("pie", False)
    NO_INLINE = extra_compiler_options.get("no_inline", False)
    STACK_PROTECTOR = extra_compiler_options.get("stack_protector", False)

    ##! Note: PIE is default option for modern compilers, but crosstool-ng's
    ##! default option is no-PIE.
    ##! default no-PIE: gcc v6-v14, clang v6-v14
    ##! default PIE: clang v15-v19
    PIE = False
    if not PIE:
        if compiler in (("clang-15.0.7", "clang-16.0.6", "clang-17.0.6", "clang-18.1.8", "clang-19.1.7")):
            #! Pay attention to the order -fPIC must come after.
            EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -fno-PIE -fPIC"
            EXTRA_LDFLAGS = f"{EXTRA_LDFLAGS} -no-pie"
    else:
        EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -fPIE -fPIC"
        EXTRA_LDFLAGS = f"{EXTRA_LDFLAGS} -pie" # Ordering -pie -> -shared

    if NO_INLINE:
        EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -fno-inline"

    STACK_PROTECTOR = False
    if STACK_PROTECTOR:
        EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -fstack-protector-all"

    LTO = False
    if LTO:
        ##! Test LTO
        EXTRA_LDFLAGS = f"{EXTRA_LDFLAGS} -flto"
        EXTRA_CFLAGS = f"{EXTRA_CFLAGS} -flto"
        if "clang" in compiler:
            EXTRA_LDFLAGS = f"{EXTRA_LDFLAGS} -fuse-ld=lld"

    conf_options = f"--host=\"{arch_prefix}\""
    if project.name == "zlib":
        conf_options = ""

    if "gcc" in compiler:
        conf_options = f"{conf_options} CC=\"{arch_prefix}-gcc\"" ##! for project 'ed'
        conf_options = f"{conf_options} CXX=\"{arch_prefix}-g++\"" ##! for project 'ddrescue'

        conf_options = f"{conf_options} CFLAGS=\"  -isysroot {sysroot} -I{system} {common_options} {EXTRA_CFLAGS}\"" # '-isystem {system} -I{system}' -> '-I{system}'
        conf_options = f"{conf_options} CXXFLAGS=\"-isysroot {sysroot} -I{system} {common_options} {EXTRA_CFLAGS}\"" # '-isystem {system} -I{system}' -> '-I{system}'

        conf_options = f"{conf_options} LDFLAGS=\"{common_options} -L{toolchain_lib_path} {EXTRA_LDFLAGS}\""
        conf_options = f"{conf_options} AR=\"{arch_prefix}-gcc-ar\""
        conf_options = f"{conf_options} RANLIB=\"{arch_prefix}-gcc-ranlib\""
        conf_options = f"{conf_options} NM=\"{arch_prefix}-gcc-nm\""
        conf_options = f"{conf_options} {EXTRA_CONF_OPTIONS}"
    elif "clang" in compiler:
        ##! So far, the only projects that need to be explicitly passed to
        ##! `CC` are "help2man" and "gmp," but there have been no issues
        ##! even when passing it to all projects.

        #! Set CC
        conf_options = f"{conf_options} CC=\"clang --target={arch_prefix} --gcc-toolchain={TOOL_PATH}/gcc/{arch_prefix}-{toolchain_version}"
        if arch == "ppceb_64": conf_options = f"{conf_options} {arch_info.arch_option}"
        conf_options = f"{conf_options}  \""

        #! Set CXX
        conf_options = f"{conf_options} CXX=\"clang++ --target={arch_prefix} --gcc-toolchain={TOOL_PATH}/gcc/{arch_prefix}-{toolchain_version}"
        if arch == "ppceb_64": conf_options = f"{conf_options} {arch_info.arch_option}"
        conf_options = f"{conf_options}  \""

        #! '-isystem' for libxcrypt clang
        conf_options = f"{conf_options} CFLAGS=\"  -isysroot {sysroot} -isystem {system} -I{system} {common_options} {EXTRA_CFLAGS}\""
        conf_options = f"{conf_options} CXXFLAGS=\"-isysroot {sysroot} -isystem {system} -I{system} {common_options} {EXTRA_CFLAGS}\""

        conf_options = f"{conf_options} LDFLAGS=\"{common_options} -L{toolchain_lib_path} {EXTRA_LDFLAGS}\""
        conf_options = f"{conf_options} AR=\"llvm-ar\""
        conf_options = f"{conf_options} RANLIB=\"llvm-ranlib\""
        conf_options = f"{conf_options} NM=\"llvm-nm\""
        conf_options = f"{conf_options} {EXTRA_CONF_OPTIONS}"

    install_prefix = project.install_prefix if project.install_prefix else os.path.join(work_dir_path, "bb_install")

    if project.name == "openssl":
        platform = "linux-generic64" if "_64" in arch else "linux-generic32"
        command_conf = f"./Configure {platform} shared --prefix=\"{install_prefix}\""
        command_conf = f"{command_conf} --cross-compile-prefix=\"{arch_prefix}-\""
        command_conf = f"{command_conf} -I{system}"
        command_conf = f"{command_conf} {common_options}"
        ##! Openssl requires that flags be passed using env.
        set_env_variable("CFLAGS", EXTRA_CFLAGS)
        set_env_variable("LDFLAGS", EXTRA_LDFLAGS)
    elif EXTRA_DEP_PKG_CONFIG_PATH:
        pkg_conf = f"PKG_CONFIG_PATH={EXTRA_DEP_PKG_CONFIG_PATH}:$PKG_CONFIG_PATH"
        command_conf = f"{pkg_conf} ./configure --prefix=\"{install_prefix}\" --build=x86_64-linux-gnu {conf_options}"
    else:
        command_conf = f"./configure --prefix=\"{install_prefix}\" --build=x86_64-linux-gnu {conf_options}"

    if project.name == "zlib":
        cc = os.path.join(TOOL_PATH, "gcc", f"{arch_prefix}-{toolchain_version}", "bin", f"{arch_prefix}-gcc")
        conf_options = f"CC=\"{cc}\" CFLAGS=\"-g\""
        conf_options = f"{conf_options} AR=\"{arch_prefix}-gcc-ar\""
        conf_options = f"{conf_options} RANLIB=\"{arch_prefix}-gcc-ranlib\""
        command_conf = f"{conf_options} ./configure --prefix=\"{install_prefix}\""

    command_make = f"make -j {NUM_JOBS} -l {MAX_JOBS} --output-sync=target" # V=1 --debug=a
    #! Race condition bug
    if project.name == "libming":
        command_make = f"make -j 1 -l 1 --output-sync=target" # V=1 --debug=a

    if project.name == "libpng":
        pre_make_cmd = "sed -i 's/^CPPFLAGS[[:space:]]*=.*/CPPFLAGS = $(CFLAGS)/' Makefile"
        command_make = f"{pre_make_cmd}; {command_make}"

    # command_make = f"make V=1" # -d --debug=a
    if project.name in (("openssl", "bzip2")):
        if "gcc" in compiler:
            command_make = f"{command_make} AR=\"{arch_prefix}-gcc-ar\""
            command_make = f"{command_make} RANLIB=\"{arch_prefix}-gcc-ranlib\""
            command_make = f"{command_make} NM=\"{arch_prefix}-gcc-nm\""
        elif "clang" in compiler:
            command_make = f"{command_make} CC=\"clang --target={arch_prefix} --gcc-toolchain={TOOL_PATH}/gcc/{arch_prefix}-{toolchain_version} \""
            command_make = f"{command_make} AR=\"llvm-ar\""
            command_make = f"{command_make} RANLIB=\"llvm-ranlib\""
            command_make = f"{command_make} NM=\"llvm-nm\""

    command_install = "make install"
    if project.name == "openssl":
        command_install = "make install_sw" # -j88: slow

    command_conf = f"{command_conf} >{log_dir_path}/{log_prefix}_configure.log 2>{log_dir_path}/{log_prefix}_configure.error"
    command_make = f"{command_make} >{log_dir_path}/{log_prefix}_make.log 2>{log_dir_path}/{log_prefix}_make.error"
    command_install  = f"{command_install} >{log_dir_path}/{log_prefix}_install.log 2>{log_dir_path}/{log_prefix}_install.error"

    ##! save cmd and export here
    cur_PATH = os.environ["PATH"]
    with open(f"{log_dir_path}/{log_prefix}.cmd", 'w') as file:
        file.write(f"export PATH={cur_PATH}\n\n{command_conf}")

    #! Delete default optimization from configure file
    if file_exists(config_file_path):
        modify_config(config_file_path)

    mkdir(install_prefix, exist_ok=True)

    ##! ======================== Configure =====================================
    ##! Configure
    if not project.skip_configure:
        result = execute_command(command_conf, directory=work_dir_path, exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=True)
        execute_command(f"cp {work_dir_path}/config.log {log_dir_path}/{log_prefix}_config.log", exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=True)

    ##! ======================== Configure (CMake) =============================

    if project.use_cmake:
        # logger.info("[*] Use cmake")
        toolchain_cmake_file_path = f"{log_dir_path}/{log_prefix}.cmake"
        generate_cmake_toolchain_file(toolchain_cmake_file_path, arch, compiler, sysroot, toolchain_bin_path, arch_prefix, system, common_options, EXTRA_CFLAGS, EXTRA_LDFLAGS, TOOL_PATH, toolchain_lib_path, toolchain_version)

        command_cmake = f"cmake -DCMAKE_INSTALL_PREFIX={install_prefix}"
        command_cmake = f"{command_cmake} -DCMAKE_BUILD_TYPE=Debug" # Debug for TikNib
        command_cmake = f"{command_cmake} -DCMAKE_TOOLCHAIN_FILE={toolchain_cmake_file_path} {EXTRA_CONF_OPTIONS}"

        if project.name == "exiv2":
            command_cmake = command_cmake.replace(f"-DCMAKE_TOOLCHAIN_FILE={toolchain_cmake_file_path}", '')

        if project.name in (("zstd")):
            command_cmake = f"{command_cmake} ../build/cmake"
        elif project.name in (("bzip2")):
            ##! Test 'shared' to default option
            command_cmake = f"{command_cmake} -Ddefault_library=shared ../"
        else:
            command_cmake = f"{command_cmake} ../"

        command_cmake = f"{command_cmake} >{log_dir_path}/{log_prefix}_cmake.log 2>{log_dir_path}/{log_prefix}_cmake.error"

        build_path = os.path.join(work_dir_path, f"bb_build_{arch}")
        mkdir(install_prefix)
        mkdir(build_path)
        execute_command(command_cmake, directory=build_path, exit_on_error=False, DEBUG_LEVEL=0)
        execute_command(command_make, directory=build_path, exit_on_error=False, DEBUG_LEVEL=0)
        execute_command(command_install, directory=build_path, exit_on_error=False, DEBUG_LEVEL=0)

        ##! save cmake command and export here
        cur_PATH = os.environ["PATH"]
        with open(f"{log_dir_path}/{log_prefix}.cmd", 'w') as file:
            file.write(f"export PATH={cur_PATH}\n\n{command_cmake}")

    ##! ======================== Make ==========================================

    if not project.use_cmake:
        execute_command(command_make, directory=work_dir_path, exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=False)
        execute_command(command_install, directory=work_dir_path, exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=True)

    ##! ======================== Postprocess ===================================

    ##! for all (e.g. inteutils)
    execute_command(f"chmod -R +x {install_prefix}", exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=False)
    if project.name == "ncurses":
        #! for extra deps
        install_lib_path = os.path.join(install_prefix, "lib")
        install_include_path = os.path.join(install_prefix, "include")
        execute_command("ln -s libncursesw.so libncurses.so", directory=install_lib_path, exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=False)
        execute_command("ln -s libncursesw.so libcurses.so", directory=install_lib_path, exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=False)
        execute_command("ln -s libncursesw.a libncurses.a", directory=install_lib_path, exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=False)
        execute_command("ln -s libncursesw.so libtinfo.so", directory=install_lib_path, exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=False)
        execute_command("mkdir -p ncursesw", directory=install_include_path, exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=False)
        execute_command("cp curses.h ncursesw/ncurses.h", directory=install_include_path, exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=False)
    #! These libraries do not support cross-compilation.
    elif project.name == "texinfo":
        execute_command(f"rm -rf {install_prefix}/lib/texi2any", exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=False)
    elif project.name == "gettext":
        execute_command(f"rm -rf {install_prefix}/lib/libasprintf.so.0.0.0", exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=False)
    elif project.name == "libpython" and EXTRA_DEP:
        pyconfig_arch_map = {
            "arm_32": "arm-linux-gnueabi",
            "arm_64": "aarch64-linux-gnu",
            "x86_32": "i386-linux-gnu",
            "x86_64": "x86_64-linux-gnu",
            "mips_32": "mipsel-linux-gnu",
            "mips_64": "mips64el-linux-gnuabi64",
            "mipseb_32": "mips-linux-gnu",
            "mipseb_64": "mips64-linux-gnuabi64",
            "ppceb_32": "powerpc-linux-gnu",
            "ppceb_64": "powerpc64-linux-gnu",
            "ppc_64": "powerpc64le-linux-gnu",
        }
        pyconfig_arch = pyconfig_arch_map[arch]
        execute_command(f"mkdir -p {install_prefix}/include/{pyconfig_arch}", exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=False)
        execute_command(f"ln -sf {install_prefix}/include/python3.12 {install_prefix}/include/{pyconfig_arch}/python3.12", exit_on_error=False, DEBUG_LEVEL=0, NO_STREAM=False)

    ##! ======================== Check binaries ================================

    ##! Check compiled binaries and copy
    command_find = "find . -type f -executable -exec file {} \\; 2>/dev/null "
    command_find += "| grep -v \"Error\" "
    command_find += f"| grep \"{arch_info.elf_type}\" | grep \"{arch_info.arch_type}\" "
    command_find += "| cut -d \":\" -f 1"
    result = execute_command(command_find, directory=install_prefix, DEBUG_LEVEL=0)
    exec_list = result.stdout.splitlines()
    exec_list = [exec for exec in exec_list if not exec.endswith(".o")]

    pattern = r'/extension/|/gettext-tools/|/contrib/|/test/|/testsuite/|/modules/|/builtins/|/support/|/tests/|/examples/|/doc/|/po/'
    filtered_files = [b for b in exec_list if b.strip() and not re.search(pattern, b)]

    ##! remove dup here
    filtered_files = remove_dup(filtered_files, install_prefix)
    if not EXTRA_DEP:
        mkdir(output_bins_path, exist_ok=True)
        for file in filtered_files:
            command_cp = f"cp {file} {output_bins_path}/{log_prefix}_{os.path.basename(file)}"
            execute_command(command_cp, directory=install_prefix, DEBUG_LEVEL=0)
    num_compiled_bins = len(filtered_files)
    if num_compiled_bins == 0:
        logger.info(f"[RESULT] {num_compiled_bins}: {project_name} {compiler} {arch} {optmz} !!!!")
    else:
        logger.info(f"[RESULT] {num_compiled_bins}: {project_name} {compiler} {arch} {optmz}")

    # if project.name == "":
    #     return

    ##! Cleanup
    command_cleanup = f"rm -rf {work_dir_path}"
    execute_command(command_cleanup, DEBUG_LEVEL=0, NO_STREAM=True)

# EOF
