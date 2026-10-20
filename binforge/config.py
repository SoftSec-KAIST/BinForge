import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import os
import distro
import platform
from pathlib import Path

from binforge.logging_helper import logger
from binforge.utils import load_yaml

#! TODO:
#!   1. Set config variables without CONFIG
#!   2. Use `import platform; platform.freedesktop_os_release().get("VERSION_ID")`

def get_project_path():
    return str(Path(__file__).resolve().parent.parent)


def get_system_name() -> str:
    sysname = distro.id()
    if sysname not in (("ubuntu", "fedora")):
        logger.error("[-] Unknown system name.")
        sysname = "unknown"
    return sysname


# Only override if the YAML value exists and is not an empty string.
def merge_config(default_cfg, override_cfg):
    merged = default_cfg.copy()
    for k, v in override_cfg.items():
        if v is not None and str(v).strip() != "":
            merged[k] = v
    return merged


def print_config_values(config):
    for key, value in config.items():
        print(f"[*] {key}: {value}")


PROJECT_PATH = get_project_path()

TOOL_PATH = os.path.join(PROJECT_PATH, "tools")
CONFIG_PATH = os.path.join(PROJECT_PATH, "config")
SCRIPT_PATH = os.path.join(PROJECT_PATH, "scripts")

GCC_DIR_PATH = os.path.join(TOOL_PATH, "gcc")
CLANG_DIR_PATH = os.path.join(TOOL_PATH, "clang")
BOOTSTRAP_PATH = os.path.join(TOOL_PATH, "bootstrap")
B2R2_PATH = os.path.join(TOOL_PATH, "b2r2")
B2R2_VALIDATOR_PATH = os.path.join(
    B2R2_PATH, "validator", "BinForge.B2R2Validator.dll"
)

PATCH_SETUP_GCC_DIR_PATH = os.path.join(PROJECT_PATH, "patches", "setup_gcc")
PATCH_SETUP_CLANG_DIR_PATH = os.path.join(PROJECT_PATH, "patches", "setup_clang")

DEFAULT_APT_PACKAGES_PATH = os.path.join(CONFIG_PATH, "default_apt_packages", "apt_packages.yaml")

GCC_TOOLCHAIN_TARGETS_YAML = os.path.join(CONFIG_PATH, "gcc_toolchain_targets.yaml")
CLANG_TOOLCHAIN_TARGETS_YAML = os.path.join(CONFIG_PATH, "clang_toolchain_targets.yaml")

CTNG_PATH = os.path.join(TOOL_PATH, "crosstool-ng")
CTNG_BIN = os.path.join(CTNG_PATH, "ct-ng")
CTNG_CONF_PATH = os.path.join(PROJECT_PATH, "ctng_conf")
CTNG_TARBALL_PATH = os.path.join(TOOL_PATH, "ctng_tarballs")

EXTRA_DEP_PATH = os.path.join(TOOL_PATH, "extra_deps")
ARCHIVE_ROOT = str(Path.home() / "archive_binforge" / "cache" / "downloads")
DEFAULTS_YAML_PATH = os.path.join(CONFIG_PATH, "defaults.yaml")

SYSTEM_NAME = get_system_name()

NUM_JOBS = os.cpu_count() or 1
MAX_JOBS = os.cpu_count() or 1

#! Need test
OS_VERSION = platform.freedesktop_os_release().get("VERSION_ID")

DEFAULT_CONFIG = {
    "PROJECT_PATH": PROJECT_PATH,
    "TOOL_PATH": TOOL_PATH,
    "CONFIG_PATH": CONFIG_PATH,
    "SCRIPT_PATH": SCRIPT_PATH,
    "GCC_DIR_PATH": GCC_DIR_PATH,
    "CLANG_DIR_PATH": CLANG_DIR_PATH,
    "BOOTSTRAP_PATH": BOOTSTRAP_PATH,
    "B2R2_PATH": B2R2_PATH,
    "B2R2_VALIDATOR_PATH": B2R2_VALIDATOR_PATH,
    "PATCH_SETUP_GCC_DIR_PATH": PATCH_SETUP_GCC_DIR_PATH,
    "PATCH_SETUP_CLANG_DIR_PATH": PATCH_SETUP_CLANG_DIR_PATH,
    "DEFAULT_APT_PACKAGES_PATH": DEFAULT_APT_PACKAGES_PATH,
    "GCC_TOOLCHAIN_TARGETS_YAML": GCC_TOOLCHAIN_TARGETS_YAML,
    "CLANG_TOOLCHAIN_TARGETS_YAML": CLANG_TOOLCHAIN_TARGETS_YAML,
    "CTNG_PATH": CTNG_PATH,
    "CTNG_BIN": CTNG_BIN,
    "CTNG_CONF_PATH": CTNG_CONF_PATH,
    "CTNG_TARBALL_PATH": CTNG_TARBALL_PATH,
    "EXTRA_DEP_PATH": EXTRA_DEP_PATH,
    "ARCHIVE_ROOT": ARCHIVE_ROOT,
    "DEFAULTS_YAML_PATH": DEFAULTS_YAML_PATH,
    "SYSTEM_NAME": SYSTEM_NAME,
    "NUM_JOBS": NUM_JOBS,
    "MAX_JOBS": MAX_JOBS,
    "OS_VERSION": OS_VERSION
}

CONFIG = merge_config(DEFAULT_CONFIG, load_yaml(DEFAULTS_YAML_PATH) or {})
ARCHIVE_ROOT = os.path.expanduser(str(CONFIG.get("ARCHIVE_ROOT", ARCHIVE_ROOT)))
CONFIG["ARCHIVE_ROOT"] = ARCHIVE_ROOT
NUM_JOBS = int(CONFIG["NUM_JOBS"])
MAX_JOBS = int(CONFIG["MAX_JOBS"])
if NUM_JOBS < 1 or MAX_JOBS < 1:
    raise ValueError("NUM_JOBS and MAX_JOBS must be positive integers")
CONFIG["NUM_JOBS"] = NUM_JOBS
CONFIG["MAX_JOBS"] = MAX_JOBS

if __name__ == "__main__":
    # For debug
    print_config_values(CONFIG)

# EOF
