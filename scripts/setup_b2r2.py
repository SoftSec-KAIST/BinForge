#!/usr/bin/env python3
"""Install or inspect BinForge's pinned B2R2 dependency."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT_ROOT / "support" / "b2r2_validator" / "BinForge.B2R2Validator.csproj"
PINNED_VERSION = "0.10.1"
CLI_PACKAGE = "B2R2.RearEnd.Launcher"
API_PACKAGE = "B2R2.FrontEnd.BinFile"
B2R2_ROOT = PROJECT_ROOT / "tools" / "b2r2"
CLI_DIR = B2R2_ROOT / "bin"
CLI = CLI_DIR / "b2r2"
CLI_WRAPPER = B2R2_ROOT / "b2r2"
VALIDATOR_DIR = B2R2_ROOT / "validator"
VALIDATOR = VALIDATOR_DIR / "BinForge.B2R2Validator.dll"
EXPECTED_VERSION = "0.1.0 (B2R2 0.10.1)"


def find_dotnet() -> str:
    candidates = [Path.home() / ".dotnet" / "dotnet"]
    if command := shutil.which("dotnet"):
        candidates.append(Path(command))
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file():
            continue
        result = subprocess.run(
            [str(candidate), "--version"], capture_output=True, text=True, check=False
        )
        try:
            compatible = int(result.stdout.split(".", 1)[0]) >= 10
        except ValueError:
            compatible = False
        if result.returncode == 0 and compatible:
            return str(candidate)
    raise RuntimeError(".NET SDK 10 or later is required; no compatible dotnet was found")


def dotnet_environment(dotnet: str) -> dict[str, str]:
    env = os.environ.copy()
    dotnet_root = str(Path(dotnet).resolve().parent)
    env["DOTNET_ROOT"] = dotnet_root
    env["PATH"] = f"{dotnet_root}{os.pathsep}{env.get('PATH', '')}"
    return env


def cli_status(dotnet: str) -> tuple[bool, str]:
    if not CLI.is_file():
        return False, f"missing: {CLI}"
    if not CLI_WRAPPER.is_file():
        return False, f"missing launcher: {CLI_WRAPPER}"
    result = subprocess.run(
        [str(CLI_WRAPPER), "--version"], capture_output=True, text=True, check=False,
    )
    version = result.stdout.strip()
    if result.returncode != 0:
        return False, f"B2R2 CLI exited with {result.returncode}: {result.stderr.strip()}"
    if not version.startswith(f"v{PINNED_VERSION}+"):
        return False, f"version mismatch: expected {PINNED_VERSION}, found {version}"
    return True, version


def write_cli_wrapper() -> None:
    """Create a relocatable launcher that finds a user or system .NET runtime."""
    CLI_WRAPPER.write_text(
        """#!/bin/sh
set -eu
dotnet_bin=
for candidate in \"$HOME/.dotnet/dotnet\" \"$(command -v dotnet 2>/dev/null || true)\"; do
    [ -x \"$candidate\" ] || continue
    version=$(\"$candidate\" --version 2>/dev/null || true)
    major=${version%%.*}
    case $major in
        ''|*[!0-9]*) continue ;;
    esac
    if [ \"$major\" -ge 10 ]; then
        dotnet_bin=$candidate
        break
    fi
done
if [ -z \"$dotnet_bin\" ]; then
    echo \"B2R2 requires a .NET 10 runtime; dotnet was not found\" >&2
    exit 1
fi
DOTNET_ROOT=$(dirname \"$(readlink -f \"$dotnet_bin\")\")
export DOTNET_ROOT
exec \"$(dirname \"$0\")/bin/b2r2\" \"$@\"
"""
    )
    CLI_WRAPPER.chmod(0o755)


def installed_cli_is_current(dotnet: str) -> bool:
    if not CLI.is_file():
        return False
    result = subprocess.run(
        [str(CLI), "--version"], capture_output=True, text=True, check=False,
        env=dotnet_environment(dotnet),
    )
    return result.returncode == 0 and result.stdout.strip().startswith(
        f"v{PINNED_VERSION}+"
    )


def validator_status(dotnet: str) -> tuple[bool, str]:
    if not VALIDATOR.is_file():
        return False, f"missing: {VALIDATOR}"
    result = subprocess.run(
        [dotnet, str(VALIDATOR), "--version"], capture_output=True, text=True, check=False
    )
    version = result.stdout.strip()
    if result.returncode != 0:
        return False, f"validator exited with {result.returncode}: {result.stderr.strip()}"
    if version != EXPECTED_VERSION:
        return False, f"version mismatch: expected {EXPECTED_VERSION}, found {version}"
    return True, version


def latest_nuget_version(dotnet: str, package: str) -> str:
    result = subprocess.run(
        [dotnet, "package", "search", package, "--exact-match", "--format", "json"],
        capture_output=True, text=True, check=True,
    )
    payload = json.loads(result.stdout)
    versions = [
        item["version"]
        for source in payload.get("searchResult", [])
        for item in source.get("packages", [])
        if item.get("id", "").lower() == package.lower()
        and re.fullmatch(r"\d+(?:\.\d+)+", item.get("version", ""))
    ]
    if not versions:
        raise RuntimeError(f"NuGet returned no stable versions for {package}")
    return max(versions, key=lambda version: tuple(map(int, version.split("."))))


def print_status(dotnet: str, *, refresh: bool) -> int:
    sdk = subprocess.run(
        [dotnet, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    cli_ready, cli_detail = cli_status(dotnet)
    validator_ready, validator_detail = validator_status(dotnet)
    print(f".NET SDK: {sdk}")
    print(f"B2R2 CLI: {'ready' if cli_ready else 'unavailable'} ({cli_detail})")
    print(
        "B2R2 BinForge validator: "
        f"{'ready' if validator_ready else 'unavailable'} ({validator_detail})"
    )
    if refresh:
        cli_latest = latest_nuget_version(dotnet, CLI_PACKAGE)
        api_latest = latest_nuget_version(dotnet, API_PACKAGE)
        print(f"NuGet latest {CLI_PACKAGE}: {cli_latest}")
        print(f"NuGet latest {API_PACKAGE}: {api_latest}")
        current = cli_latest == PINNED_VERSION and api_latest == PINNED_VERSION
        print(f"Pinned B2R2 {PINNED_VERSION}: {'current' if current else 'update available'}")
    else:
        print("NuGet latest: not queried (use --refresh-status)")
        current = True
    return 0 if cli_ready and validator_ready and current else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--status", action="store_true", help="Check the installed validator without changing it"
    )
    actions.add_argument(
        "--refresh-status", action="store_true",
        help="Check the installation and query NuGet for the latest stable B2R2 version",
    )
    args = parser.parse_args()

    if args.status or args.refresh_status:
        try:
            dotnet = find_dotnet()
            return print_status(dotnet, refresh=args.refresh_status)
        except (RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
            print(f"B2R2: unavailable ({error})")
            return 1

    dotnet = find_dotnet()
    CLI_DIR.mkdir(parents=True, exist_ok=True)
    cli_current = installed_cli_is_current(dotnet)
    if not cli_current:
        command = "update" if CLI.is_file() else "install"
        subprocess.run(
            [
                dotnet, "tool", command, "--tool-path", str(CLI_DIR),
                CLI_PACKAGE, "--version", PINNED_VERSION,
            ],
            check=True,
        )
    write_cli_wrapper()
    subprocess.run([dotnet, "restore", str(SOURCE), "--locked-mode"], check=True)
    subprocess.run(
        [
            dotnet,
            "publish",
            str(SOURCE),
            "--no-restore",
            "--configuration",
            "Release",
            "--output",
            str(VALIDATOR_DIR),
        ],
        check=True,
    )
    return print_status(dotnet, refresh=False)


if __name__ == "__main__":
    sys.exit(main())
