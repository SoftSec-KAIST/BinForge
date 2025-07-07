from binforge.utils import *
from binforge import config
from binforge.models import Project
from binforge.compile_manager import compile_project
from binforge.source_manager import set_project_info

import concurrent.futures
from collections import defaultdict

def compile_dep(dep: Project, arch: str, extra_dep_path: str) -> None:
    install_prefix = os.path.join(extra_dep_path, "install", arch, dep.name)

    if directory_exists(install_prefix):
        if not is_directory_empty(install_prefix):
            logger.info(f"Already installed ({dep.name})")
            return

    mkdir(install_prefix)
    dep.install_prefix = install_prefix

    comb = "gcc-13.3.0", arch, "O2"
    PATH = get_env_variable("PATH")

    compile_project(dep, comb, PATH, EXTRA_DEP=True)


def install_deps(dep_list: list[Project]) -> None:
    arch_list = ["x86_32", "x86_64", "arm_32", "arm_64", "mips_32", "mips_64", "mipseb_32", "mipseb_64", "ppceb_32", "ppceb_64", "ppc_64"]
    extra_dep_path = config.EXTRA_DEP_PATH
    with concurrent.futures.ProcessPoolExecutor() as executor:
        futures = []
        for dep in dep_list:
            log_path = os.path.join(extra_dep_path, 'log', dep.name)
            mkdir(log_path, exist_ok=True)

            #! Download and extract source code
            dep.download_source_file()
            dep.extract_source_file()

            for arch in arch_list:
                futures.append(executor.submit(compile_dep, dep, arch, extra_dep_path))
        concurrent.futures.wait(futures)


def get_dep_depths():
    data = load_yaml("config/dep_map.yaml")
    deps_map = {}

    for entry in data["projects"]:
        name = entry["name"]
        deps = entry.get("deps", [])
        deps_map[name] = deps

    all_deps = set()
    for deps in deps_map.values():
        all_deps.update(deps)

    depth_cache = {}

    def get_depth(dep_name):
        if dep_name in depth_cache:
            return depth_cache[dep_name]
        if dep_name not in deps_map or not deps_map[dep_name]:
            depth_cache[dep_name] = 0
            return 0
        dep_depths = [get_depth(sub_dep) for sub_dep in deps_map[dep_name]]
        max_depth = 1 + max(dep_depths)
        depth_cache[dep_name] = max_depth
        return max_depth

    dep_depths = {dep: get_depth(dep) for dep in all_deps}

    return dep_depths


def compile_extra_deps():
    extra_dep_path = config.EXTRA_DEP_PATH
    logger.info(f"extra_dep_path: {extra_dep_path}")
    mkdir(extra_dep_path, exist_ok=True)

    dep_depths = get_dep_depths()

    group_by_depth = defaultdict(list)
    for dep, depth in dep_depths.items():
        group_by_depth[depth].append(dep)

    for depth in sorted(group_by_depth):
        deps = sorted(group_by_depth[depth])
        print(f"Depth {depth}: {deps}")

    dep_map = load_yaml("config/dep_map.yaml")["projects"]
    name_to_deps = {item['name']: item['deps'] for item in dep_map}

    #! Supported
    data = load_yaml("config/supported_deps.yaml")
    supported_deps = {item["name"]: item for item in data["projects"]}

    #! Install dependencies by depth
    for depth in sorted(group_by_depth):
        target_deps = sorted(group_by_depth[depth])

        dep_list = []
        for name in target_deps:
            # print(f"[DEBUG] target_dep: {name}")
            if name not in supported_deps.keys():
                logger.error(f"URL must be given: {name}")
                exit()

            url = supported_deps[name].get("url")

            version = supported_deps[name].get("version") or None
            if version is not None:
                version = str(version).strip()
            if not version:
                version = None

            use_git_clone = supported_deps[name].get("use_git_clone") or None


            dep = Project(name, extra_dep_path)
            dep.version = version
            dep.use_git_clone = use_git_clone
            set_project_info(dep, url)

            #! Set deps of target dep
            deps_of_dep = name_to_deps.get(name) or []
            dep.dep_list = deps_of_dep

            dep_list.append(dep)

        install_deps(dep_list)

    return


# EOF
