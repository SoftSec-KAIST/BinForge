import sys
import os
import re

from yaml import safe_load
from hashlib import md5
from inspect import signature
from tempfile import TemporaryDirectory
from colorama import Fore, Style
from threading import Thread
from subprocess import Popen, DEVNULL, PIPE
from dataclasses import dataclass
from multiprocessing import cpu_count, Pool
from concurrent.futures import ProcessPoolExecutor, as_completed

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

##! ========== Shell: Command ==========

def streamer(stream: IO[str], storage: list, log_func: Callable, DEBUG_LEVEL: int=0) -> None:
    for line in iter(stream.readline, ''):
        storage.append(line.strip())
        if log_func == logger.info and DEBUG_LEVEL >= 2:
            log_func(f"    {line.strip()}")
        elif log_func == logger.error and DEBUG_LEVEL != -1:
            log_func(f"{line.strip()}")

def execute_command(
    command: str | List[Dict],
    directory: str | None = None,
    env_vars: Dict | None = None,
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
    dynamic_values_list: List[Any],
    dynamic_arg_name: str = "arg_name",
    max_parallel_tasks: int = 10,
    **kwargs
) -> List[Any]:
    # Extract the function's signature to identify valid arguments
    func_sig = signature(func)
    valid_args = set(func_sig.parameters.keys())

    # Filter keyword arguments to include only those valid for the function
    static_args = {key: value for key, value in kwargs.items() if key in valid_args}

    results = []
    with ProcessPoolExecutor(max_workers=max_parallel_tasks) as executor:
        futures = []
        for item in dynamic_values_list:
            # Merge static arguments with the dynamic argument
            kwargs_with_dynamic_arg = {**static_args, dynamic_arg_name: item}
            # Submit the task to the executor
            futures.append(executor.submit(func, **kwargs_with_dynamic_arg))

            # If the number of tasks reaches the limit, process the completed tasks
            if len(futures) >= max_parallel_tasks:
                for f in as_completed(futures[:max_parallel_tasks]):
                    results.append(f.result())  # Collect the result of the completed task
                    futures.remove(f)  # Remove the completed task from the futures list

        # Process any remaining tasks
        for f in as_completed(futures):
            results.append(f.result())  # Collect the result of the remaining tasks

    return results

def run_command_in_pool(kwargs: Dict) -> CommandResult:
    return execute_command(**kwargs)

def parallel_execute_commands(commands: List[Dict], max_workers: int | None = None) -> List[CommandResult]:
    max_workers = max_workers or cpu_count()
    results = []

    # Using a Pool with limited max_workers
    with Pool(processes=max_workers) as pool:
        for result in pool.imap_unordered(run_command_in_pool, commands):
            results.append(result)  # Collect results as they are completed

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

def is_file_empty(file_path):
    return os.path.getsize(file_path) == 0

def load_yaml(file_path:str|Any):
    with open(file_path, 'r') as f:
        data = safe_load(f)
    return data

##! ========== Handle tar ==========

def get_tar_root_name(source_file_path: str) -> str | None:
    command_detect_tar_root = f"tar --auto-compress -tf {source_file_path} | sed -e 's@/.*@@' | uniq"
    command_wc = f"{command_detect_tar_root} | wc -l"
    tar_root_name = execute_command(command_detect_tar_root, DEBUG_LEVEL=0).stdout
    num_tar_root = execute_command(command_wc, DEBUG_LEVEL=0).stdout

    if tar_root_name == "." or int(num_tar_root) != 1:
        return None
    else:
        return tar_root_name

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

    tar_root_name = get_tar_root_name(source_archive_path)

    with TemporaryDirectory(dir="/tmp", delete=True) as temp_dir_path:
        command_extract = f"tar --auto-compress -xf {source_archive_path} -C {temp_dir_path}"
        command_move = f"mv {temp_dir_path}/{tar_root_name} {source_code_path}"
        execute_command(command_extract, DEBUG_LEVEL=0)
        execute_command(command_move, DEBUG_LEVEL=0)

    return source_code_path


def get_compressed_extension(url: str) -> str:
    pattern = re.compile(r"([\w\-.]+(\.tar\.gz|\.tar\.xz|\.tar.bz2|\.tgz|\.tar))")
    matches = pattern.findall(url)
    if matches:
        last_match = matches[-1]
        return last_match[1]
    else:
        logger.info("[-] Failed to get compressed file's extension from url:")
        logger.info(f"     {url}")
        logger.info("Set extension as \".archive\"")
        return ".archive"


def get_version_from_url(project_name: str, url: str) -> str:
    version_pattern = r"([0-9]+\.[0-9]+(?:\.[0-9]+)?)"
    version_match = re.search(version_pattern, url)
    if version_match:
        return version_match.group(1)
    else:
        logger.info(f"{project_name}'s version is not match.")
        logger.info(f"  URL: {url}")
        logger.info(f"  Set version as \"0.0.0\"")
        return "0.0.0"


def add_path_env(new_dir: str) -> None:
    current_path = os.environ.get("PATH", "")
    if new_dir not in current_path.split(os.pathsep):
        os.environ["PATH"] = f"{new_dir}{os.pathsep}{current_path}"

def set_path_env(new_dir: str) -> None:
    os.environ["PATH"] = new_dir

def count_leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip())

def modify_config(config_file_path: str) -> None:
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

def path_distance(path: str) -> int:
    return path.count('/')

def calculate_hash(filepath: str) -> str | None:
    try:
        with open(filepath, "rb") as f:
            return md5(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def remove_dup(binaries: list[str], path: str) -> list[str]:
    unique_binaries = {}
    for binary in binaries:
        binary = os.path.join(path, binary)
        name = os.path.basename(binary)
        distance = path_distance(binary)
        file_hash = calculate_hash(binary)

        if file_hash is None:
            continue

        if name not in unique_binaries:
            unique_binaries[name] = (binary, distance, file_hash)
        else:
            current_binary, current_distance, current_hash = unique_binaries[name]
            if file_hash == current_hash:
                if distance < current_distance or (distance == current_distance and binary < current_binary):
                    unique_binaries[name] = (binary, distance, file_hash)
            else:
                unique_binaries[f"{name}_{file_hash}"] = (binary, distance, file_hash)

    result = sorted([binary[0] for binary in unique_binaries.values()])
    return result


def split_by_package_blocks(input_str: str, n: int) -> list[str]:
    blocks = re.split(r'(?=^Package: )', input_str, flags=re.M)
    blocks = [b for b in blocks if b.strip()]
    result = []
    step = max(1, len(blocks) // n)
    for i in range(0, len(blocks), step):
        result.append("".join(blocks[i:i + step]))

    return result[:n]


# EOF
