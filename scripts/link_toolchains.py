import sys
import os
import argparse
import subprocess
import stat

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from binforge import config
from binforge.logging_helper import logger

TOOLCHAIN_VERSION = "13.3.0" # Test "14.2.0"

arch_prefix_list = [
    "i686-unknown-linux-gnu",
    "x86_64-unknown-linux-gnu",
    "arm-unknown-linux-gnueabi",
    "aarch64-unknown-linux-gnu",
    "mipsel-unknown-linux-gnu",
    "mips64el-unknown-linux-gnu",
    "mips-unknown-linux-gnu",
    "mips64-unknown-linux-gnu",
    "powerpc-unknown-linux-gnu",
    "powerpc64le-unknown-linux-gnu",
    "powerpc64-unknown-linux-gnu",
    # "riscv32-unknown-linux-gnu",
    # "riscv64-unknown-linux-gnu",
]

toolchain_list = [
    "addr2line",
    "ar",
    "as",
    "c++",
    "cc",
    "c++filt",
    "cpp",
    # "ct-ng.config",
    "elfedit",
    "g++",
    f"gcc-{TOOLCHAIN_VERSION}",
    "gcc-ar",
    "gcc-nm",
    "gcc-ranlib",
    "gcov",
    "gcov-dump",
    "gcov-tool",
    "gprof",
    "ld",
    "ld.bfd",
    "ldd",
    "nm",
    "objcopy",
    "objdump",
    "populate",
    "ranlib",
    "readelf",
    "size",
    "strings",
    "strip"
]


def alternative_path(prefix: str, tool_path: str = config.TOOL_PATH) -> str:
    return os.path.join(tool_path, "gcc", f"{prefix}-{TOOLCHAIN_VERSION}", "bin", f"{prefix}-gcc")


def alternative_command(
    prefix: str,
    action: str,
    tool_path: str = config.TOOL_PATH,
    system_bin_dir: str = "/usr/bin",
    admin_dir: str | None = None,
    alternative_dir: str | None = None,
) -> list[str]:
    if system_bin_dir != "/usr/bin" and (admin_dir is None or alternative_dir is None):
        raise ValueError("Custom alternatives paths require both admin_dir and alternative_dir")
    command = ["update-alternatives"]
    if alternative_dir is not None:
        command.extend(["--altdir", alternative_dir])
    if admin_dir is not None:
        command.extend(["--admindir", admin_dir])
    name = f"{prefix}-gcc"
    compiler = alternative_path(prefix, tool_path)
    if action == "install":
        command.extend(["--force", "--install", os.path.join(system_bin_dir, name), name, compiler, "100"])
        bin_dir = os.path.dirname(compiler)
        for tool in toolchain_list:
            slave = f"{prefix}-{tool}"
            command.extend(["--slave", os.path.join(system_bin_dir, slave), slave, os.path.join(bin_dir, slave)])
    elif action == "remove":
        command.extend(["--remove", name, compiler])
    else:
        raise ValueError(f"Unknown alternative action: {action}")
    return command


def run_alternative_command(prefix: str, action: str, *, sudo: bool = True, **kwargs) -> None:
    command = alternative_command(prefix, action, **kwargs)
    if sudo:
        command.insert(0, "sudo")
    subprocess.run(command, check=True)


def selected_alternative(link: str) -> str | None:
    if not os.path.islink(link):
        return None
    target = os.readlink(link)
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(link), target)
    if os.path.islink(target):
        next_target = os.readlink(target)
        if not os.path.isabs(next_target):
            next_target = os.path.join(os.path.dirname(target), next_target)
        target = next_target
    return os.path.normpath(target)


def alternative_link_problems(
    expected_state: str,
    tool_path: str = config.TOOL_PATH,
    system_bin_dir: str = "/usr/bin",
) -> list[str]:
    if expected_state not in ("linked", "unlinked"):
        raise ValueError(f"Unknown link state: {expected_state}")
    problems = []
    for prefix in arch_prefix_list:
        target_bin = os.path.dirname(alternative_path(prefix, tool_path))
        for tool in ("gcc", *toolchain_list):
            name = f"{prefix}-{tool}"
            link = os.path.join(system_bin_dir, name)
            target = os.path.join(target_bin, name)
            matches = selected_alternative(link) == os.path.normpath(target)
            if expected_state == "linked" and not matches:
                problems.append(f"{name}: not linked to BinForge")
            elif expected_state == "unlinked" and matches:
                problems.append(f"{name}: still linked to BinForge")
    return problems


def show_link_status(expected_state: str = "linked", tool_path: str = config.TOOL_PATH, system_bin_dir: str = "/usr/bin") -> bool:
    problems = alternative_link_problems(expected_state, tool_path, system_bin_dir)
    total = len(arch_prefix_list) * (len(toolchain_list) + 1)
    print(f"BinForge /usr/bin alternatives: {total - len(problems)}/{total} {expected_state}")
    for problem in problems:
        print(f"  {problem}")
    if expected_state == "linked":
        internal = missing_internal_links(tool_path)
        print(f"Internal startup links: {'ready' if not internal else 'incomplete'}")
        for problem in internal:
            print(f"  {problem}")
        sysroot = missing_sysroot_links(tool_path)
        print(f"Internal sysroot links: {'ready' if not sysroot else 'incomplete'}")
        for problem in sysroot:
            print(f"  {problem}")
        libmvec = missing_libmvec_links(tool_path)
        print(f"AArch64 libmvec links: {'ready' if not libmvec else 'incomplete'}")
        for problem in libmvec:
            print(f"  {problem}")
        return not problems and not internal and not sysroot and not libmvec
    print("Internal sysroot and libmvec links are checked separately with --all.")
    return not problems


def missing_installation_files(tool_path: str = config.TOOL_PATH) -> list[str]:
    missing = []
    for prefix in arch_prefix_list:
        bin_dir = os.path.join(tool_path, "gcc", f"{prefix}-{TOOLCHAIN_VERSION}", "bin")
        for name in ("gcc", *toolchain_list):
            path = os.path.join(bin_dir, f"{prefix}-{name}")
            if not os.path.isfile(path):
                missing.append(path)

    source_root = os.path.join(tool_path, "gcc", f"aarch64-unknown-linux-gnu-{TOOLCHAIN_VERSION}", "aarch64-unknown-linux-gnu")
    # lib/libmvec.a is created from sysroot/usr/lib below; it is not an
    # installation prerequisite on a fresh compiler build.
    for relative in ("sysroot/lib/libmvec.so.1", "sysroot/usr/lib/libmvec.a", "sysroot/usr/lib/libmvec.so"):
        path = os.path.join(source_root, relative)
        if not os.path.isfile(path):
            missing.append(path)
    return missing


def missing_internal_links(tool_path: str = config.TOOL_PATH) -> list[str]:
    missing = []
    for prefix in arch_prefix_list:
        target_lib = os.path.join(tool_path, "gcc", f"{prefix}-{TOOLCHAIN_VERSION}", prefix, "lib")
        for name in ("crt1.o", "crti.o"):
            path = os.path.join(target_lib, name)
            if not os.path.islink(path) or not os.path.exists(path):
                missing.append(f"{prefix}: {name} link")
    return missing


def missing_sysroot_links(tool_path: str = config.TOOL_PATH) -> list[str]:
    missing = []
    for prefix in arch_prefix_list:
        root = os.path.join(tool_path, "gcc", f"{prefix}-{TOOLCHAIN_VERSION}", prefix)
        directories = [os.path.join(root, "lib")]
        if prefix == "powerpc-unknown-linux-gnu":
            directories.append(os.path.join(root, "lib32"))
        elif prefix in ("powerpc64-unknown-linux-gnu", "powerpc64le-unknown-linux-gnu"):
            directories.append(os.path.join(root, "lib64"))
        expected = {}
        for source_dir in ("lib", "usr/lib"):
            source = os.path.join(root, "sysroot", source_dir)
            if not os.path.isdir(source):
                missing.append(f"{prefix}: missing sysroot source directory {source_dir}")
                continue
            for name in os.listdir(source):
                source_file = os.path.join(source, name)
                if os.path.isfile(source_file) or os.path.islink(source_file):
                    expected[name] = f"../sysroot/{source_dir}/{name}"
        for directory in directories:
            for name, target in expected.items():
                link = os.path.join(directory, name)
                # crosstool-NG may already install a real library here.
                if os.path.isfile(link) and not os.path.islink(link):
                    continue
                if not os.path.islink(link) or not os.path.exists(link) or os.path.realpath(link) != os.path.realpath(os.path.join(directory, target)):
                    missing.append(f"{prefix}: missing sysroot link {directory}/{name}")
    return missing


def missing_libmvec_links(tool_path: str = config.TOOL_PATH) -> list[str]:
    missing = []
    for link, source_file in libmvec_expected_links(tool_path).items():
        if not os.path.islink(link) or os.readlink(link) != source_file:
            missing.append(f"{link}: missing or different libmvec link")
    return missing


def libmvec_expected_links(tool_path: str = config.TOOL_PATH) -> dict[str, str]:
    tool_path = os.path.realpath(tool_path)
    prefix = "aarch64-unknown-linux-gnu"
    source = os.path.join(tool_path, "gcc", f"{prefix}-{TOOLCHAIN_VERSION}", prefix)
    expected = {
        "lib/libmvec.a": "lib/libmvec.a",
        "lib/libmvec.so": "lib/libmvec.a",
        "lib/libmvec.so.1": "lib/libmvec.a",
        "sysroot/lib/libmvec.so.1": "sysroot/lib/libmvec.so.1",
        "sysroot/usr/lib/libmvec.a": "sysroot/usr/lib/libmvec.a",
        "sysroot/usr/lib/libmvec.so": "sysroot/usr/lib/libmvec.so",
    }
    links = {}
    for version in ("8.5.0", "9.5.0"):
        root = os.path.join(tool_path, "gcc", f"{prefix}-{version}", prefix)
        for dest, target in expected.items():
            links[os.path.join(root, dest)] = os.path.join(source, target)
    return links


def managed_internal_symlinks(tool_path: str = config.TOOL_PATH) -> list[str]:
    managed = []
    for link, source in libmvec_expected_links(tool_path).items():
        if os.path.islink(link) and os.readlink(link) == source:
            managed.append(link)
    for prefix in arch_prefix_list:
        root = os.path.join(tool_path, "gcc", f"{prefix}-{TOOLCHAIN_VERSION}", prefix)
        directories = [os.path.join(root, "lib")]
        if prefix == "powerpc-unknown-linux-gnu":
            directories.append(os.path.join(root, "lib32"))
        elif prefix in ("powerpc64-unknown-linux-gnu", "powerpc64le-unknown-linux-gnu"):
            directories.append(os.path.join(root, "lib64"))
        for directory in directories:
            if not os.path.isdir(directory):
                continue
            for name in os.listdir(directory):
                link = os.path.join(directory, name)
                if not os.path.islink(link):
                    continue
                target = os.readlink(link)
                if target in (f"../sysroot/lib/{name}", f"../sysroot/usr/lib/{name}"):
                    managed.append(link)
    return sorted(set(managed))


def remove_internal_links(tool_path: str = config.TOOL_PATH, *, sudo_for_protected: bool = True) -> int:
    links = managed_internal_symlinks(tool_path)
    for link in links:
        if os.access(os.path.dirname(link), os.W_OK):
            os.unlink(link)
        elif sudo_for_protected:
            subprocess.run(["sudo", "unlink", link], check=True)
        else:
            raise PermissionError(f"Cannot remove protected link: {link}")
    return len(links)

def missing_link_targets(tool_path: str = config.TOOL_PATH, system_bin_dir: str = "/usr/bin") -> list[str]:
    missing = []
    for prefix in arch_prefix_list:
        root = os.path.join(tool_path, "gcc", f"{prefix}-{TOOLCHAIN_VERSION}")
        compiler = os.path.join(root, "bin", f"{prefix}-gcc")
        system_compiler = os.path.join(system_bin_dir, f"{prefix}-gcc")
        if not os.path.exists(system_compiler) or os.path.realpath(system_compiler) != os.path.realpath(compiler):
            missing.append(f"{prefix}: /usr/bin compiler link")
        for name in toolchain_list:
            target = os.path.join(root, "bin", f"{prefix}-{name}")
            link = os.path.join(system_bin_dir, f"{prefix}-{name}")
            if not os.path.exists(link) or os.path.realpath(link) != os.path.realpath(target):
                missing.append(f"{prefix}: {name} link")
    missing.extend(missing_internal_links(tool_path))
    missing.extend(missing_sysroot_links(tool_path))
    return missing


def _writable_directory(path: str) -> None:
    if not os.path.isdir(path):
        _writable_directory(os.path.dirname(path))
        os.mkdir(path)
    if not os.access(path, os.W_OK):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        os.chmod(path, mode | stat.S_IWUSR)


def _ensure_symlink(link: str, target: str, *, allow_regular: bool = False) -> None:
    _writable_directory(os.path.dirname(link))
    if os.path.islink(link):
        if os.path.exists(link) and (
            os.readlink(link) == target or (
                allow_regular and os.path.realpath(link) == os.path.realpath(os.path.join(os.path.dirname(link), target))
            )
        ):
            return
        raise RuntimeError(f"Existing link points elsewhere: {link}")
    if os.path.lexists(link):
        if allow_regular and os.path.isfile(link):
            return
        raise RuntimeError(f"Refusing to replace existing file: {link}")
    os.symlink(target, link)


def install_internal_links(tool_path: str = config.TOOL_PATH) -> None:
    for prefix in arch_prefix_list:
        root = os.path.join(tool_path, "gcc", f"{prefix}-{TOOLCHAIN_VERSION}", prefix)
        directories = [os.path.join(root, "lib")]
        if prefix == "powerpc-unknown-linux-gnu":
            directories.append(os.path.join(root, "lib32"))
        elif prefix in ("powerpc64-unknown-linux-gnu", "powerpc64le-unknown-linux-gnu"):
            directories.append(os.path.join(root, "lib64"))
        expected = {}
        for source_dir in ("lib", "usr/lib"):
            source = os.path.join(root, "sysroot", source_dir)
            if not os.path.isdir(source):
                raise RuntimeError(f"Missing sysroot directory: {source}")
            for name in os.listdir(source):
                source_file = os.path.join(source, name)
                if os.path.isfile(source_file) or os.path.islink(source_file):
                    expected[name] = f"../sysroot/{source_dir}/{name}"
        for directory in directories:
            for name, target in expected.items():
                _ensure_symlink(os.path.join(directory, name), target, allow_regular=True)

    # GCC 8 and 9 AArch64 sysroots lack libmvec; use the installed GCC 13 copy.
    for link, source in libmvec_expected_links(tool_path).items():
        if not os.path.isfile(source):
            raise RuntimeError(f"Missing libmvec source: {source}")
        _ensure_symlink(link, source)

    problems = missing_internal_links(tool_path) + missing_sysroot_links(tool_path) + missing_libmvec_links(tool_path)
    if problems:
        raise RuntimeError(f"Internal link setup incomplete: {'; '.join(problems[:5])}")


def link_toolchains(*, internal_only: bool = False, alternatives_only: bool = False) -> None:
    tool_path = config.TOOL_PATH
    missing_files = missing_installation_files(tool_path)
    if missing_files:
        for path in missing_files:
            logger.error(f"[-] Required toolchain file is missing: {path}")
        raise SystemExit(1)

    if not alternatives_only:
        install_internal_links(tool_path)
    if not internal_only:
        for prefix in arch_prefix_list:
            run_alternative_command(prefix, "install", sudo=os.geteuid() != 0)

    if internal_only:
        logger.info("[+] Internal compiler links are ready.")
    elif alternatives_only:
        if alternative_link_problems("linked", tool_path):
            raise SystemExit(1)
        logger.info("[+] GCC alternatives are linked.")
    elif not show_link_status():
        raise SystemExit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Register BinForge GCC alternatives and build links.")
    parser.add_argument("--status", action="store_true", help="Inspect links without changing files")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--internal-only", action="store_true", help="Set up or check internal sysroot and libmvec links only")
    modes.add_argument("--alternatives-only", action="store_true", help="Register or check /usr/bin alternatives only")
    args = parser.parse_args()
    if args.status:
        if args.internal_only:
            problems = missing_internal_links() + missing_sysroot_links() + missing_libmvec_links()
            print(f"Internal links: {'ready' if not problems else f'{len(problems)} missing or different'}")
            for problem in problems[:20]:
                print(f"  {problem}")
            raise SystemExit(bool(problems))
        if args.alternatives_only:
            problems = alternative_link_problems("linked")
            total = len(arch_prefix_list) * (len(toolchain_list) + 1)
            print(f"BinForge /usr/bin alternatives: {total - len(problems)}/{total} linked")
            for problem in problems[:20]:
                print(f"  {problem}")
            raise SystemExit(bool(problems))
        raise SystemExit(0 if show_link_status() else 1)
    link_toolchains(internal_only=args.internal_only, alternatives_only=args.alternatives_only)

# EOF
