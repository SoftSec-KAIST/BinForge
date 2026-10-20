import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import re
from packaging import version

from binforge import config
from binforge.types_helper import *
from binforge.logging_helper import logger
from binforge.utils import execute_command, mkdir

TOOL_PATH = config.TOOL_PATH

# setup crosstool-ng
CTNG_PATH = config.CTNG_PATH
CTNG_BIN = config.CTNG_BIN

# Crosstool-ng does not support specifying the path for the source or target configuration file.
CTNG_CONF = ".config"
CTNG_CONF_OUTPUT = "output"

# architectures supported by ct-ng
CT_CONF = dict()
CT_CONF["arm_32"] = "arm-unknown-linux-gnueabi"
CT_CONF["arm_64"] = "aarch64-unknown-linux-gnu"
CT_CONF["x86_32"] = ""
CT_CONF["x86_64"] = "x86_64-unknown-linux-gnu"
CT_CONF["mips_32"] = "mipsel-unknown-linux-gnu"   # (MIPS 32-bit LE)
CT_CONF["mips_64"] = ""                           # (MIPS 64-bit LE)
CT_CONF["mipseb_32"] = "mips-unknown-linux-gnu"   # (MIPS 32-bit BE)
CT_CONF["mipseb_64"] = "mips64-unknown-linux-gnu" # (MIPS 64-bit BE)
CT_CONF["ppceb_32"] = "powerpc-unknown-linux-gnu"   # (POWERPC 64-bit BE)
CT_CONF["ppceb_64"] = "powerpc64-unknown-linux-gnu" # (POWERPC 64-bit BE)
CT_CONF["ppc_64"] = "powerpc64le-unknown-linux-gnu" # (POWERPC 64-bit LE)
CT_CONF["riscv_32"] = ""                          # (RISC-V 32-bit)
CT_CONF["riscv_64"] = "riscv64-unknown-linux-gnu" # (RISC-V 64-bit)

def select_config(arch: str) -> None | NoReturn:
    command = f"{CTNG_BIN} {arch}"
    execute_command(command, exit_on_error=True, DEBUG_LEVEL=1)

def patch_line(search_line: str, replace_line: str) -> None:
    with open(CTNG_CONF, 'r') as file:
        content = file.read()
    content = content.replace(search_line, replace_line)
    with open(CTNG_CONF, 'w') as file:
        file.write(content)

def patch_line_with_prefix(prefix: str, replace_line: str) -> None:
    with open(CTNG_CONF, 'r') as file:
        content = file.read()
    pattern = rf"^{re.escape(prefix)}.*"
    modified_content = re.sub(pattern, replace_line, content, flags=re.MULTILINE)
    with open(CTNG_CONF, 'w') as file:
        file.write(modified_content)

def apply_basic_binforge_patch() -> None:
    print("[*] Apply my patch file")

    # Set target vendor to unknown
    patch_line_with_prefix( 'CT_TARGET_VENDOR=',
                            'CT_TARGET_VENDOR=\"unknown\"')

    # Set output paths
    patch_line_with_prefix( 'CT_LOCAL_TARBALLS_DIR=',
                            'CT_LOCAL_TARBALLS_DIR=\"${TOOL_PATH}/ctng_tarballs\"')
    patch_line_with_prefix( 'CT_PREFIX_DIR=',
                            'CT_PREFIX_DIR="${CT_PREFIX:-${TOOL_PATH}}/gcc/${CT_HOST:+HOST-${CT_HOST}/}${CT_TARGET}-${CT_GCC_VERSION}"')

    # Set log level to error
    patch_line( '# CT_LOG_ERROR is not set', \
                'CT_LOG_ERROR=y')
    patch_line( 'CT_LOG_EXTRA=y', \
                '# CT_LOG_EXTRA is not set')
    patch_line( 'CT_LOG_LEVEL_MAX="EXTRA"', \
                'CT_LOG_LEVEL_MAX="ERROR"')

    # Do not use multilib
    patch_line( 'CT_MULTILIB=y', \
                '# CT_MULTILIB is not set')
    patch_line_with_prefix( 'CT_CC_GCC_MULTILIB_LIST=',\
                            'CT_DEMULTILIB=y')

    # Unset debug
    patch_line( 'CT_DEBUG_CT=y', \
                '# CT_DEBUG_CT is not set')
    patch_line( 'CT_DEBUG_CT_SAVE_STEPS=y', \
                '# CT_DEBUG_CT_SAVE_STEPS is not set')
    patch_line( 'CT_DEBUG_CT_SAVE_STEPS_GZIP=y', \
                '# CT_DEBUG_CT_SAVE_STEPS_GZIP is not set')

    # Unset static compile
    patch_line( 'CT_STATIC_TOOLCHAIN=y', \
                '# CT_STATIC_TOOLCHAIN is not set')

    # Unset progress bar
    patch_line( 'CT_LOG_PROGRESS_BAR=y', \
                'CT_LOG_PROGRESS_BAR=n')

def patch_bits_64_to_32() -> None:
    patch_line( "CT_ARCH_BITNESS=64", \
                "CT_ARCH_BITNESS=32")
    patch_line( "# CT_ARCH_32 is not set", \
                "CT_ARCH_32=y")
    patch_line( "CT_ARCH_64=y", \
                "# CT_ARCH_64 is not set")

def set_arch_for_x86_32() -> None:
    patch_line( 'CT_ARCH_ARCH=\"\"', \
                'CT_ARCH_ARCH=\"i686\"')

def set_arch_for_mips_32() -> None:
    patch_line( 'CT_ARCH_ARCH=\"mips1\"', \
                'CT_ARCH_ARCH=\"\"')

def set_arch_for_riscv_32() -> None:
    patch_line( 'CT_ARCH_ARCH=\"rv64gc\"', \
                'CT_ARCH_ARCH=\"\"')

def patch_float_mode_soft_to_auto() -> None:
    patch_line( '# CT_ARCH_FLOAT_AUTO is not set', \
                'CT_ARCH_FLOAT_AUTO=y')
    patch_line( 'CT_ARCH_FLOAT_SW=y', \
                '# CT_ARCH_FLOAT_SW is not set')
    patch_line( 'CT_ARCH_FLOAT=\"soft\"', \
                'CT_ARCH_FLOAT=\"auto\"')

def patch_float_mode_hard_to_auto() -> None:
    patch_line( '# CT_ARCH_FLOAT_AUTO is not set', \
                'CT_ARCH_FLOAT_AUTO=y')
    patch_line( 'CT_ARCH_FLOAT_HW=y', \
                '# CT_ARCH_FLOAT_HW is not set')
    patch_line( 'CT_ARCH_FLOAT=\"hard\"', \
                'CT_ARCH_FLOAT=\"auto\"')

def patch_endian_be_to_le() -> None:
    patch_line( 'CT_ARCH_BE=y', \
                '# CT_ARCH_BE is not set')
    patch_line( '# CT_ARCH_LE is not set', \
                'CT_ARCH_LE=y')
    patch_line( 'CT_ARCH_ENDIAN=\"big\"', \
                'CT_ARCH_ENDIAN=\"little\"')

def set_abi_version() -> None:
    patch_line( 'CT_ARCH_ABI=\"\"', \
                'CT_ARCH_ABI=\"elfv2\"')

##! for GCC 6-9, AArch64
def disable_mathvec() -> None:
    patch_line( 'CT_GLIBC_EXTRA_CONFIG_ARRAY=\"\"', \
                'CT_GLIBC_EXTRA_CONFIG_ARRAY=\"--disable-mathvec\"')

def get_gcc_version_from_config(config_path: str) -> str | None:
    CT_GCC_VERSION_PREFIX = "CT_GCC_VERSION="
    with open(config_path, "r") as file:
        for line in file:
            if line.startswith(CT_GCC_VERSION_PREFIX):
                return line.split("=", 1)[1].strip().strip('"')
    return None

def is_version_lower_than(target_version_str: str) -> bool | NoReturn:
    target_version = version.parse(target_version_str)

    gcc_version_str = get_gcc_version_from_config(CTNG_CONF)

    if gcc_version_str is None:
        logger.error("[-] Error: CT_GCC_VERSION not found in config file.")
        exit(1)

    gcc_version = version.parse(gcc_version_str)
    return gcc_version < target_version

def unset_long_double_128() -> None:
    patch_line( 'CT_CC_GCC_LDBL_128=m', \
                '# CT_CC_GCC_LDBL_128 is not set')

def generate_ctng_config(
    arch: str,
    conf_output_path: str = CTNG_CONF_OUTPUT
) -> None:
    # Generating architecture-specific basic config files with ct-ng
    # Since ct-ng provides x86_32 only up to Ubuntu 16.04, apply patches based on x86_64
    if arch == "x86_32":
        select_config(CT_CONF["x86_64"])
    # Since ct-ng does not provide MIPS 64-bit (LE), apply patches based on MIPS 64-bit (BE)
    elif arch == "mips_64":
        select_config(CT_CONF["mipseb_64"])
    elif arch == "riscv_32":
        select_config(CT_CONF["riscv_64"])
    else:
        select_config(CT_CONF[arch])

    # Select the GCC version and unset all debug tools
    # For interaction.
    os.system(f"{CTNG_BIN} menuconfig")

    apply_basic_binforge_patch()

    # Apply patches based on architecture or version
    if arch == "arm_64" and is_version_lower_than("10.1.0"):
        disable_mathvec()
    elif arch == "x86_32":
        patch_bits_64_to_32()
        set_arch_for_x86_32()
    elif arch in (("mips_32", "mipseb_32")):
        patch_float_mode_soft_to_auto()
        set_arch_for_mips_32()
    elif arch == "mips_64":
        patch_float_mode_hard_to_auto()
        patch_endian_be_to_le()
    elif arch == "mipseb_64":
        patch_float_mode_hard_to_auto()
    elif arch == "ppceb_64":
        set_abi_version()
    elif arch == "riscv_32":
        patch_bits_64_to_32()
        set_arch_for_riscv_32()
        unset_long_double_128()

    command = f"mv {CTNG_CONF} {conf_output_path}/{arch}.conf"
    execute_command(command, exit_on_error=True, DEBUG_LEVEL=1)


def main() -> None:
    # Make output directory
    mkdir(path=CTNG_CONF_OUTPUT, exist_ok=True)

    arch_list = [
        "arm_32", "arm_64",
        "x86_32", "x86_64",
        "mips_32", "mips_64",
        "mipseb_32", "mipseb_64",
        "ppceb_32", "ppceb_64",
        "ppc_64",
        # "riscv_32", "riscv_64",
    ]

    # Generate ct-ng config files
    for arch in arch_list:
        generate_ctng_config(arch, CTNG_CONF_OUTPUT)


if __name__ == "__main__":
    main()

# EOF
