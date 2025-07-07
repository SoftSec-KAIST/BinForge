import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from binforge import config
from binforge.utils import execute_command
from binforge.logging_helper import logger


def main() -> None:
    TOOL_PATH = config.TOOL_PATH

    dirs_to_remove = ["crosstool-ng", "ctng_tarballs", "llvm-project", "gcc/build", "clang/logs"]
    for dir_to_remove in dirs_to_remove:
        path_to_remove = os.path.join(TOOL_PATH, dir_to_remove)
        logger.info(f"Path to remove: {path_to_remove}")
        cmd_remove = f"rm -rf {path_to_remove}"
        execute_command(cmd_remove, DEBUG_LEVEL=0)


if __name__ == "__main__":
    main()

# EOF
