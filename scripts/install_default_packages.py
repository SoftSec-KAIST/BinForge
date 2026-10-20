import sys
import os
import argparse
import shlex
import subprocess

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from binforge import config
from binforge.types_helper import *
from binforge.utils import execute_command, load_yaml


def run_apt_command(command: str) -> None:
    result = execute_command(command, exit_on_error=False, DEBUG_LEVEL=1)
    if result.returncode != 0:
        raise RuntimeError(f"APT command failed with exit code {result.returncode}: {command}")


def update_apt_manager() -> None:
    cmd_update_apt = "sudo apt-get update -o APT::Update::Error-Mode=any"
    run_apt_command(cmd_update_apt)


def apt_install(packages: list[str]) -> None:
    if not packages:
        return
    package_args = " ".join(shlex.quote(package) for package in packages)
    run_apt_command(f"sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y {package_args}")


def get_default_packages() -> list[str]:
    packages = load_yaml(config.CONFIG['DEFAULT_APT_PACKAGES_PATH'])['default_packages']
    return list(dict.fromkeys(packages))


def package_versions(packages: list[str]) -> dict[str, tuple[str, str]]:
    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    result = subprocess.run(
        ['apt-cache', 'policy', *packages],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    policies: dict[str, dict[str, str]] = {}
    current_package = None
    for line in result.stdout.splitlines():
        if line and not line[0].isspace() and line.endswith(':'):
            current_package = line[:-1]
            policies[current_package] = {}
            continue
        if current_package is None:
            continue
        stripped = line.strip()
        for key in ('Installed', 'Candidate'):
            if stripped.startswith(f'{key}:'):
                policies[current_package][key] = stripped.partition(':')[2].strip()
    versions = {}
    for package in packages:
        fields = policies.get(package)
        if fields is None:
            versions[package] = ('(none)', '(none)')
        elif 'Installed' in fields and 'Candidate' in fields:
            versions[package] = (fields['Installed'], fields['Candidate'])
        else:
            raise RuntimeError(f'Could not read APT policy for {package}')
    return versions


def package_state(installed: str, candidate: str) -> str:
    if candidate == '(none)':
        return 'unavailable'
    if installed == '(none)':
        return 'missing'
    if installed == candidate:
        return 'current'
    result = subprocess.run(
        ['dpkg', '--compare-versions', installed, 'lt', candidate],
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError('Could not compare APT package versions')
    return 'upgrade' if result.returncode == 0 else 'newer-than-candidate'


def show_status(packages: list[str], refreshed: bool = False) -> int:
    if refreshed:
        print('APT indexes: refreshed successfully; versions are repository candidates')
    else:
        print('APT indexes: local cache only; remote freshness is unverified')
    package_names = list(dict.fromkeys(['apt', 'apt-utils', *packages]))
    versions = package_versions(package_names)
    rows = [('package', 'installed', 'candidate', 'state')]
    counts: dict[str, int] = {}
    for package in package_names:
        installed, candidate = versions[package]
        state = package_state(installed, candidate)
        counts[state] = counts.get(state, 0) + 1
        displayed_candidate = '=' if candidate == installed else candidate
        rows.append((package, installed, displayed_candidate, state))
    widths = [max(len(row[column]) for row in rows) for column in range(4)]
    for row in rows:
        print('  '.join(value.ljust(widths[column]) for column, value in enumerate(row)).rstrip())
    print('Candidate = means the same version as installed.')
    print('Summary: ' + ', '.join(f'{name}={count}' for name, count in sorted(counts.items())))
    return 1 if any(counts.get(name, 0) for name in ('upgrade', 'missing', 'unavailable')) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Install or inspect required APT packages')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--status', action='store_true', help='Inspect installed and candidate versions without changing APT state')
    actions.add_argument('--refresh-status', action='store_true', help='Update APT indexes, then inspect versions without installing packages')
    args = parser.parse_args()
    default_packages = get_default_packages()

    if args.status or args.refresh_status:
        if args.refresh_status:
            update_apt_manager()
        return show_status(default_packages, refreshed=args.refresh_status)

    update_apt_manager()
    apt_install(list(dict.fromkeys(["apt-utils", *default_packages])))
    return 0


if __name__ == "__main__":
    sys.exit(main())


# EOF
