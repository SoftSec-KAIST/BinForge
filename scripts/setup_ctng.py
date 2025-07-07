import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from binforge import config
from tempfile import TemporaryDirectory
from binforge.utils import directory_exists, execute_command, mkdir

from binforge.logging_helper import logger

def main() -> None:
    CTNG_PATH = config.CTNG_PATH

    mkdir(path=config.CTNG_TARBALL_PATH, exist_ok=True)

    if os.path.isfile(config.CTNG_BIN):
        logger.info("Crosstool-ng already installed.")
        return

    if not directory_exists(CTNG_PATH):
        #! crosstool-ng 1.27.0-rc1
        CTNG_GIT_REPO_URL = "https://github.com/crosstool-ng/crosstool-ng"
        with TemporaryDirectory(dir="/tmp", delete=True) as temp_dir_path:
            CTNG_1_27_0_RC_1_TAG = "59089fee3c50cb2c2e5601981a6e84b3a1c49f18"
            execute_command(f"git clone {CTNG_GIT_REPO_URL} {CTNG_PATH} 2>&1", DEBUG_LEVEL=1, exit_on_error=False)
            execute_command(f"git checkout {CTNG_1_27_0_RC_1_TAG} 2>&1", directory=CTNG_PATH, DEBUG_LEVEL=1, exit_on_error=False)

        #! crosstool-ng 1.27.0 release
        # CTNG_1_27_URL = "https://github.com/crosstool-ng/crosstool-ng/releases/download/crosstool-ng-1.27.0/crosstool-ng-1.27.0.tar.xz"
        # with tempfile.TemporaryDirectory(dir="/tmp", delete=True) as temp_dir_path:
        #     execute_command(f"wget -O {temp_dir_path}/crosstool-ng-1.27.0.tar.xz {CTNG_1_27_URL} 2>&1", DEBUG_LEVEL=2)
        #     execute_command(f"tar -xf {temp_dir_path}/crosstool-ng-1.27.0.tar.xz -C {temp_dir_path}", DEBUG_LEVEL=2)
        #     execute_command(f"mv {temp_dir_path}/crosstool-ng-1.27.0 {CTNG_PATH}")


        ##! Test for zunnoor
        #! The latest crosstool-ng
        # CTNG_GIT_REPO_URL = "https://github.com/crosstool-ng/crosstool-ng"
        # with TemporaryDirectory(dir="/tmp", delete=True) as temp_dir_path:
        #     CTNG_LATEST = "93d5d7664faaf3a0e63c58cd74105c3c8364130b"
        #     execute_command(f"git clone {CTNG_GIT_REPO_URL} {CTNG_PATH} 2>&1", DEBUG_LEVEL=1, exit_on_error=False)
        #     execute_command(f"git checkout {CTNG_LATEST} 2>&1", directory=CTNG_PATH, DEBUG_LEVEL=1, exit_on_error=False)

    execute_command(f"make distclean", directory=CTNG_PATH, exit_on_error=False, DEBUG_LEVEL=-1)
    execute_command(f"./bootstrap 2>&1", directory=CTNG_PATH, exit_on_error=False, DEBUG_LEVEL=2)
    execute_command(f"./configure --enable-local", directory=CTNG_PATH, DEBUG_LEVEL=2)
    execute_command(f"make -j {config.NUM_JOBS} -l {config.MAX_JOBS}", directory=CTNG_PATH, DEBUG_LEVEL=2)


if __name__ == "__main__":
    main()

# EOF
