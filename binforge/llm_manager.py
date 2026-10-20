from __future__ import annotations

from binforge import config
from binforge.utils import *
from binforge.models import Project
from binforge.compile_manager import compile_project
from binforge.autotools_fallback import guarded_autotools_probe
from binforge.source_manager import download_and_extract_projects
from binforge.conf_manager import analyze_projects
from binforge.compiler_options import compiler_option_sets
from binforge.llm_utils import print_dependency_tree_status
from binforge.run_openai_api import DEFAULT_MODEL, analyze_build_error_once

import os
import json
import requests
import shlex
import datetime
import time
import copy
import re
import subprocess
from treelib import Tree
import pprint
import shutil
from pathlib import Path
from itertools import product
from dataclasses import dataclass

ROOT_LOG = ""

COMPILER = "gcc-13.3.0"
# COMPILER = "clang-19.1.7"
ARCH = "x86_64"
# ARCH = "arm_64"
OPTMZ = "O2"

EXTRA_COMPILER_OPTIONS = {
    "pie": False,
    "no_inline": False,
    "stack_protector": False
}

COMBINATION = (COMPILER, ARCH, OPTMZ)


@dataclass(frozen=True)
class SolverBuildVariant:
    compiler: str
    arch: str
    optmz: str
    extra_compiler_options: dict[str, bool]
    option_mode: str = ""

    @property
    def combination(self) -> tuple[str, str, str]:
        return self.compiler, self.arch, self.optmz

    @property
    def flag_bits(self) -> str:
        return "".join(
            "1" if self.extra_compiler_options[key] else "0"
            for key in ("pie", "no_inline", "stack_protector")
        )

    @property
    def file_tag(self) -> str:
        return f"flags-{self.flag_bits}" if self.option_mode else ""


BUILD_VARIANTS: list[SolverBuildVariant] = [
    SolverBuildVariant(COMPILER, ARCH, OPTMZ, dict(EXTRA_COMPILER_OPTIONS))
]
CURRENT_VARIANT = BUILD_VARIANTS[0]
PROCESS_LOG_DIR = ''
MAX_LLM_REPAIR_ITERATIONS = 20
DEPENDENCY_SOURCE_LOOKUP_ENABLED = True
SOURCE_CONFIG_PATH = ""
SOLVER_DIRNAME = "llm_solver"
STATE_FILENAME = "state.json"
MERGE_FILENAME = "merge_candidates.yaml"
MERGED_CONFIG_FILENAME = "merged_config.yaml"
TRIALS_DIRNAME = "trials"
PROMPT_PATH = Path(__file__).resolve().with_name("prompt.txt")
PROMPT_NO_DEP_LOOKUP_PATH = Path(__file__).resolve().with_name("prompt_no_dep_lookup.txt")

SAVE_KEYS = ["cflags", "ldflags", "dep_list", "dep_path", "conf_options"]


def _get_build_values(
    build_cfg: dict[str, Any],
    key: str,
    default: str,
) -> list[str]:
    values = build_cfg.get(key)
    if not isinstance(values, list) or not values:
        return [default]

    normalized = [str(value).strip() for value in values if str(value).strip()]
    return normalized or [default]


def set_solver_build_context(variant: SolverBuildVariant) -> None:
    global COMPILER
    global ARCH
    global OPTMZ
    global COMBINATION
    global EXTRA_COMPILER_OPTIONS
    global CURRENT_VARIANT

    CURRENT_VARIANT = variant
    COMPILER, ARCH, OPTMZ = variant.combination
    COMBINATION = variant.combination
    EXTRA_COMPILER_OPTIONS = dict(variant.extra_compiler_options)
    logger.info(
        "Auto-resolve active build variant: compiler=%s arch=%s opti=%s "
        "flags=%s extra=%s",
        COMPILER,
        ARCH,
        OPTMZ,
        variant.flag_bits,
        json.dumps(EXTRA_COMPILER_OPTIONS, ensure_ascii=False, sort_keys=True),
    )


def initialize_solver_build_context(source_config_path: str) -> None:
    global COMPILER
    global ARCH
    global OPTMZ
    global COMBINATION
    global EXTRA_COMPILER_OPTIONS
    global BUILD_VARIANTS

    def configure_variants(build_cfg: dict[str, Any]) -> None:
        global BUILD_VARIANTS
        compiler_list = _get_build_values(build_cfg, "compiler", COMPILER)
        arch_list = _get_build_values(build_cfg, "arch", ARCH)
        optmz_list = _get_build_values(build_cfg, "opti", OPTMZ)

        option_sets = compiler_option_sets(build_cfg)

        BUILD_VARIANTS = [
            SolverBuildVariant(compiler, arch, optmz, options, mode)
            for compiler, arch, optmz, (mode, options) in product(
                compiler_list, arch_list, optmz_list, option_sets
            )
        ]

    if not source_config_path or not os.path.exists(source_config_path):
        configure_variants({})
        set_solver_build_context(BUILD_VARIANTS[0])
        return

    cfg = load_yaml_safe(source_config_path) or {}
    build_cfg = cfg.get("build", {})
    if not isinstance(build_cfg, dict):
        configure_variants({})
        set_solver_build_context(BUILD_VARIANTS[0])
        return

    configure_variants(build_cfg)

    logger.info(
        "Auto-resolve configuration variants: %d variant(s): %s",
        len(BUILD_VARIANTS),
        ", ".join(
            f"{variant.compiler}/{variant.arch}/{variant.optmz}/flags-{variant.flag_bits}"
            for variant in BUILD_VARIANTS
        ),
    )
    set_solver_build_context(BUILD_VARIANTS[0])


def get_version_from_url(project_name: str, url: str) -> str:
    version_pattern = r"([0-9]+\.[0-9]+(?:\.[0-9]+)?)"
    version_match = re.search(version_pattern, url)
    if version_match:
        return version_match.group(1)
    else:
        logger.info(f"{project_name}'s version is not match.")
        logger.info(f"  URL: {url}")
        logger.info(f"  Set version as \"0.0.0\"")
        return "0.0.0"


def save_project_to_file(project: Project, filepath: str):
    data = {}
    for key in SAVE_KEYS:
        value = getattr(project, key, None)
        if value is None:
            value = [] if key == "dep_list" else ""
        data[key] = value
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_project_from_file(project: Project, filepath: str) -> Project:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    for key in SAVE_KEYS:
        if key in data:
            setattr(project, key, data[key])
        else:
            if key == "dep_list":
                setattr(project, key, [])
            else:
                setattr(project, key, "")
    return project

class DepNode:
    project: Project
    children: list[DepNode]
    status: str
    parent: DepNode | None
    failed_stage: str
    id : str
    llm_repair_iterations: int

    def __init__(self, project: Project, parent=None, id=''):
        self.project = project
        self.children: list[DepNode] = []
        self.status = "pending" # pending | building | failed | success
        self.parent = parent
        self.failed_stage = ''
        self.id = id
        self.llm_repair_iterations = 0

    def __str__(self):
        return self.project.name


#! ============================ Utils ==========================================

def save_process_log(
    content: str,
    project_name: str = "",
    action: str = "",
    status: str = "",
    stage: str = "",
):
    mkdir(PROCESS_LOG_DIR, exist_ok=True)
    idx = len(os.listdir(PROCESS_LOG_DIR)) + 1

    parts = [f"{idx:02d}", project_name, f"{action}"]
    if not project_name:
        parts = [f"{idx:02d}", f"{action}"]

    if status:
        parts.append(f"{status}")
    if stage:
        parts.append(f"{stage}")

    filename = "_".join(parts) + ".log"
    path = os.path.join(PROCESS_LOG_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _collect_relative_files(root_path: str, limit: int = 64) -> list[str]:
    if not os.path.isdir(root_path):
        return []

    files: list[str] = []
    for dirpath, _, filenames in os.walk(root_path):
        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(full_path, root_path)
            files.append(rel_path)

    files.sort()
    return files[:limit]


def _collect_library_entries(lib_dir: str, limit: int = 64) -> list[str]:
    if not os.path.isdir(lib_dir):
        return []

    names: list[str] = []
    for name in sorted(os.listdir(lib_dir)):
        path = os.path.join(lib_dir, name)
        if not os.path.isfile(path):
            continue
        if ".so" in name or name.endswith(".a") or name.endswith(".la"):
            names.append(name)

    return names[:limit]


def _readelf_needed_entries(so_path: str) -> list[str]:
    try:
        output = subprocess.check_output(
            ["readelf", "-d", so_path],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []

    needed: list[str] = []
    for line in output.splitlines():
        if "(NEEDED)" not in line:
            continue
        parts = line.split("[", 1)
        if len(parts) < 2 or "]" not in parts[1]:
            continue
        soname = parts[1].split("]", 1)[0].strip()
        if soname and soname not in needed:
            needed.append(soname)
    return needed


def build_attached_dependency_inventory(project: Project, arch: str) -> list[dict[str, Any]]:
    dep_list = list(project.dep_list or [])
    dep_root = project.dep_path

    if not dep_root or not dep_list:
        return []

    inventory: list[dict[str, Any]] = []
    for dep_name in dep_list:
        prefix = os.path.join(dep_root, "install", arch, dep_name)
        include_dir = os.path.join(prefix, "include")
        lib_dir = os.path.join(prefix, "lib")
        pkgconfig_dir = os.path.join(lib_dir, "pkgconfig")

        item: dict[str, Any] = {
            "name": dep_name,
            "prefix": prefix,
            "headers": _collect_relative_files(include_dir),
            "libraries": _collect_library_entries(lib_dir),
            "pkgconfig": _collect_relative_files(pkgconfig_dir),
            "needed": {},
        }

        for lib_name in item["libraries"]:
            if ".so" not in lib_name:
                continue
            so_path = os.path.join(lib_dir, lib_name)
            needed = _readelf_needed_entries(so_path)
            if needed:
                item["needed"][lib_name] = needed

        inventory.append(item)

    return inventory


def render_attached_dependency_inventory(project: Project, arch: str) -> str:
    inventory = build_attached_dependency_inventory(project, arch)
    if not inventory:
        return ""
    payload = {"attached_dependencies": inventory}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def get_solver_root(base_path: str) -> str:
    return os.path.join(base_path, SOLVER_DIRNAME)


def get_solver_state_path(base_path: str) -> str:
    return os.path.join(get_solver_root(base_path), STATE_FILENAME)


def get_solver_merge_path(base_path: str) -> str:
    return os.path.join(get_solver_root(base_path), MERGE_FILENAME)


def get_solver_merged_config_path(base_path: str) -> str:
    if SOURCE_CONFIG_PATH:
        source_dir = os.path.dirname(SOURCE_CONFIG_PATH)
        return os.path.join(source_dir, MERGED_CONFIG_FILENAME)
    return os.path.join(get_solver_root(base_path), MERGED_CONFIG_FILENAME)


def get_solver_trials_dir(base_path: str) -> str:
    return os.path.join(get_solver_root(base_path), TRIALS_DIRNAME)


def ensure_solver_dirs(base_path: str) -> None:
    mkdir(get_solver_root(base_path), exist_ok=True)
    mkdir(get_solver_trials_dir(base_path), exist_ok=True)


def initialize_solver_state(base_path: str, targets: list[str]) -> None:
    ensure_solver_dirs(base_path)
    trials_dir = get_solver_trials_dir(base_path)
    for name in os.listdir(trials_dir):
        if name.endswith(".jsonl"):
            os.remove(os.path.join(trials_dir, name))

    merge_path = get_solver_merge_path(base_path)
    merged_config_path = get_solver_merged_config_path(base_path)
    if os.path.exists(merge_path):
        os.remove(merge_path)
    if os.path.exists(merged_config_path):
        os.remove(merged_config_path)

    state = {
        "source_config": SOURCE_CONFIG_PATH,
        "generated_at": datetime.datetime.now().isoformat(),
        "max_iter": MAX_LLM_REPAIR_ITERATIONS,
        "dependency_source_lookup": DEPENDENCY_SOURCE_LOOKUP_ENABLED,
        "build_variants": [
            {
                "compiler": variant.compiler,
                "arch": variant.arch,
                "opti": variant.optmz,
                "option_mode": variant.option_mode or None,
                "flag_set": variant.flag_bits,
                "extra_compiler_options": dict(variant.extra_compiler_options),
            }
            for variant in BUILD_VARIANTS
        ],
        "targets": {
            target: {
                "status": "pending",
                "attempts": 0,
                "last_decision": "",
                "resolve_file": "",
                "merge_candidate": False,
            }
            for target in targets
        },
    }
    with open(get_solver_state_path(base_path), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    refresh_merged_config(base_path)


def load_solver_state(base_path: str) -> dict[str, Any]:
    state_path = get_solver_state_path(base_path)
    if not os.path.exists(state_path):
        return {"source_config": SOURCE_CONFIG_PATH, "targets": {}}
    with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f)


def update_solver_state(base_path: str, project_name: str, **changes: Any) -> None:
    state = load_solver_state(base_path)
    targets = state.setdefault("targets", {})
    project_state = targets.setdefault(
        project_name,
        {
            "status": "pending",
            "attempts": 0,
            "last_decision": "",
            "resolve_file": "",
            "merge_candidate": False,
        },
    )
    project_state.update(changes)
    with open(get_solver_state_path(base_path), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def append_solver_trial(base_path: str, project_name: str, payload: dict[str, Any]) -> None:
    ensure_solver_dirs(base_path)
    trial_path = os.path.join(get_solver_trials_dir(base_path), f"{project_name}.jsonl")
    record = {
        "timestamp": datetime.datetime.now().isoformat(),
        **payload,
    }
    with open(trial_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def get_mergeable_deps(base_path: str, dep_names: list[str]) -> list[str]:
    state = load_solver_state(base_path)
    targets = state.get("targets", {})
    mergeable: list[str] = []

    for dep_name in dep_names:
        dep_state = targets.get(dep_name)
        if dep_state is None:
            mergeable.append(dep_name)
            continue
        if dep_state.get("status") == "success":
            mergeable.append(dep_name)

    return mergeable


def get_project_source_config(project: Project) -> dict[str, Any]:
    source_type = (project.source_type or "").strip()
    source_value = (project.source_value or "").strip()
    if not source_type or not source_value:
        return {}

    source: dict[str, Any] = {
        "type": source_type,
        "value": source_value,
    }
    archive: dict[str, Any] = {}
    for attr_name, key in (
        ("archive_group", "group"),
        ("archive_name", "name"),
        ("archive_md5", "md5"),
    ):
        value = getattr(project, attr_name, None)
        if value:
            archive[key] = value
    if archive:
        source["archive"] = archive
    return source


def build_merge_candidate_entry(base_path: str, project: Project) -> dict[str, Any]:
    entry: dict[str, Any] = {}

    version = normalize_version(getattr(project, "version", None))
    if version and not (project.source_type == "apt" and version == "0.0.0"):
        entry["version"] = version

    source = get_project_source_config(project)
    if source:
        entry["source"] = source

    entry["deps"] = get_mergeable_deps(base_path, list(project.dep_list or []))
    entry["options"] = split_config_tokens(project.conf_options)
    entry["cflags"] = split_config_tokens(project.cflags)
    entry["ldflags"] = split_config_tokens(project.ldflags)
    return entry


def write_merge_candidate(base_path: str, project: Project) -> None:
    ensure_solver_dirs(base_path)
    merge_path = get_solver_merge_path(base_path)
    if os.path.exists(merge_path):
        with open(merge_path, "r", encoding="utf-8") as f:
            merge_data = yaml.safe_load(f) or {}
    else:
        merge_data = {}

    projects = merge_data.setdefault("projects", {})
    projects[project.name] = build_merge_candidate_entry(base_path, project)
    merge_data["source_config"] = SOURCE_CONFIG_PATH
    merge_data["generated_at"] = datetime.datetime.now().isoformat()

    with open(merge_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(merge_data, f, sort_keys=False, allow_unicode=True)
    refresh_merged_config(base_path)


def iter_dep_tree_nodes(node: DepNode):
    yield node
    for child in node.children:
        yield from iter_dep_tree_nodes(child)


def write_merge_candidates_for_tree(base_path: str, root_node: DepNode) -> None:
    ensure_solver_dirs(base_path)
    merge_path = get_solver_merge_path(base_path)
    if os.path.exists(merge_path):
        with open(merge_path, "r", encoding="utf-8") as f:
            merge_data = yaml.safe_load(f) or {}
    else:
        merge_data = {}

    projects = merge_data.setdefault("projects", {})
    for node in iter_dep_tree_nodes(root_node):
        if node.status != "success":
            continue
        projects[node.project.name] = build_merge_candidate_entry(base_path, node.project)

    merge_data["source_config"] = SOURCE_CONFIG_PATH
    merge_data["generated_at"] = datetime.datetime.now().isoformat()

    with open(merge_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(merge_data, f, sort_keys=False, allow_unicode=True)
    refresh_merged_config(base_path)


def load_yaml_safe(file_path: str) -> Any:
    with open(file_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _ensure_project_mapping(cfg: dict[str, Any], project_name: str) -> dict[str, Any]:
    projects = cfg.setdefault("projects", {})
    current = projects.get(project_name)
    if not isinstance(current, dict):
        current = {}
    projects[project_name] = current
    return current


def refresh_merged_config(base_path: str) -> None:
    if not SOURCE_CONFIG_PATH:
        return
    if not os.path.exists(SOURCE_CONFIG_PATH):
        logger.warning(f"Source config not found for merged config: {SOURCE_CONFIG_PATH}")
        return

    ensure_solver_dirs(base_path)
    merged_config_path = get_solver_merged_config_path(base_path)
    source_cfg = load_yaml_safe(SOURCE_CONFIG_PATH) or {}
    merged_cfg = copy.deepcopy(source_cfg)

    merge_path = get_solver_merge_path(base_path)
    merge_data: dict[str, Any] = {}
    if os.path.exists(merge_path):
        merge_data = load_yaml_safe(merge_path) or {}

    for project_name, patch in (merge_data.get("projects", {}) or {}).items():
        if not isinstance(patch, dict):
            continue
        project_meta = _ensure_project_mapping(merged_cfg, project_name)
        source = patch.get("source")
        if isinstance(source, dict) and source.get("type") and source.get("value"):
            project_meta["source"] = copy.deepcopy(source)

        version = patch.get("version")
        if version:
            project_meta["version"] = version

        for key in ("deps", "options", "cflags", "ldflags"):
            values = patch.get(key, [])
            if values:
                project_meta[key] = list(values)
            else:
                project_meta.pop(key, None)

    merged_cfg["_solver_merge"] = {
        "source_config": SOURCE_CONFIG_PATH,
        "generated_at": datetime.datetime.now().isoformat(),
        "merge_candidates": os.path.basename(merge_path),
    }

    with open(merged_config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(merged_cfg, f, sort_keys=False, allow_unicode=True)


def get_root_base_path(node: DepNode) -> str:
    while node.parent is not None:
        node = node.parent
    assert node.project.dep_path is not None
    return node.project.dep_path


def get_solver_base_path(node: DepNode) -> str:
    while node.parent is not None:
        node = node.parent
    return node.project.base_path


def get_project_repair_iterations(root_node: DepNode) -> int:
    return root_node.llm_repair_iterations


def increment_project_repair_iterations(root_node: DepNode) -> int:
    root_node.llm_repair_iterations += 1
    return root_node.llm_repair_iterations


def set_solver_max_iter(max_iter: int) -> None:
    global MAX_LLM_REPAIR_ITERATIONS
    if max_iter < 1:
        raise ValueError("LLM max_iter must be >= 1")
    MAX_LLM_REPAIR_ITERATIONS = max_iter


def set_dependency_source_lookup(enabled: bool) -> None:
    global DEPENDENCY_SOURCE_LOOKUP_ENABLED
    DEPENDENCY_SOURCE_LOOKUP_ENABLED = enabled


def find_first_failed_stage(project: Project, build_result: dict) -> str :
    """Return the name of the first failed build stage, or None if all succeeded."""
    stages = ["configure", "make", "install"]
    if project.use_cmake:
        stages = ["cmake", "make", "install"]

    for stage in stages:
        if not build_result.get(stage, {}).get("success", True):
            return stage
    return ''


def check_url_exists(url: str) -> bool:
    try:
        resp = requests.head(url, allow_redirects=True, timeout=10)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def check_apt_source_exists(package_name: str) -> bool:
    source_name = get_apt_source_package_from_showsrc(package_name)
    return source_name is not None


def get_apt_source_package_from_showsrc(package_name: str) -> str | None:
    package_name = package_name.strip()
    if not package_name:
        return None
    result = execute_command(
        f"apt-cache showsrc {shlex.quote(package_name)}",
        DEBUG_LEVEL=-1,
        exit_on_error=False,
    )
    if result.returncode != 0:
        return None

    match = re.search(r"(?m)^Package:\s*(\S+)\s*$", result.stdout or "")
    if not match:
        return None
    return match.group(1).strip()


def normalize_apt_source_field(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    # apt-cache show may print "Source: foo (= 1.2.3)".
    return re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()


def get_apt_source_package_from_binary(binary_name: str) -> str | None:
    binary_name = binary_name.strip()
    if not binary_name:
        return None
    result = execute_command(
        f"apt-cache show {shlex.quote(binary_name)}",
        DEBUG_LEVEL=-1,
        exit_on_error=False,
    )
    if result.returncode != 0:
        return None

    for stanza in (result.stdout or "").split("\n\n"):
        source_name = ""
        package_name = ""
        for line in stanza.splitlines():
            if line.startswith("Source:"):
                source_name = normalize_apt_source_field(line.split(":", 1)[1])
            elif line.startswith("Package:"):
                package_name = line.split(":", 1)[1].strip()
        candidate = source_name or package_name
        resolved = get_apt_source_package_from_showsrc(candidate)
        if resolved:
            return resolved
    return None


def search_apt_binary_packages(term: str, limit: int = 20) -> list[str]:
    term = term.strip()
    if not term:
        return []
    result = execute_command(
        f"apt-cache search --names-only {shlex.quote(term)}",
        DEBUG_LEVEL=-1,
        exit_on_error=False,
    )
    if result.returncode != 0:
        return []

    packages: list[str] = []
    for line in (result.stdout or "").splitlines():
        package_name = line.split(" - ", 1)[0].split()[0].strip()
        if package_name and package_name not in packages:
            packages.append(package_name)
        if len(packages) >= limit:
            break
    return packages


def lookup_apt_source_for_dependency(dep_name: str, proposed_source_value: str = "") -> str | None:
    raw_candidates = [
        proposed_source_value,
        dep_name,
        f"{dep_name}-dev",
    ]
    if dep_name and not dep_name.startswith("lib"):
        raw_candidates.extend([f"lib{dep_name}", f"lib{dep_name}-dev"])

    candidates: list[str] = []
    for candidate in raw_candidates:
        candidate = candidate.strip()
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        source_name = get_apt_source_package_from_showsrc(candidate)
        if source_name:
            return source_name

    for candidate in candidates:
        source_name = get_apt_source_package_from_binary(candidate)
        if source_name:
            return source_name

    for candidate in candidates:
        for binary_package in search_apt_binary_packages(candidate):
            source_name = get_apt_source_package_from_binary(binary_package)
            if source_name:
                return source_name

    return None


def normalize_dep_info_with_source_lookup(dep_info: Any) -> Any:
    if isinstance(dep_info, str):
        dep_name = dep_info.strip()
        apt_source = lookup_apt_source_for_dependency(dep_name)
        if apt_source:
            logger.info(
                "[APT lookup] Resolved dependency %s to apt source package %s",
                dep_name,
                apt_source,
            )
            return {
                "name": dep_name,
                "source": {
                    "type": "apt",
                    "value": apt_source,
                },
            }
        return dep_info

    if not isinstance(dep_info, dict):
        return dep_info

    dep_name = str(dep_info.get("name", "")).strip()
    source = dep_info.get("source", {})
    source_type = ""
    source_value = ""
    if isinstance(source, dict):
        source_type = str(source.get("type", "")).strip()
        source_value = str(source.get("value", "")).strip()

    apt_source = lookup_apt_source_for_dependency(
        dep_name,
        source_value if source_type == "apt" else "",
    )
    if not apt_source:
        return dep_info

    if source_type != "apt" or source_value != apt_source:
        logger.info(
            "[APT lookup] Resolved dependency %s source %s:%s to apt source package %s",
            dep_name,
            source_type or "<missing>",
            source_value or "<missing>",
            apt_source,
        )

    normalized = copy.deepcopy(dep_info)
    normalized["source"] = {
        "type": "apt",
        "value": apt_source,
    }
    return normalized

def check_git_remote_exists(url: str) -> bool:
    if not url.strip():
        return False
    result = execute_command(
        f"git ls-remote {shlex.quote(url)} HEAD",
        DEBUG_LEVEL=-1,
        exit_on_error=False,
    )
    return result.returncode == 0 and bool((result.stdout or "").strip())


def validate_project_source(project: Project) -> tuple[bool, str]:
    source_type = (project.source_type or "").strip()
    source_value = (project.source_value or "").strip()

    if source_type == "apt":
        ok = check_apt_source_exists(source_value or project.name)
        detail = source_value or project.name
        return ok, f"apt source {detail}"
    if source_type == "url":
        ok = check_url_exists(source_value)
        return ok, source_value
    if source_type == "url_git":
        ok = check_git_remote_exists(source_value)
        return ok, source_value

    return False, f"unsupported source_type={source_type}"


def extract_failed_probe_markers(failed_stage_log: str, limit: int = 3) -> list[str]:
    markers: list[str] = []
    for line in failed_stage_log.splitlines():
        stripped = line.strip()
        if "checking " not in stripped:
            continue
        marker = stripped[stripped.find("checking "):]
        if "..." in marker:
            marker = marker.split("...", 1)[0]
        marker = marker.rstrip(".: ")
        if marker and marker not in markers:
            markers.append(marker)

    return markers[-limit:]


def find_configure_file(source_path: str) -> str:
    if not source_path or not os.path.isdir(source_path):
        return ""

    for root, _, files in os.walk(source_path):
        if "configure" in files:
            return os.path.join(root, "configure")
    return ""


def extract_configure_evidence(
    configure_file_path: str,
    markers: list[str],
    *,
    context: int = 6,
    max_snippets: int = 1,
) -> tuple[str, list[str]]:
    if not configure_file_path or not os.path.exists(configure_file_path):
        return "", []

    try:
        with open(configure_file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return "", []

    lower_lines = [line.lower() for line in lines]
    snippets: list[str] = []
    found_vars: list[str] = []
    seen_ranges: set[tuple[int, int]] = set()

    for marker in reversed(markers):
        marker_lower = marker.lower()
        for idx, lower_line in enumerate(lower_lines):
            if marker_lower not in lower_line:
                continue

            start = max(0, idx - context)
            end = min(len(lines), idx + context + 1)
            range_key = (start, end)
            if range_key in seen_ranges:
                continue
            seen_ranges.add(range_key)

            snippet = "".join(
                f"{line_no + 1}: {lines[line_no]}"
                for line_no in range(start, end)
            )
            snippets.append(snippet)

            snippet_text = "".join(lines[idx:end])
            for match in re.findall(r"\b(?:ac_cv|gl_cv|utils_cv)_[A-Za-z0-9_]+\b", snippet_text):
                if match not in found_vars:
                    found_vars.append(match)

            if len(snippets) >= max_snippets:
                return "\n---\n".join(snippets), found_vars

    return "\n---\n".join(snippets), found_vars


def extract_config_log_cache_vars(config_log_lines: list[str], markers: list[str], lookahead: int = 500) -> list[str]:
    if not config_log_lines:
        return []

    lower_lines = [line.lower() for line in config_log_lines]

    for marker in reversed(markers):
        marker_lower = marker.lower()
        marker_tokens = [
            token
            for token in re.findall(r"[a-z0-9_]+", marker_lower)
            if token not in {"checking", "whether", "for", "the", "a", "an"}
        ]
        matched_index = -1
        for idx, line in enumerate(lower_lines):
            if marker_lower in line:
                matched_index = idx
        if matched_index < 0:
            continue

        window_text = "".join(config_log_lines[matched_index:min(len(config_log_lines), matched_index + lookahead)])
        raw_matches = re.findall(r"\b(?:ac_cv|gl_cv|utils_cv)_[A-Za-z0-9_]+\b", window_text)
        scored: list[tuple[int, str]] = []
        for match in raw_matches:
            score = sum(1 for token in marker_tokens if token in match.lower())
            if score > 0:
                scored.append((score, match))

        if scored:
            best_score = max(score for score, _ in scored)
            best_matches: list[str] = []
            for score, match in scored:
                if score == best_score and match not in best_matches:
                    best_matches.append(match)
            return best_matches

    return []


def query(
    project: Project,
    log_path: str,
    prefix: str,
    build_stage: str,
    project_name: str,
    solver_base_path: str,
    max_retries: int = 3,
    model: str = DEFAULT_MODEL,
    temperature: float | None = None
) -> dict:
    log_file = os.path.join(log_path, f"{prefix}_{build_stage}.log" if build_stage in (("configure", "cmake")) else f"{prefix}_{build_stage}.error")
    rc_file = os.path.join(log_path, f"{prefix}_{build_stage}.returncode")
    llm_log_dir = os.path.join(log_path, "llm_log")
    mkdir(llm_log_dir, exist_ok=True)

    if not os.path.exists(rc_file):
        logger.error(f"Return code file not found: {rc_file}")
        return {}

    with open(rc_file, "r", encoding="utf-8") as f:
        return_code = int(f.read().strip())

    help_file = os.path.join(
        project.log_path,
        f"{project.name}.configure_help" if project.use_configure else f"{project.name}.cmake_help",
    )
    phase_map = {
        "configure": "configuration",
        "cmake": "configuration",
        "make": "compilation",
        "install": "installation",
    }
    phase = phase_map.get(build_stage, build_stage)
    current_solution_json = json.dumps(export_project_solution_state(project), ensure_ascii=False, indent=2)
    llm_input_log = log_file
    evidence_vars: list[str] = []
    attached_dep_inventory_json = render_attached_dependency_inventory(project, ARCH)

    if build_stage in (("configure", "cmake")):
        failed_stage_log = ""
        configure_evidence = ""
        markers: list[str] = []
        config_log_path = os.path.join(log_path, f"{prefix}_config.log")

        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                failed_stage_log = f.read()
        except OSError:
            failed_stage_log = ""

        if failed_stage_log:
            markers = extract_failed_probe_markers(failed_stage_log)

        configure_file_path = getattr(project, "configure_file_path", "")
        if not configure_file_path:
            candidate_roots = [
                getattr(project, "source_path", ""),
                os.path.join(project.base_path, "sources", project.name),
            ]
            for root in candidate_roots:
                configure_file_path = find_configure_file(root)
                if configure_file_path:
                    break
        if failed_stage_log and configure_file_path:
            markers = extract_failed_probe_markers(failed_stage_log)
            configure_evidence, evidence_vars = extract_configure_evidence(
                configure_file_path,
                markers,
            )

        if os.path.exists(config_log_path):
            try:
                with open(config_log_path, "r", encoding="utf-8", errors="replace") as f:
                    config_log_lines = f.readlines()
                config_tail = "".join(config_log_lines[-400:])
                config_log_vars = extract_config_log_cache_vars(config_log_lines, markers)
                for item in config_log_vars:
                    if item not in evidence_vars:
                        evidence_vars.append(item)
                if evidence_vars:
                    logger.info(
                        "[LLM] Exact cache-variable candidates for %s: %s",
                        project.name,
                        ", ".join(evidence_vars),
                    )
                llm_input_log = os.path.join(llm_log_dir, f"{project_name}_{build_stage}_llm_input.log")
                with open(llm_input_log, "w", encoding="utf-8") as f:
                    f.write("=== FAILED STAGE LOG ===\n")
                    f.write(failed_stage_log)
                    f.write("\n\n=== CONFIG.LOG TAIL ===\n")
                    f.write(config_tail)
                    if configure_evidence:
                        f.write("\n\n=== CONFIGURE SCRIPT EVIDENCE ===\n")
                        f.write(configure_evidence)
                    if evidence_vars:
                        f.write("\n\n=== EVIDENCE-BACKED CACHE VARIABLES ===\n")
                        for item in evidence_vars:
                            f.write(f"{item}\n")
                    if attached_dep_inventory_json:
                        f.write("\n\n=== ATTACHED DEPENDENCY INVENTORY JSON ===\n")
                        f.write(attached_dep_inventory_json)
            except OSError:
                llm_input_log = log_file

    for attempt in range(max_retries):
        print(f"[DEBUG] LLM query attempt {attempt + 1}...")

        instruction_payload: dict[str, object] = {}
        if attached_dep_inventory_json:
            try:
                instruction_payload.update(json.loads(attached_dep_inventory_json))
            except json.JSONDecodeError:
                instruction_payload["attached_dependencies_raw"] = attached_dep_inventory_json
        if evidence_vars:
            instruction_payload["evidence_cache_variables"] = evidence_vars
        extra_instruction = (
            json.dumps(instruction_payload, ensure_ascii=False, indent=2)
            if instruction_payload
            else ""
        )

        result = analyze_build_error_once(
            prompt_path=PROMPT_PATH
            if DEPENDENCY_SOURCE_LOOKUP_ENABLED
            else PROMPT_NO_DEP_LOOKUP_PATH,
            project_name=project.name,
            project_version=project.version or "0.0.0",
            phase=phase,
            error_log_path=Path(llm_input_log),
            build_options_help_path=Path(help_file),
            failed_solution_json=current_solution_json,
            instructions=extra_instruction,
            return_code=return_code,
            model=model,
            temperature=temperature,
        )

        decision = result.get("decision", {})
        decision_type = str(decision.get("type", "")).strip().lower() if isinstance(decision, dict) else ""
        solution = result.get("solution", {})
        option_list = solution.get("options", []) if isinstance(solution, dict) else []
        if decision_type == "options" and evidence_vars:
            invalid_cache_vars: list[str] = []
            for option in option_list:
                if not isinstance(option, str):
                    continue
                option_name = option.split("=", 1)[0].strip()
                if re.match(r"^(?:ac_cv|gl_cv|utils_cv)_[A-Za-z0-9_]+$", option_name) and option_name not in evidence_vars:
                    invalid_cache_vars.append(option_name)

            if invalid_cache_vars:
                logger.warning(
                    "LLM proposed cache variables not backed by evidence: %s",
                    ", ".join(invalid_cache_vars),
                )
                append_solver_trial(
                    solver_base_path,
                    project.name,
                    {
                        "kind": "llm_rejected",
                        "stage": build_stage,
                        "reason": "invalid_cache_var",
                        "invalid_cache_vars": invalid_cache_vars,
                        "allowed_cache_vars": evidence_vars,
                        "result": result,
                    },
                )
                continue

        if not DEPENDENCY_SOURCE_LOOKUP_ENABLED and decision_type == "deps":
            invalid_dep_sources = get_invalid_dep_sources_without_lookup(result)
            if invalid_dep_sources:
                logger.warning(
                    "LLM proposed dependency sources that are invalid when source lookup is disabled: %s",
                    invalid_dep_sources,
                )
                append_solver_trial(
                    solver_base_path,
                    project.name,
                    {
                        "kind": "llm_rejected",
                        "stage": build_stage,
                        "reason": "dependency_source_lookup_disabled_requires_url",
                        "invalid_dep_sources": invalid_dep_sources,
                        "result": result,
                    },
                )
                continue

        # get num of files
        out_path = os.path.join(llm_log_dir, f"openai_response_{project_name}_{len(os.listdir(llm_log_dir)) + 1}_{build_stage}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        logger.info(f"LLM analysis saved to: {out_path}")
        append_solver_trial(
            solver_base_path,
            project.name,
            {
                "kind": "llm_result",
                "stage": build_stage,
                "result": result,
                "llm_meta": result.get("_llm_meta", {}),
            },
        )

        return result

    return {}


#! =============================================================================

def compile_project_wrapper(node: DepNode):
    plist = [node.project]
    analyze_projects(plist)
    node.project = plist[0]

    project = node.project

    mkdir(project.log_path, exist_ok=True)

    is_dep_node = node.parent is not None
    if is_dep_node:
        install_prefix = os.path.join(project.base_path, "install", ARCH, project.name)
        mkdir(install_prefix, exist_ok=True)
        project.install_prefix = install_prefix

    if project.resolve_file_path is not None:
        project = load_project_from_file(project, project.resolve_file_path)

    # compile_project's traditional prefix contains only compiler/arch/opti.
    # Auto-resolve mode matrices add this tag so 000 and 111 artifacts do not
    # overwrite one another. A single fixed option set retains old names.
    project.variant_tag = CURRENT_VARIANT.file_tag

    assert project.cflags is not None
    assert project.ldflags is not None
    assert project.conf_options is not None

    path_env = get_env_variable("PATH")
    compile_kwargs = {
        "EXTRA_DEP": is_dep_node,
        "cflags": project.cflags,
        "ldflags": project.ldflags,
        "conf_options": project.conf_options,
    }
    with guarded_autotools_probe(
        project,
        COMBINATION,
        path_env,
        EXTRA_COMPILER_OPTIONS,
        compile_kwargs=compile_kwargs,
    ) as probe:
        if probe is None:
            return compile_project(
                project=project,
                comb=COMBINATION,
                PATH=path_env,
                extra_compiler_options=EXTRA_COMPILER_OPTIONS,
                **compile_kwargs,
            )
        return probe.success


def analyze_build_result(project: Project, log_path: str, prefix: str) -> dict:
    stages = ["configure", "make", "install"]
    if project.use_cmake:
        stages = ["cmake", "make", "install"]
    result = {}
    overall_success = True
    for stage in stages:
        returncode_file = os.path.join(log_path, f"{prefix}_{stage}.returncode")
        # If file is missing, treat as failure
        if not os.path.exists(returncode_file):
            rc = -1
            success = False
        else:
            with open(returncode_file, "r", encoding="utf-8") as f:
                rc_str = f.read().strip()
            try:
                rc = int(rc_str)
                success = (rc == 0)
            except ValueError:
                rc = -1
                success = False
        result[stage] = {
            "returncode": rc,
            "success": success
        }
        if not success:
            overall_success = False
    result["overall_success"] = overall_success
    return result


def split_config_tokens(value: str | None) -> list[str]:
    if not value:
        return []
    return [token for token in shlex.split(value) if token.strip()]


def export_project_solution_state(project: Project) -> dict[str, Any]:
    return {
        "project": {
            "name": project.name,
            "version": project.version or "0.0.0",
        },
        "decision": {
            "type": "none",
            "reason": "Current accumulated solution before this retry.",
        },
        "solution": {
            "deps": list(project.dep_list or []),
            "options": split_config_tokens(project.conf_options),
            "cflags": split_config_tokens(project.cflags),
            "ldflags": split_config_tokens(project.ldflags),
        },
    }


def normalize_llm_decision_type(llm_result: dict[str, Any]) -> str:
    decision = llm_result.get("decision", {})
    if not isinstance(decision, dict):
        return "none"
    raw_type = str(decision.get("type", "none")).strip().lower()
    aliases = {
        "dependency": "deps",
        "dependencies": "deps",
        "feature": "options",
        "configure_options": "options",
        "option": "options",
        "none": "none",
        "deps": "deps",
        "options": "options",
        "cflags": "cflags",
        "ldflags": "ldflags",
    }
    return aliases.get(raw_type, "none")


def get_llm_solution(llm_result: dict[str, Any]) -> dict[str, list[Any]]:
    solution = llm_result.get("solution", {})
    if not isinstance(solution, dict):
        solution = {}
    return {
        "deps": list(solution.get("deps", [])),
        "options": list(solution.get("options", [])),
        "cflags": list(solution.get("cflags", [])),
        "ldflags": list(solution.get("ldflags", [])),
    }


def get_invalid_dep_sources_without_lookup(llm_result: dict[str, Any]) -> list[dict[str, str]]:
    invalid: list[dict[str, str]] = []
    for dep_info in get_llm_solution(llm_result)["deps"]:
        if isinstance(dep_info, str):
            invalid.append(
                {
                    "name": dep_info.strip(),
                    "source_type": "<missing>",
                    "source_value": "<missing>",
                    "reason": "dependency_source_lookup_disabled_requires_url",
                }
            )
            continue

        if not isinstance(dep_info, dict):
            invalid.append(
                {
                    "name": "<invalid>",
                    "source_type": type(dep_info).__name__,
                    "source_value": "<invalid>",
                    "reason": "dependency_payload_is_not_an_object",
                }
            )
            continue

        dep_name = str(dep_info.get("name", "")).strip()
        source = dep_info.get("source", {})
        source_type = ""
        source_value = ""
        if isinstance(source, dict):
            source_type = str(source.get("type", "")).strip()
            source_value = str(source.get("value", "")).strip()

        if source_type not in ("url", "url_git") or not source_value:
            invalid.append(
                {
                    "name": dep_name,
                    "source_type": source_type or "<missing>",
                    "source_value": source_value or "<missing>",
                    "reason": "dependency_source_lookup_disabled_requires_url",
                }
            )
    return invalid


def append_unique_tokens(existing: str | None, new_tokens: list[str]) -> tuple[str, list[str]]:
    merged = split_config_tokens(existing)
    added: list[str] = []
    for token in new_tokens:
        token = token.strip()
        if not token or token in merged:
            continue
        merged.append(token)
        added.append(token)
    return " ".join(merged), added


def current_log_prefix(project_name: str) -> str:
    prefix = f"{project_name}_{COMPILER}_{ARCH}_{OPTMZ}"
    if CURRENT_VARIANT.file_tag:
        prefix = f"{prefix}_{CURRENT_VARIANT.file_tag}"
    return prefix


def current_variant_metadata() -> dict[str, Any]:
    return {
        "compiler": COMPILER,
        "arch": ARCH,
        "opti": OPTMZ,
        "option_mode": CURRENT_VARIANT.option_mode or None,
        "flag_set": CURRENT_VARIANT.flag_bits,
        "extra_compiler_options": dict(EXTRA_COMPILER_OPTIONS),
    }


def has_successful_build_for_current_variant(project: Project) -> bool:
    prefix = current_log_prefix(project.name)
    result = analyze_build_result(project, project.log_path, prefix)
    return bool(result.get("overall_success"))


def ensure_known_children_for_current_variant(node: DepNode, tree: Tree, root_node: DepNode) -> bool:
    for child in list(node.children):
        if has_successful_build_for_current_variant(child.project):
            child.status = "success"
            continue

        logger.info(
            "[*] Rebuilding known dependency for current variant: %s (%s/%s/%s)",
            child.project.name,
            COMPILER,
            ARCH,
            OPTMZ,
        )
        success = resolve_node(child, tree, root_node)
        curr_dep_tree = print_dependency_tree_status(root_node)
        save_process_log(curr_dep_tree, action="dep_tree")
        if not success:
            msg = f"Failed to build known dep {child.project.name} for {COMPILER}/{ARCH}/{OPTMZ}."
            logger.warning(msg)
            save_process_log(
                msg,
                project_name=child.project.name,
                action="compile",
                status="fail",
                stage="known_dep",
            )
            return False
    return True


def compile_and_analyze(node: DepNode) -> dict:
    #! Compile
    build_started_at = time.perf_counter()
    compile_project_wrapper(node)
    build_elapsed_ms = int((time.perf_counter() - build_started_at) * 1000)

    #! Analyze
    prefix = current_log_prefix(node.project.name)
    result = analyze_build_result(node.project, node.project.log_path, prefix)
    node.status = "success" if result["overall_success"] else "failed"
    node.failed_stage = '' if result["overall_success"] else find_first_failed_stage(node.project, result)
    result["build_elapsed_ms"] = build_elapsed_ms
    result["prefix"] = prefix
    return result


def create_dep_node_from_llm_info(dep_info: Any, parent_node: DepNode) -> DepNode:
    dep_name = ""
    dep_version = ""
    dep_source_value = ""
    dep_source_type = "apt"

    if isinstance(dep_info, str):
        dep_name = dep_info.strip()
        dep_source_value = dep_name
    elif isinstance(dep_info, dict):
        dep_name = str(dep_info.get("name", "")).strip()
        dep_version = str(dep_info.get("version", "")).strip()
        source = dep_info.get("source", {})
        if isinstance(source, dict):
            dep_source_type = str(source.get("type", dep_source_type)).strip()
            dep_source_value = str(source.get("value", "")).strip()
        else:
            dep_source_type = str(dep_info.get("source_type", dep_source_type)).strip()
            dep_source_value = str(
                dep_info.get("source_value", dep_info.get("url", ""))
            ).strip()
    else:
        raise TypeError(f"Unsupported dependency payload: {type(dep_info)}")

    if not dep_name:
        raise ValueError("Dependency name is empty")
    if dep_source_type not in ("apt", "url", "url_git"):
        raise ValueError(f"Unsupported dependency source type '{dep_source_type}' for '{dep_name}'")
    if not dep_source_value:
        dep_source_value = dep_name if dep_source_type == "apt" else ""
    if not dep_source_value:
        raise ValueError(f"Dependency '{dep_name}' is missing source.value")

    root_base_path = get_root_base_path(parent_node)

    dep_project = Project(dep_name, root_base_path)
    #! Update dep_list
    dep_list = parent_node.project.dep_list or []
    if dep_name not in dep_list:
        dep_list.append(dep_name)
        parent_node.project.dep_list = dep_list

    version = dep_version
    if dep_source_type in ("url", "url_git") and not version:
        version = get_version_from_url(dep_project.name, dep_source_value)
    if version:
        dep_project.set_version(version)
    dep_project.source_type = dep_source_type
    dep_project.source_value = dep_source_value
    # dep_project.set_full_name(dep_project.name, dep_project.version)

    dep_project.dep_path = root_base_path
    mkdir(dep_project.dep_path, exist_ok=True)

    #! Save
    dep_project.resolve_file_path = os.path.join(ROOT_LOG, dep_project.name + ".resolve")
    save_project_to_file(dep_project, dep_project.resolve_file_path)

    parent_node.project.resolve_file_path = os.path.join(ROOT_LOG, parent_node.project.name + ".resolve")
    save_project_to_file(parent_node.project, parent_node.project.resolve_file_path)

    id = f"{dep_project.name}@{parent_node.id}"

    dep_node = DepNode(dep_project, parent=parent_node, id=f"{id}")

    return dep_node


def resolve_node(node: DepNode, tree: Tree, root_node: DepNode):
    logger.info("=" * 80)
    logger.info(f"[*] Resolving: {node.project.name}")
    node.status = "building"
    solver_base_path = get_solver_base_path(node)
    project_attempts = get_project_repair_iterations(root_node)
    update_solver_state(solver_base_path, node.project.name, status="building")

    print_dependency_tree_status(root_node)

    is_root = (node == root_node)

    # -----------------------------
    # 0. (child only) Validate current node
    # -----------------------------
    if not is_root:
        is_valid_source, source_detail = validate_project_source(node.project)
        if not is_valid_source:
            node.status = "failed"
            msg = f"❌ Invalid source for {node.project.name}: {source_detail}"
            logger.warning(msg)
            save_process_log(msg,
                             project_name=node.project.name,
                             action="compile", status="fail", stage="source_check")
            curr_dep_tree = print_dependency_tree_status(root_node)
            save_process_log(curr_dep_tree, action="dep_tree")
            update_solver_state(solver_base_path, node.project.name, status="failed")
            return False
        node.project.download_source_file()
        node.project.extract_source_file()

    # -----------------------------
    # 1. Compile current node
    # -----------------------------
    if node.children and not ensure_known_children_for_current_variant(node, tree, root_node):
        node.status = "failed"
        update_solver_state(
            solver_base_path,
            node.project.name,
            status="failed",
            last_decision="known_dep:failed",
            attempts=project_attempts,
            resolve_file=node.project.resolve_file_path or "",
        )
        return False

    build_result = compile_and_analyze(node)
    append_solver_trial(
        solver_base_path,
        node.project.name,
        {
            "kind": "build_attempt",
            **current_variant_metadata(),
            "status": node.status,
            "failed_stage": node.failed_stage,
            "build_elapsed_ms": build_result.get("build_elapsed_ms", 0),
            "result": build_result,
        },
    )

    compile_status = "success" if node.status == "success" else "fail"
    stage_info = node.failed_stage if compile_status == "fail" else ""
    save_process_log(f"Build result: {node.project.name} -> {node.status}",
                     project_name=node.project.name,
                     action="compile", status=compile_status, stage=stage_info)

    # -----------------------------
    # 2. Success -> Return True
    # -----------------------------
    if node.status == "success":
        logger.info(f"✅ {node.project.name} built successfully.")
        if is_root:
            curr_dep_tree = print_dependency_tree_status(root_node)
            save_process_log(curr_dep_tree, action="dep_tree")
            update_solver_state(
                solver_base_path,
                node.project.name,
                status="success",
                resolve_file=node.project.resolve_file_path or "",
                merge_candidate=False,
            )
        else:
            update_solver_state(
                solver_base_path,
                node.project.name,
                status="success",
                resolve_file=node.project.resolve_file_path or "",
                merge_candidate=False,
            )
        return True

    # -----------------------------
    # 3. Failed -> Query LLM
    # -----------------------------
    logger.info(f"❌ {node.project.name} failed at stage {node.failed_stage}")
    curr_dep_tree = print_dependency_tree_status(root_node)
    save_process_log(curr_dep_tree, action="dep_tree")

    project_attempts = get_project_repair_iterations(root_node)
    if project_attempts >= MAX_LLM_REPAIR_ITERATIONS:
        msg = (
            f"LLM max_iter reached for {root_node.project.name} "
            f"({project_attempts}/{MAX_LLM_REPAIR_ITERATIONS}) while resolving {node.project.name}."
        )
        logger.warning(msg)
        save_process_log(
            msg,
            project_name=node.project.name,
            action="llm",
            status="limit",
            stage=node.failed_stage,
        )
        node.status = "failed"
        update_solver_state(
            solver_base_path,
            node.project.name,
            status="failed",
            last_decision="max_iter",
            attempts=project_attempts,
            resolve_file=node.project.resolve_file_path or "",
        )
        append_solver_trial(
            solver_base_path,
            node.project.name,
            {
                "kind": "max_iter_reached",
                "root_project": root_node.project.name,
                **current_variant_metadata(),
                "attempts": project_attempts,
                "max_iter": MAX_LLM_REPAIR_ITERATIONS,
                "failed_stage": node.failed_stage,
            },
        )
        return False

    prefix = current_log_prefix(node.project.name)
    logger.info(f"[*] Querying LLM for {node.project.name} (stage: {node.failed_stage})")

    llm_result = query(
        project=node.project,
        log_path=node.project.log_path,
        prefix=prefix,
        build_stage=node.failed_stage,
        project_name=node.project.name,
        solver_base_path=solver_base_path,
    )
    project_attempts = increment_project_repair_iterations(root_node)
    update_solver_state(
        solver_base_path,
        root_node.project.name,
        attempts=project_attempts,
    )
    append_solver_trial(
        solver_base_path,
        node.project.name,
        {
            "kind": "repair_iteration",
            "root_project": root_node.project.name,
            "iteration": project_attempts,
            "max_iter": MAX_LLM_REPAIR_ITERATIONS,
            **current_variant_metadata(),
            "failed_stage": node.failed_stage,
        },
    )

    # -----------------------------
    # 4. Classification
    # -----------------------------

    pretty_str = pprint.pformat(llm_result, indent=2, width=80)
    save_process_log(pretty_str, project_name=node.project.name, action="llm", stage=node.failed_stage)

    pretty_str = pprint.pformat(llm_result, indent=2, width=80)
    print(pretty_str)

    issue_type = normalize_llm_decision_type(llm_result)
    solution = get_llm_solution(llm_result)
    if issue_type == "none":
        print("No known issue type detected (none).")
    else:
        print(f"Detected issue type: {issue_type}")

    if issue_type == "none":
        msg = "LLM found no problem. Aborting subtree."
        logger.warning(msg)
        save_process_log(msg,
                            project_name=node.project.name, action="llm", status="empty", stage=node.failed_stage)
        update_solver_state(
            solver_base_path,
            node.project.name,
            status="failed",
            last_decision="none",
            attempts=project_attempts,
            resolve_file=node.project.resolve_file_path or "",
        )
        return False

    if issue_type == "deps":
        # -----------------------------
        # 5.1 Add deps to tree
        # -----------------------------
        new_children = []
        added_dep_names: list[str] = []
        for dep_info in solution["deps"]:
            if DEPENDENCY_SOURCE_LOOKUP_ENABLED:
                dep_info = normalize_dep_info_with_source_lookup(dep_info)
            if node.project.dep_list:
                dep_name = dep_info if isinstance(dep_info, str) else dep_info.get("name")
                if dep_name in node.project.dep_list:
                    continue
            child_node = create_dep_node_from_llm_info(dep_info, node)
            node.children.append(child_node)
            new_children.append(child_node)
            added_dep_names.append(child_node.project.name)
            # tree.create_node(tag=child_node.project.name, identifier=child_node.project.name, parent=node.project.name)
            tree.create_node(tag=child_node.project.name, identifier=child_node.id, parent=node.id)

        if added_dep_names:
            update_solver_state(
                solver_base_path,
                node.project.name,
                last_decision="deps",
                attempts=project_attempts,
                resolve_file=node.project.resolve_file_path or "",
            )
            append_solver_trial(
                solver_base_path,
                node.project.name,
                {
                    "kind": "applied_delta",
                    "decision_type": "deps",
                    "iteration": project_attempts,
                    "values": added_dep_names,
                },
            )

        curr_dep_tree = print_dependency_tree_status(root_node)
        save_process_log(curr_dep_tree, action="dep_tree")

        # -----------------------------
        # 5.2 DFS Recursion into deps
        # -----------------------------
        for child in new_children:
            if child.project.name == child.parent.project.name:
                print("[!] Duplicated deps")
                continue
            success = resolve_node(child, tree, root_node)

            curr_dep_tree = print_dependency_tree_status(root_node)
            save_process_log(curr_dep_tree, action="dep_tree")

            if not success:
                msg = f"❌ Failed to build dep {child.project.name}, continuing with others..."
                logger.warning(msg)
                save_process_log(msg,
                                project_name=child.project.name, action="compile", status="fail")

        if new_children:
            return resolve_node(node, tree, root_node)
        else:
            print("[-] No new dep node(s)")
            update_solver_state(
                solver_base_path,
                node.project.name,
                status="failed",
                last_decision="deps:no_new_delta",
                attempts=project_attempts,
                resolve_file=node.project.resolve_file_path or "",
            )
            return False
    elif issue_type == "options":
        conf_options = [str(x) for x in solution["options"]]
        merged_options, added_options = append_unique_tokens(node.project.conf_options, conf_options)
        if not added_options:
            logger.warning("LLM proposed no new configure options. Aborting subtree.")
            update_solver_state(
                solver_base_path,
                node.project.name,
                status="failed",
                last_decision="options:no_new_delta",
                attempts=project_attempts,
                resolve_file=node.project.resolve_file_path or "",
            )
            return False
        assert node.project.conf_options is not None
        node.project.conf_options = merged_options
        update_solver_state(
            solver_base_path,
            node.project.name,
            last_decision="options",
            attempts=project_attempts,
            resolve_file=node.project.resolve_file_path or "",
        )
        append_solver_trial(
            solver_base_path,
            node.project.name,
            {
                "kind": "applied_delta",
                "decision_type": "options",
                "iteration": project_attempts,
                "values": added_options,
            },
        )
        assert node.project.resolve_file_path is not None
        save_project_to_file(node.project, node.project.resolve_file_path)
        return resolve_node(node, tree, root_node)
    elif issue_type == "cflags":
        cflags = [str(x) for x in solution["cflags"]]
        merged_cflags, added_cflags = append_unique_tokens(node.project.cflags, cflags)
        if not added_cflags:
            logger.warning("LLM proposed no new CFLAGS. Aborting subtree.")
            update_solver_state(
                solver_base_path,
                node.project.name,
                status="failed",
                last_decision="cflags:no_new_delta",
                attempts=project_attempts,
                resolve_file=node.project.resolve_file_path or "",
            )
            return False
        assert node.project.cflags is not None
        node.project.cflags = merged_cflags
        update_solver_state(
            solver_base_path,
            node.project.name,
            last_decision="cflags",
            attempts=project_attempts,
            resolve_file=node.project.resolve_file_path or "",
        )
        append_solver_trial(
            solver_base_path,
            node.project.name,
            {
                "kind": "applied_delta",
                "decision_type": "cflags",
                "iteration": project_attempts,
                "values": added_cflags,
            },
        )
        assert node.project.resolve_file_path is not None
        save_project_to_file(node.project, node.project.resolve_file_path)
        return resolve_node(node, tree, root_node)
    elif issue_type == "ldflags":
        ldflags = [str(x) for x in solution["ldflags"]]
        merged_ldflags, added_ldflags = append_unique_tokens(node.project.ldflags, ldflags)
        if not added_ldflags:
            logger.warning("LLM proposed no new LDFLAGS. Aborting subtree.")
            update_solver_state(
                solver_base_path,
                node.project.name,
                status="failed",
                last_decision="ldflags:no_new_delta",
                attempts=project_attempts,
                resolve_file=node.project.resolve_file_path or "",
            )
            return False
        assert node.project.ldflags is not None
        node.project.ldflags = merged_ldflags
        update_solver_state(
            solver_base_path,
            node.project.name,
            last_decision="ldflags",
            attempts=project_attempts,
            resolve_file=node.project.resolve_file_path or "",
        )
        append_solver_trial(
            solver_base_path,
            node.project.name,
            {
                "kind": "applied_delta",
                "decision_type": "ldflags",
                "iteration": project_attempts,
                "values": added_ldflags,
            },
        )
        assert node.project.resolve_file_path is not None
        save_project_to_file(node.project, node.project.resolve_file_path)
        return resolve_node(node, tree, root_node)
    else:
        logger.error("??? No issue_type ???")


def select_auto_resolve_dependency_path(
    target_project: Project,
    use_extra_deps: bool = False,
) -> str:
    dependency_path = (
        config.EXTRA_DEP_PATH
        if use_extra_deps
        else os.path.join(target_project.log_path, "deps")
    )
    target_project.dep_path = dependency_path
    mkdir(dependency_path, exist_ok=True)
    logger.info(
        "Auto-resolve dependency root: %s (%s)",
        dependency_path,
        "shared extra_deps" if use_extra_deps else "isolated",
    )
    return dependency_path


def auto_resolve_target_project(
    target_project: Project,
    use_extra_deps: bool = False,
) -> bool:
    dependency_path = select_auto_resolve_dependency_path(
        target_project,
        use_extra_deps,
    )

    global PROCESS_LOG_DIR
    PROCESS_LOG_DIR = os.path.join(target_project.log_path, "process")
    shutil.rmtree(PROCESS_LOG_DIR, ignore_errors=True)
    mkdir(PROCESS_LOG_DIR)

    global ROOT_LOG
    ROOT_LOG = target_project.log_path
    target_project.resolve_file_path = os.path.join(ROOT_LOG, target_project.name + ".resolve")
    save_project_to_file(target_project, target_project.resolve_file_path)
    update_solver_state(
        target_project.base_path,
        target_project.name,
        dependency_mode="extra_deps" if use_extra_deps else "isolated",
        dependency_root=dependency_path,
    )

    root_node = DepNode(target_project, parent=None, id=target_project.name)
    tree = Tree()
    tree.create_node(tag=root_node.project.name, identifier=root_node.id)

    #! Start to resolve dependency tree!
    success = True
    for variant in BUILD_VARIANTS:
        set_solver_build_context(variant)
        root_node.project.variant_tag = CURRENT_VARIANT.file_tag
        variant_label = (
            f"{COMPILER}/{ARCH}/{OPTMZ}/flags-{CURRENT_VARIANT.flag_bits}"
        )
        logger.info("=" * 80)
        logger.info("[*] Auto-resolve variant: %s -> %s", root_node.project.name, variant_label)
        append_solver_trial(
            root_node.project.base_path,
            root_node.project.name,
            {
                "kind": "variant_start",
                **current_variant_metadata(),
                "attempts": get_project_repair_iterations(root_node),
                "max_iter": MAX_LLM_REPAIR_ITERATIONS,
            },
        )

        variant_success = resolve_node(root_node, tree, root_node)
        append_solver_trial(
            root_node.project.base_path,
            root_node.project.name,
            {
                "kind": "variant_result",
                **current_variant_metadata(),
                "status": "success" if variant_success else "failed",
                "failed_stage": root_node.failed_stage,
                "attempts": get_project_repair_iterations(root_node),
                "max_iter": MAX_LLM_REPAIR_ITERATIONS,
                "solution": export_project_solution_state(root_node.project).get("solution", {}),
            },
        )
        if not variant_success:
            success = False
            break

    result_file_path = os.path.join(root_node.project.base_path, "result.log")
    if success:
        logger.info("🟢 Auto-resolve build completed successfully.")
        write_merge_candidates_for_tree(root_node.project.base_path, root_node)
        update_solver_state(
            root_node.project.base_path,
            root_node.project.name,
            status="success",
            resolve_file=root_node.project.resolve_file_path or "",
            merge_candidate=True,
        )
        with open(result_file_path, "a") as f:
            f.write(f"True: {root_node.project.name}\n")
    else:
        logger.error("🔴 Auto-resolve build failed.")
        update_solver_state(
            root_node.project.base_path,
            root_node.project.name,
            status="failed",
            resolve_file=root_node.project.resolve_file_path or "",
            merge_candidate=False,
        )
        with open(result_file_path, "a") as f:
            f.write(f"False: {root_node.project.name}\n")
    return success


def auto_resolve(
    projects: list[Project],
    targets: list[str] = [],
    source_config_path: str = "",
    max_iter: int = 20,
    dependency_source_lookup: bool = True,
    use_extra_deps: bool = False,
) -> bool:
    global SOURCE_CONFIG_PATH
    SOURCE_CONFIG_PATH = os.path.abspath(source_config_path) if source_config_path else ""
    set_solver_max_iter(max_iter)
    set_dependency_source_lookup(dependency_source_lookup)
    initialize_solver_build_context(SOURCE_CONFIG_PATH)
    download_and_extract_projects(projects, targets)
    analyze_projects(projects, targets)

    tmp_projects = []
    for proj in projects:
        if proj.name in targets:
            tmp_projects.append(proj)

    if tmp_projects:
        initialize_solver_state(tmp_projects[0].base_path, [proj.name for proj in tmp_projects])

    results: list[bool] = []
    for proj in tmp_projects:
        proj.print_info()
        results.append(
            auto_resolve_target_project(proj, use_extra_deps=use_extra_deps)
        )

    return bool(results) and all(results)



# EOF
