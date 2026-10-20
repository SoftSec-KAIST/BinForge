import sys
import os
import re
import shlex
import yaml
from inspect import signature
from tempfile import TemporaryDirectory
from colorama import Fore, Style
from threading import Thread
from subprocess import Popen, DEVNULL, PIPE
from dataclasses import dataclass
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

from binforge.types_helper import *
from binforge.logging_helper import *

@dataclass
class CommandResult:
    command: str
    stdout: str
    stderr: str
    returncode: int


class CriticalSTDError(Exception):
    pass

COLOR_GREEN = Fore.GREEN
COLOR_BLUE = Fore.BLUE
COLOR_RED = Fore.RED
COLOR_RESET = Style.RESET_ALL


def normalize_version(version: Any, default: str = "latest") -> str:
    if version is None:
        return default

    version_str = str(version).strip()
    return version_str if version_str else default

def load_yaml(file_path: str | Any) -> Any:
    with open(file_path, 'r') as f:
        data = yaml.load(f, Loader=yaml.BaseLoader)
    return data

##! ========== Shell: Command ==========

def streamer(stream: IO[str], storage: list, log_func: Callable, DEBUG_LEVEL: int=0) -> None:
    for line in iter(stream.readline, ''):
        storage.append(line.strip())
        if log_func == logger.info and DEBUG_LEVEL >= 2:
            log_func(f"    {line.strip()}")
        elif log_func == logger.error and DEBUG_LEVEL != -1:
            log_func(f"{line.strip()}")

def execute_command(
    command: str | list[dict],
    directory: str | None = None,
    env_vars: dict | None = None,
    replace_env: bool = False,
    exit_on_error: bool = True,
    DEBUG_LEVEL: int = 2,
    NO_STREAM: bool = False,
) -> CommandResult | NoReturn:
    if env_vars is not None:
        if replace_env:
            # Ignore existing environment variables and use the provided env
            environment = env_vars
        else:
            # Add the provided env to the existing environment variables
            environment = os.environ.copy()
            for key, value in env_vars.items():
                if key == "PATH" and "PATH" in environment:
                    # Add the new PATH value to the front of the existing PATH
                    environment[key] = f"{value}:{environment[key]}"
                else:
                    # Add or update other environment variables
                    environment[key] = value
    else:
        # Use the default environment variables if no env is provided
        environment = None

    assert isinstance(command, str)

    try:
        if DEBUG_LEVEL >= 1:
            logger.info(f"{COLOR_BLUE}Command{COLOR_RESET}: {COLOR_GREEN}{command}{COLOR_RESET}")

        if NO_STREAM:
            process = Popen(
                args=command,
                executable="/bin/bash",
                shell=True,
                cwd=directory,
                stdout=DEVNULL,
                stderr=DEVNULL,
                env=environment,
                text=True
            )
            process.wait()
            stdout = ""
            stderr = ""
        else:
            process = Popen(
                command,
                executable="/bin/bash",
                shell=True,
                cwd=directory,
                stdout=PIPE,
                stderr=PIPE,
                env=environment,
                text=True
            )

            # Read stdout and stderr streams in parallel
            stdout_lines = []
            stderr_lines = []

            stdout_thread = Thread(target=streamer, args=(process.stdout, stdout_lines, logger.info, DEBUG_LEVEL))
            stderr_thread = Thread(target=streamer, args=(process.stderr, stderr_lines, logger.error, DEBUG_LEVEL))

            stdout_thread.start()
            stderr_thread.start()

            # Wait for process and threads
            process.wait()
            stdout_thread.join()
            stderr_thread.join()
            process.stdout.close()
            process.stderr.close()

            # Collect outputs
            stdout = "\n".join(stdout_lines)
            stderr = "\n".join(stderr_lines)

        # DEBUG_LEVEL outputs
        if DEBUG_LEVEL >= 1:
            if process.returncode == 0:
                logger.info(f"{COLOR_BLUE}Returncode{COLOR_RESET}: {COLOR_GREEN}{process.returncode}{COLOR_RESET}")
            else:
                logger.error(f"Returncode: {process.returncode}")

        if stderr and exit_on_error:
            raise CriticalSTDError()

        return CommandResult(
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=process.returncode,
        )

    except Exception as e:
        logger.error(f"Failed to execute command: \"{command}\"")
        logger.error("Error info:", exc_info=True)
        if exit_on_error:
            exit_program(1)
        else:
            raise


def parallel_execute_func(
    func: Callable,
    dynamic_values_list: list[Any],
    dynamic_arg_name: str = "arg_name",
    max_parallel_tasks: int = 10,
    **kwargs
) -> list[Any]:
    # Extract the function's signature to identify valid arguments
    func_sig = signature(func)
    valid_args = set(func_sig.parameters.keys())

    # Filter keyword arguments to include only those valid for the function
    static_args = {key: value for key, value in kwargs.items() if key in valid_args}

    results = [None] * len(dynamic_values_list)
    items = iter(enumerate(dynamic_values_list))
    with ProcessPoolExecutor(max_workers=max_parallel_tasks) as executor:
        pending = {}

        def submit_next() -> bool:
            try:
                index, item = next(items)
            except StopIteration:
                return False
            kwargs_with_dynamic_arg = {**static_args, dynamic_arg_name: item}
            pending[executor.submit(func, **kwargs_with_dynamic_arg)] = index
            return True

        for _ in range(min(max_parallel_tasks, len(dynamic_values_list))):
            submit_next()

        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                results[pending.pop(future)] = future.result()
            for _ in completed:
                submit_next()

    return results


##! ========== LOGGING ==========

def exit_program(exit_code: int=1) -> NoReturn:
    logger.error(f"ReturnCode: {exit_code}")
    sys.exit(exit_code)

##! ========== SHELL: ENV ==========

def set_env_variable(env_var_name: str, env_var_content: str | int) -> None:
    if type(env_var_content) == int:
        print("[-] Environment variables must be of type string, ", end='')
        print(f"so {env_var_content} have been converted to string")
        os.environ[env_var_name] = str(env_var_content)
    else:
        assert not isinstance(env_var_content, int), "env_var_content is int"
        os.environ[env_var_name] = env_var_content

def get_env_variable(env_var_name: str) -> str:
    return "" if (env := os.environ.get(env_var_name)) is None else env

def print_env_variable(env_var_name: str) -> None:
    env_var_content = os.environ.get(env_var_name)
    if env_var_content is not None:
        print(f"[*] {env_var_name}: {env_var_content}")
    else:
        logger.error(f"Environment variable \"{env_var_name}\" is not set.")

def add_path_env(new_dir: str) -> None:
    current_path = os.environ.get("PATH", "")
    if new_dir not in current_path.split(os.pathsep):
        os.environ["PATH"] = f"{new_dir}{os.pathsep}{current_path}"

def set_path_env(new_dir: str) -> None:
    os.environ["PATH"] = new_dir

##! ========== SHELL: MISC ==========

def mkdir(path: str, exist_ok: bool = True) -> None:
    if not exist_ok and os.path.isdir(path):
        logger.error(f"Directory path {path} already exists.")
        exit(1)

    os.makedirs(path, exist_ok=True)

def directory_exists(path: str) -> bool:
    return os.path.isdir(path)

def is_directory_empty(path: str) -> bool:
    return not os.listdir(path)

def file_exists(path: str) -> bool:
    return os.path.isfile(path)

def is_file_empty(file_path: str) -> bool:
    return os.path.getsize(file_path) == 0

def find_files(file_name, root_path):
    matches = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        if file_name in filenames:
            matches.append(os.path.join(dirpath, file_name))
    return matches

##! ========== Handle tar ==========

def get_tar_root_name(source_file_path: str) -> str | None:
    result = execute_command(
        f"tar --auto-compress -tf {shlex.quote(source_file_path)}",
        DEBUG_LEVEL=0,
        exit_on_error=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Cannot list source archive: {source_file_path}")
    roots = {member.split("/", 1)[0] for member in result.stdout.splitlines() if member}
    if len(roots) != 1 or "." in roots:
        return None
    return next(iter(roots))

def unzip_tar(
    source_archive_path: str,
    source_code_dir_name: str='',
) -> str | None | NoReturn:

    if not file_exists(source_archive_path):
        logger.error(f"File not exist: {source_archive_path}")
        logger.error(f"Failed to unzip: {source_archive_path}")
        return None

    source_code_path = os.path.join(os.path.dirname(source_archive_path), source_code_dir_name)

    if directory_exists(source_code_path):
        if not is_directory_empty(source_code_path):
            return source_code_path
        os.rmdir(source_code_path)

    tar_root_name = get_tar_root_name(source_archive_path)
    if tar_root_name is None:
        raise ValueError(f"Expected one top-level directory in source archive: {source_archive_path}")

    with TemporaryDirectory(dir="/tmp", delete=True) as temp_dir_path:
        command_extract = f"tar --auto-compress -xf {shlex.quote(source_archive_path)} -C {shlex.quote(temp_dir_path)}"
        result = execute_command(command_extract, DEBUG_LEVEL=0, exit_on_error=False)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to extract source archive: {source_archive_path}")
        extracted_root = os.path.join(temp_dir_path, tar_root_name)
        if not os.path.isdir(extracted_root):
            raise RuntimeError(f"Archive root is not a directory: {source_archive_path}")
        result = execute_command(
            f"mv {shlex.quote(extracted_root)} {shlex.quote(source_code_path)}",
            DEBUG_LEVEL=0,
            exit_on_error=False,
        )
        if result.returncode != 0 or not os.path.isdir(source_code_path):
            raise RuntimeError(f"Failed to place extracted source: {source_code_path}")

    return source_code_path


def get_compressed_extension(url: str) -> str:
    pattern = re.compile(r"([\w\-.]+(\.tar\.gz|\.tar\.xz|\.tar.bz2|\.tgz|\.tar))")
    matches = pattern.findall(url)
    if matches:
        last_match = matches[-1]
        return last_match[1][1:]
    else:
        logger.info("[-] Failed to get compressed file's extension from url:")
        logger.info(f"     {url}")
        logger.info("Set extension as \".archive\"")
        return ".archive"

##! ========== etc. ==========

def modify_config(config_file_path: str) -> None:
    def count_leading_spaces(line: str) -> int:
        return len(line) - len(line.lstrip())

    command_backup = f"cp {config_file_path} {config_file_path}.backup"
    execute_command(command_backup, DEBUG_LEVEL=0)
    with open(config_file_path, "r") as file:
        lines = file.readlines()
        new_lines = []
        for line in lines:
            if line.startswith("#") and "-O" in line:
                new_lines.append(line)
                continue
            matches = re.findall(r'-O(?:[0-3sz]|fast)?', line)
            if matches:
                if "checking" in line:
                    new_lines.append(line)
                    continue
                if "sed " in line:
                    #! for GNU cflow project
                    if line.strip().startswith("CFLAGS=\"") and line.strip().endswith("$CFLAGS\""):
                        new_line = ' ' * count_leading_spaces(line) + "CFLAGS=\"`echo $save_CFLAGS` $CFLAGS\"" + '\n'
                        line = new_line
                    #! for GNU gawk project
                    if "/-O2/s///" in line:
                        # Because this line(s) does not logically alter the optimization options.
                        pass
                    new_lines.append(line)
                    continue
                line = re.sub(r'-O(?:[0-3sz]|fast)', '', line)

                if "-OUT" in line:
                    new_lines.append(line)
                    continue

                line = re.sub(r'-O', '', line)
            new_lines.append(line)

    with open(config_file_path, "w") as file:
        file.writelines(new_lines)

    #! for debugging
    # command_diff = f"diff {config_file_path}.backup {config_file_path}; true"
    # execute_command(command_diff, DEBUG_LEVEL=2)


def is_valid_git_repo(path: str, debug_level: int=-1):
    command = f"git -C {path} rev-parse --show-toplevel"
    result = execute_command(command, exit_on_error=False, DEBUG_LEVEL=debug_level)

    if result.returncode == 0 and result.stdout.strip() == path:
        return True

    if debug_level >= 0:
        logger.info(f"[*] Command: `{command}`")
        if result.returncode != 0:
            logger.info(f"[-] command returncode: {result.returncode}")
        elif result.stdout.strip() != path:
            logger.info(f"[-] command stdout: {result.stdout} (it should be '{path}')")

    return False


def git_clone(
    repo_url: str,
    target_dir: str,
    debug_level: int=-1,
) -> CommandResult | bool | None:
    mkdir(target_dir, exist_ok=True)

    is_valid_repo = is_valid_git_repo(target_dir, debug_level=1)
    if is_valid_repo:
        logger.info(f"Repository {repo_url} is already cloned at directory '{target_dir}'.")
        return True
    else:
        # Cloning is considered aborted due to network, permissions, etc.
        command_cleanup = f"rm -rf {target_dir}"
        execute_command(command_cleanup, exit_on_error=True, DEBUG_LEVEL=1)

    # Check if the directory exists and is non-empty
    if not os.path.isdir(target_dir) or not os.listdir(target_dir):
        logger.info(f"[*] Cloning repo at '{target_dir}'")
        #! Since output of git clone is stderr
        command_clone = f"git clone {repo_url} {target_dir} 2>&1"
        execute_command(command_clone, exit_on_error=True, DEBUG_LEVEL=debug_level)
    else:
        logger.info(f"Directory '{target_dir}' already exists and is not empty.")

# EOF
