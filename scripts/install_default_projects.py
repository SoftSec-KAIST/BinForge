import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from binforge.utils import execute_command, load_yaml
from binforge.types_helper import *
from binforge import config

def update_apt_manager() -> None | NoReturn:
    cmd_update_apt = "sudo apt-get update"
    execute_command(cmd_update_apt, exit_on_error=True, DEBUG_LEVEL=1)


def apt_install(project: str) -> None | NoReturn:
    # cmd_install_project = f"DEBIAN_FRONTEND=noninteractive sudo apt-get install -y {project}"
    cmd_install_project = f"sudo apt-get install -y {project}"
    execute_command(cmd_install_project, exit_on_error=True, DEBUG_LEVEL=1)


def main() -> None:
    ##! run "sudo locale-gen en_US.UTF-8"
    execute_command("sudo locale-gen en_US.UTF-8", exit_on_error=True, DEBUG_LEVEL=1)
    update_apt_manager()
    apt_install("apt-utils")

    default_deps = load_yaml(config.DEFAULT_APT_PROJECTS_PATH)
    default_projects = default_deps['default_projects']

    for project in default_projects:
        apt_install(project)

if __name__ == "__main__":
    main()

# EOF
