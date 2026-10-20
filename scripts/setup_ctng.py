import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import argparse
import re
import subprocess

from binforge import config
from binforge.logging_helper import logger
from binforge.utils import directory_exists, execute_command, mkdir

CTNG_GIT_REPO_URL = "https://github.com/crosstool-ng/crosstool-ng"
CTNG_COMMIT = "59089fee3c50cb2c2e5601981a6e84b3a1c49f18"
CTNG_VERSION = "1.27.0_rc1"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set up crosstool-ng.")
    parser.add_argument("--status", action="store_true", help="Check the executable, version, and source commit without building")
    return parser.parse_args()


def run_required(command: str, directory: str | None = None, debug_level: int = 2) -> None:
    result = execute_command(
        command,
        directory=directory,
        exit_on_error=False,
        DEBUG_LEVEL=debug_level,
    )
    if result.returncode != 0:
        logger.error(f"Command failed with return code {result.returncode}: {command}")
        sys.exit(result.returncode)


def source_commit_matches(path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == CTNG_COMMIT


def installation_status() -> tuple[bool, str]:
    if not source_commit_matches(config.CTNG_PATH):
        return False, f"Source is missing or is not at pinned commit {CTNG_COMMIT}"
    if not os.path.isfile(config.CTNG_BIN) or not os.access(config.CTNG_BIN, os.X_OK):
        return False, f"Executable is missing: {config.CTNG_BIN}"
    result = subprocess.run(
        [config.CTNG_BIN, "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    version_line = rf"^This is crosstool-NG version {re.escape(CTNG_VERSION)}$"
    if result.returncode != 0 or not re.search(version_line, result.stdout, re.MULTILINE):
        return False, f"Executable does not report crosstool-NG {CTNG_VERSION}"
    return True, f"crosstool-NG {CTNG_VERSION} at {CTNG_COMMIT}"


def main() -> None:
    args = parse_arguments()

    CTNG_PATH = config.CTNG_PATH # CTNG_PATH = os.path.join(TOOL_PATH, "crosstool-ng")

    if args.status:
        installed, message = installation_status()
        print(message)
        if not installed:
            sys.exit(1)
        return

    if os.path.isfile(config.CTNG_BIN):
        installed, message = installation_status()
        if installed:
            logger.info(message)
            return
        raise RuntimeError(message)

    mkdir(path=config.CTNG_TARBALL_PATH, exist_ok=True)

    if not directory_exists(CTNG_PATH):
        run_required(f"git clone {CTNG_GIT_REPO_URL} {CTNG_PATH} 2>&1", debug_level=1)
        run_required(f"git checkout {CTNG_COMMIT} 2>&1", directory=CTNG_PATH, debug_level=1)

    if not source_commit_matches(CTNG_PATH):
        raise RuntimeError(f"Source is not at pinned commit {CTNG_COMMIT}: {CTNG_PATH}")

    # crosstool-ng-1.27.0: 70c2b00ad79a0a21a48e5a0eedc9f91c374af21d

    if os.path.isfile(os.path.join(CTNG_PATH, "Makefile")):
        run_required("make distclean", directory=CTNG_PATH, debug_level=-1)
    print(config.OS_VERSION)
    if config.OS_VERSION == "18.04":
        run_required("git fetch origin 48e5f24e07ee82572957f12815a2e3acc248e1c5 2>&1", directory=CTNG_PATH, debug_level=1)
        run_required("git checkout 48e5f24e07ee82572957f12815a2e3acc248e1c5 -- m4/host-cpu-c-abi.m4", directory=CTNG_PATH, debug_level=1)

    run_required("./bootstrap 2>&1", directory=CTNG_PATH)
    run_required("./configure --enable-local", directory=CTNG_PATH)
    run_required(f"make -j {config.NUM_JOBS} -l {config.MAX_JOBS}", directory=CTNG_PATH)
    installed, message = installation_status()
    if not installed:
        raise RuntimeError(message)


if __name__ == "__main__":
    main()

# EOF
