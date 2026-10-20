from binforge.utils import *
from binforge import config
from binforge.models import Project
from binforge.compile_manager import compile_project
from binforge.conf_manager import analyze_projects
from binforge.source_manager import download_and_extract_projects

import concurrent.futures
from collections import defaultdict, deque
import json
from pathlib import Path
from binforge.source_snapshot import materialize_projects, tree_sha256
from binforge.build_monitor_state import update_run, update_variant

DEFAULT_DEP_JOBS = min(config.NUM_JOBS, 32)


def deps_of(projects: list[Project], name: str) -> list[str]:
    for p in projects:
        if p.name == name:
            return p.dep_list or []
    return []


def compute_dependency_layers(projects: list[Project], targets: list[str]) -> list[list[str]]:
    targets_set = set(targets)

    project_names_list = [p.name for p in projects]

    need: set[str] = set()
    dep_targets: set[str] = set()
    stack = list(targets)
    seen = set()

    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for d in deps_of(projects, cur):
            if d not in project_names_list:
                raise ValueError(f"undefined dep '{d}' (referenced by '{cur}')")
            if d in targets_set:
                dep_targets.add(d)
            if d not in need:
                need.add(d)
                stack.append(d)

    nodes = need.union(dep_targets)
    if not nodes:
        return []

    adj = defaultdict(set)
    indeg = {n: 0 for n in nodes}

    candidates = nodes.union(targets_set)
    for u in candidates:
        for d in deps_of(projects, u):
            if d in nodes and u in nodes:
                adj[d].add(u)

    for d, users in adj.items():
        for u in users:
            indeg[u] += 1

    q = deque(sorted([n for n, deg in indeg.items() if deg == 0]))
    visited = set()
    layers: list[list[str]] = []

    while q:
        cur_level = []
        for _ in range(len(q)):
            v = q.popleft()
            if v in visited:
                continue
            visited.add(v)
            cur_level.append(v)
            for w in adj.get(v, ()):
                indeg[w] -= 1
                if indeg[w] == 0:
                    q.append(w)
        if cur_level:
            layers.append(sorted(cur_level))

    remaining = [n for n in nodes if n not in visited]
    if remaining:
        raise RuntimeError(f"dependency cycle detected among: {sorted(remaining)}")

    return layers


def install_deps(dep_list: list[Project], arch_list: list[str], jobs: int = DEFAULT_DEP_JOBS,
                 monitor_run_dir=None) -> None:
    if jobs < 1:
        raise ValueError("dependency jobs must be >= 1")
    extra_dep_path = config.EXTRA_DEP_PATH
    with concurrent.futures.ProcessPoolExecutor(max_workers=jobs) as executor:
        futures = {}
        for dep in dep_list:
            log_path = os.path.join(extra_dep_path, 'log', dep.name)
            mkdir(log_path, exist_ok=True)

            #! Download and extract source code
            # dep.download_source_file()
            # dep.extract_source_file()

            for arch in arch_list:
                future = executor.submit(compile_dep, dep, arch, extra_dep_path,
                                         monitor_run_dir)
                futures[future] = (dep.name, arch)
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except BaseException as exc:
                if monitor_run_dir:
                    name, arch = futures[future]
                    update_variant(monitor_run_dir, name, ("gcc-13.3.0", arch, "O2"),
                                   state="failed", phase="failed",
                                   error=f"{type(exc).__name__}: {exc}")
                raise


def compile_dep(dep: Project, arch: str, extra_dep_path: str,
                monitor_run_dir=None) -> None:
    install_prefix = os.path.join(extra_dep_path, "install", arch, dep.name)
    marker = Path(install_prefix) / ".binforge-complete.json"
    identity = {"project": dep.name, "arch": arch, "compiler": "gcc-13.3.0",
                "optimization": "O2", "source_tree_sha256": getattr(dep, "source_tree_sha256", None),
                "options": dep.conf_options, "cflags": dep.cflags, "ldflags": dep.ldflags}
    if marker.exists():
        if json.loads(marker.read_text()) != identity:
            raise RuntimeError(f"Dependency installation does not match requested source or settings: {install_prefix}")
        logger.info("Already installed (%s, %s)", dep.name, arch)
        if monitor_run_dir:
            update_variant(monitor_run_dir, dep.name, ("gcc-13.3.0", arch, "O2"),
                           state="succeeded", phase="cached")
        return
    if directory_exists(install_prefix) and not is_directory_empty(install_prefix):
        raise RuntimeError(f"Incomplete dependency installation requires cleanup: {install_prefix}")
    mkdir(install_prefix, exist_ok=True)
    dep.install_prefix = install_prefix

    comb = "gcc-13.3.0", arch, "O2"
    PATH = get_env_variable("PATH")

    if not compile_project(dep, comb, PATH, EXTRA_DEP=True,
                           monitor_run_dir=str(monitor_run_dir) if monitor_run_dir else None):
        raise RuntimeError(f"Dependency build failed: {dep.name} ({arch})")
    if is_directory_empty(install_prefix):
        raise RuntimeError(f"Dependency installed no files: {dep.name} ({arch})")
    marker.write_text(json.dumps(identity, sort_keys=True) + "\n")


def compile_extra_deps(projects, targets, arch_list, *, snapshot_root=None,
                       source_dataset=None, jobs=DEFAULT_DEP_JOBS, monitor_run_dir=None):
    extra_dep_path = config.EXTRA_DEP_PATH
    logger.info(f"extra_dep_path: {extra_dep_path}")
    mkdir(extra_dep_path, exist_ok=True)

    #! Set deps
    dep_project_names = set()
    for proj in projects:
        for dep_name in proj.dep_list:
            dep_project_names.add(dep_name)

    dep_projects = []
    for proj in projects:
        if proj.name in dep_project_names:
            dep_projects.append(proj)

    layers = compute_dependency_layers(projects, targets)
    for i, lvl in enumerate(layers):
        print(f"Depth {i}: {', '.join(lvl)}")

    def set_dep(name, dep_projects) -> Project: # type: ignore
        for dep_proj in dep_projects:
            if name == dep_proj.name:
                return dep_proj

    project_names_list = [p.name for p in projects]
    for depth, names in enumerate(layers):
        if monitor_run_dir:
            update_run(monitor_run_dir, phase=f"source depth {depth}")
        target_deps = sorted(names)
        print(f"Depth {depth}: {target_deps}")

        dep_objs = []
        for name in target_deps:
            if name not in project_names_list:
                raise ValueError(f"unknown dep '{name}'")

            dep = set_dep(name, dep_projects)

            dep.base_path = extra_dep_path
            dep.source_path = os.path.join(extra_dep_path, "sources", name)
            dep.log_path = os.path.join(extra_dep_path, "log", name)
            dep.bins_path = os.path.join(extra_dep_path, "bins", name)
            dep.dep_path = extra_dep_path

            dep_objs.append(dep)

        if snapshot_root is not None:
            materialize_projects(dep_objs, target_deps, snapshot_root, source_dataset)
            for dep in dep_objs:
                dep.source_tree_sha256 = tree_sha256(Path(dep.source_path) / dep.name)
        else:
            download_and_extract_projects(dep_objs, target_deps)
        analyze_projects(dep_objs)
        if monitor_run_dir:
            update_run(monitor_run_dir, phase=f"build depth {depth}")
        install_deps(dep_objs, arch_list, jobs=jobs, monitor_run_dir=monitor_run_dir)


# EOF
