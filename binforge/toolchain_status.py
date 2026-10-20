"""Read-only checks for installed compiler toolchains."""

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

from binforge import config


def _output(path, *args):
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return None, "missing executable"
    try:
        result = subprocess.run(
            [path, *args], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode:
        return None, f"exit {result.returncode}: {result.stderr.strip()}"
    return result.stdout.strip(), None


def gcc_status(version, arch, prefix, directory):
    root = os.path.join(directory, f"{prefix}-{version}")
    bin_dir = os.path.join(root, "bin")
    problems = []
    for frontend in ("gcc", "g++"):
        binary = os.path.join(bin_dir, f"{prefix}-{frontend}")
        actual, error = _output(binary, "-dumpfullversion")
        if error:
            problems.append(f"{frontend}: {error}")
        elif actual != version:
            problems.append(f"{frontend}: version {actual}, expected {version}")
        actual, error = _output(binary, "-dumpmachine")
        if not error and actual != prefix:
            problems.append(f"{frontend}: target {actual}, expected {prefix}")
        elif error and not any(item.startswith(f"{frontend}:") for item in problems):
            problems.append(f"{frontend}: {error}")
    linker = os.path.join(bin_dir, f"{prefix}-ld")
    if not os.path.isfile(linker) or not os.access(linker, os.X_OK):
        problems.append("ld: missing executable")
    if not os.path.isdir(os.path.join(root, prefix, "sysroot")):
        problems.append("sysroot: missing directory")
    return version, arch, problems


def clang_status(version, directory):
    root = os.path.join(directory, f"clang-{version}")
    problems = []
    for frontend in ("clang", "clang++"):
        binary = os.path.join(root, "bin", frontend)
        actual, error = _output(binary, "--version")
        if error:
            problems.append(f"{frontend}: {error}")
        else:
            match = re.search(r"\bclang version (\S+)", actual)
            if not match or match.group(1) != version:
                problems.append(f"{frontend}: version {match.group(1) if match else 'unknown'}, expected {version}")
        if frontend == "clang" and not error:
            resource_dir, resource_error = _output(binary, "-print-resource-dir")
            if resource_error or not os.path.isdir(resource_dir):
                problems.append("resource directory: missing")
    return version, problems


def show_gcc_status(settings, arch_prefix_map, verbose=False):
    bootstrap = settings["bootstrap"]
    bootstrap_arch = bootstrap["arch"]
    bootstrap_version = str(bootstrap["version"])
    bootstrap_prefix = arch_prefix_map[bootstrap_arch]
    _, _, bootstrap_problems = gcc_status(
        bootstrap_version, bootstrap_arch, bootstrap_prefix, config.BOOTSTRAP_PATH
    )
    bootstrap_link = os.path.join(config.BOOTSTRAP_PATH, "bootstrap", "bin", f"{bootstrap_prefix}-gcc")
    if not os.path.isfile(bootstrap_link):
        bootstrap_problems.append("bootstrap link: missing")
    print(f"Bootstrap: {'OK' if not bootstrap_problems else 'INCOMPLETE'} ({bootstrap_version}, {bootstrap_arch})")
    for problem in bootstrap_problems:
        print(f"  {problem}")

    targets = [(str(version), arch, arch_prefix_map[arch])
               for version, archs in settings["toolchain_targets"].items() for arch in archs]
    directory = os.path.join(config.TOOL_PATH, settings["compiler_group_name"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda target: gcc_status(*target, directory), targets))

    installed = sum(not problems for _, _, problems in results)
    print(f"\nGCC toolchains: {installed}/{len(results)} installed")
    print(f"  {'version':<9} {'installed':>9}  state")
    for version, archs in settings["toolchain_targets"].items():
        version_results = [(arch, problems) for found_version, arch, problems in results
                           if found_version == str(version)]
        count = sum(not problems for _, problems in version_results)
        print(f"  {version:<9} {f'{count}/{len(version_results)}':>9}  {'OK' if count == len(version_results) else 'INCOMPLETE'}")

    if verbose:
        print("\nGCC targets:")
        print(f"  {'version':<9} {'arch':<10} state")
        for version, arch, problems in results:
            print(f"  {version:<9} {arch:<10} {'OK' if not problems else 'INCOMPLETE'}")

    failures = [(version, arch, problems) for version, arch, problems in results if problems]
    if failures:
        print("\nInstallation problems:")
        for version, arch, problems in failures:
            print(f"  {version} {arch}: {'; '.join(problems)}")

    # These links are created by link_toolchains.py after compiler installation.
    link_archs = settings["toolchain_targets"].get("13.3.0", [])
    if link_archs:
        missing_links = []
        missing_startup = []
        for arch in link_archs:
            prefix = arch_prefix_map[arch]
            installed_gcc = os.path.join(directory, f"{prefix}-13.3.0", "bin", f"{prefix}-gcc")
            system_gcc = f"/usr/bin/{prefix}-gcc"
            if not os.path.exists(system_gcc) or os.path.realpath(system_gcc) != os.path.realpath(installed_gcc):
                missing_links.append(arch)
            target_lib = os.path.join(directory, f"{prefix}-13.3.0", prefix, "lib")
            if any(not os.path.islink(os.path.join(target_lib, name)) or not os.path.exists(os.path.join(target_lib, name))
                   for name in ("crt1.o", "crti.o")):
                missing_startup.append(arch)
        print(f"\nPost-install links: {len(link_archs) - len(missing_links)}/{len(link_archs)} linked (separate setup step)")
        if missing_links:
            print("  Missing or different target:")
            for start in range(0, len(missing_links), 4):
                print(f"    {', '.join(missing_links[start:start + 4])}")
        print(f"Startup object links: {len(link_archs) - len(missing_startup)}/{len(link_archs)} linked")
        if missing_startup:
            print(f"  Missing crt1.o or crti.o links: {', '.join(missing_startup)}")
    return not bootstrap_problems and installed == len(results)


def show_clang_status(settings, verbose=False):
    directory = os.path.join(config.TOOL_PATH, settings["compiler_group_name"])
    versions = [str(version) for version in settings["toolchain_targets"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda version: clang_status(version, directory), versions))
    installed = sum(not problems for _, problems in results)
    print(f"Clang toolchains: {installed}/{len(results)} installed")
    if verbose:
        print(f"  {'version':<9} state")
        for version, problems in results:
            print(f"  {version:<9} {'OK' if not problems else 'INCOMPLETE'}")
    else:
        good_versions = [version for version, problems in results if not problems]
        print("  Installed versions:")
        for start in range(0, len(good_versions), 7):
            print(f"    {', '.join(good_versions[start:start + 7])}")
        if not good_versions:
            print("    none")
    failures = [(version, problems) for version, problems in results if problems]
    if failures:
        print("\nInstallation problems:")
        for version, problems in failures:
            print(f"  {version}: {'; '.join(problems)}")
    return installed == len(results)
