import os
import yaml
import distro
from pathlib import Path

from binforge.logging_helper import logger

def get_project_path():
    return str(Path(__file__).resolve().parent.parent)


def get_system_name() -> str:
    sysname = distro.id()
    if sysname not in (("ubuntu", "fedora")):
        logger.error("[-] Unknown system name.")
        sysname = "unknown"
    return sysname


def load_yaml_config(path):
    if not os.path.exists(path):
        return {}
    with open(path, 'r') as f:
        return yaml.safe_load(f) or {}


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
SCRIPT_PATH = os.path.join(PROJECT_PATH, "script")
DEFAULT_APT_PROJECTS_PATH = os.path.join(CONFIG_PATH, "default_apt_projects", "apt_projects.yaml")
GCC_TOOLCHAIN_TARGETS_YAML = os.path.join(CONFIG_PATH, "gcc_toolchain_targets.yaml")
CLANG_TOOLCHAIN_TARGETS_YAML = os.path.join(CONFIG_PATH, "clang_toolchain_targets.yaml")
CTNG_PATH = os.path.join(TOOL_PATH, "crosstool-ng")
CTNG_BIN = os.path.join(CTNG_PATH, "ct-ng")
CTNG_CONF_PATH = os.path.join(PROJECT_PATH, "ctng_conf")
CTNG_TARBALL_PATH = os.path.join(TOOL_PATH, "ctng_tarballs")
EXTRA_DEP_PATH = os.path.join(TOOL_PATH, "extra_deps")
DEFAULTS_YAML_PATH = os.path.join(CONFIG_PATH, "defaults.yaml")
SYSTEM_NAME = get_system_name()
NUM_JOBS = os.cpu_count() or 1
MAX_JOBS = os.cpu_count() or 1


DEFAULT_CONFIG = {
    "PROJECT_PATH": PROJECT_PATH,
    "TOOL_PATH": TOOL_PATH,
    "SCRIPT_PATH": SCRIPT_PATH,
    "CONFIG_PATH": CONFIG_PATH,
    "DEFAULT_APT_PROJECTS_PATH": DEFAULT_APT_PROJECTS_PATH,
    "GCC_TOOLCHAIN_TARGETS_YAML": GCC_TOOLCHAIN_TARGETS_YAML,
    "CLANG_TOOLCHAIN_TARGETS_YAML": CLANG_TOOLCHAIN_TARGETS_YAML,
    "CTNG_PATH": CTNG_PATH,
    "CTNG_BIN": CTNG_BIN,
    "CTNG_CONF_PATH": CTNG_CONF_PATH,
    "CTNG_TARBALL_PATH": CTNG_TARBALL_PATH,
    "EXTRA_DEP_PATH": EXTRA_DEP_PATH,
    "DEFAULTS_YAML_PATH": DEFAULTS_YAML_PATH,
    "SYSTEM_NAME": SYSTEM_NAME,
    "NUM_JOBS": NUM_JOBS,
    "MAX_JOBS": MAX_JOBS,
}

CONFIG = merge_config(DEFAULT_CONFIG, load_yaml_config(DEFAULTS_YAML_PATH))

if __name__ == "__main__":
    # For debug
    print_config_values(CONFIG)

# EOF
