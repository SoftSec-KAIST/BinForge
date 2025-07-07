import sys, os

from binforge.types_helper import *
from binforge.logging_helper import logger
from binforge.utils import execute_command, mkdir, is_directory_empty
from binforge.utils import is_directory_empty, get_compressed_extension
from binforge.utils import directory_exists, unzip_tar

import requests
'''
base_path: include sources, log, bins
'''
class Project:
    name: str
    category: str | None
    full_name: str | None
    version:  str | None
    valid: bool | None

    source_archive_name: str | None
    source_archive_path: str | None

    download_url: str | None
    dep_list: list[str] | None

    base_path: str
    source_path: str
    log_path: str
    bins_path: str
    benchmark_dir_path: str

    install_prefix: str | None
    use_git_clone: bool | None

    use_configure: bool | None
    use_make: bool | None

    use_configure: bool | None
    use_make: bool | None
    use_autoconf: bool | None
    skip_configure: bool | None
    use_cmake: bool | None

    cmake_file_path: str | None
    configure_file_path: str | None
    dep_libs: list[str] | None # For analyze_dependencies
    dep_headers: list[str] | None # For analyze_dependencies

    def __init__(self, name, base_path: str=''):
        self.name = name
        self.category = None
        self.base_path = base_path
        self.source_path = os.path.join(base_path, "sources", name)
        self.log_path = os.path.join(base_path, "log", name)
        self.bins_path = os.path.join(base_path, "bins", name)
        self.benchmark_dir_path = os.path.join(base_path, "benchmark")
        # proj: {base_path}/sources/{full_name}/{work_dir}/bb_install ({workd_dir} = {ful_name}_{comb})
        # dep: {dep_path}/install/{arch}/{name}
        self.install_prefix = None

        self.valid = True

        self.source_archive_name = None
        self.source_archive_path = None

        self.version = None
        self.full_name = None
        self.download_url = None
        self.dep_list = None

        self.use_git_clone = None

        ##! TOOD: Fix me
        self.use_configure = None
        self.use_make = None
        self.use_autoconf = None
        self.skip_configure = None
        self.use_cmake = None


    def print_name(self):
        print(f"[INFO] name: {self.name}")

    def print_info(self):
        print(f"[INFO] name: {self.name}")
        print(f"[INFO] category: {self.category}")
        print(f"[INFO] full_name: {self.full_name}")
        print(f"[INFO] version: {self.version}")
        print(f"[INFO] valid: {self.valid}")
        print(f"[INFO] source_archive_name: {self.source_archive_name}")
        print(f"[INFO] source_archive_path: {self.source_archive_path}")
        print(f"[INFO] download_url: {self.download_url}")
        print(f"[INFO] use_git_clone: {self.use_git_clone}")

        print(f"[INFO] base_path: {self.base_path}")
        print(f"[INFO] source_path: {self.source_path}")
        print(f"[INFO] log_path: {self.log_path}")
        print(f"[INFO] bins_path: {self.bins_path}")
        print(f"[INFO] benchmark_dir_path: {self.benchmark_dir_path}")

        print(f"[INFO] install_prefix: {self.install_prefix}")

        print(f"[INFO] self.use_configure: {self.use_configure}")
        print(f"[INFO] self.use_make: {self.use_make}")
        print(f"[INFO] self.use_autoconf: {self.use_autoconf}")
        print(f"[INFO] self.skip_configure: {self.skip_configure}")
        print(f"[INFO] self.use_cmake: {self.use_cmake}")


    def has_issue(self):
        return not self.valid

    def get_name(self):
        return self.name

    def set_full_name(self, name, version):
        if name != self.name or version != self.version:
            self.set_valid(False)
            logger.error(f"Incorrect name and/or version!")
            sys.exit(1)
        self.full_name = f"{self.name}-{self.version}"

    def get_valid(self):
        return self.valid

    def set_valid(self, valid):
        self.valid = valid

    def set_bins_path(self, path):
        self.bins_path = path

    def set_source_archive_name(self, name: str):
        self.source_archive_name = name

    def set_source_archive_path(self, path: str):
        self.source_archive_path = path

    def set_version(self, version: str):
        self.version = version

    def set_download_url(self, url: str):
        self.download_url = url

    def download_source_file(self) -> None:
        mkdir(self.source_path, exist_ok=True)
        assert self.download_url is not None, "self.download_url is None"
        url = self.download_url

        assert self.source_path is not None, "self.source_path is None."
        source_path = self.source_path

        full_name = self.full_name

        #! For github/gitlab
        if self.use_git_clone:
            source_archive_name = f"{full_name}.tar.gz"
            source_archive_path = os.path.join(source_path, source_archive_name)

            execute_command(f"git clone '{url}' {source_path}/{full_name}", DEBUG_LEVEL=-1, exit_on_error=False)
            execute_command(f"tar -cf {source_archive_name} {full_name}", directory=source_path, DEBUG_LEVEL=-1)
        #! Download from url
        else:
            extension = get_compressed_extension(url)
            source_archive_name = f"{self.full_name}{extension}"
            source_archive_path = os.path.join(source_path, source_archive_name)

            r = requests.get(url, stream=True)
            with open(source_archive_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=4096):
                    if chunk:
                        f.write(chunk)

            #! Use wget
            # cmd = f"wget -O {source_archive_path} {url}"
            # execute_command(cmd, directory=source_path, DEBUG_LEVEL=-1, exit_on_error=False)

        self.set_source_archive_name(source_archive_name)
        self.set_source_archive_path(source_archive_path)

        return

    def extract_source_file(self, DEBUG: bool=False):
        if self.has_issue():
            return False

        if self.use_git_clone:
            return

        assert self.source_archive_path is not None, "self.source_archive_path is None"
        assert self.full_name is not None, "self.full_name is None"
        source_code_path = unzip_tar(source_archive_path=self.source_archive_path, source_code_dir_name=self.full_name)

        assert source_code_path is not None, "source_code_path is None"
        if DEBUG:
            logger.debug(f"output_path: {source_code_path}")

        if not directory_exists(source_code_path) or is_directory_empty(source_code_path):
            self.valid = False


    def is_c_project(self):
        assert self.full_name is not None, "self.full_name is None."
        target_path = os.path.join(self.source_path, self.full_name) #! Fix me
        for root, dirs, files in os.walk(target_path):
            for file in files:
                if file.endswith('.c'):
                    return True
        return False

    def validate_scope(self):
        if not self.is_c_project():
            print(f"Not a C project: {self.name}")
            return False

        #! Valid project?
        if not self.source_path or not self.name:
            logger.error(f"[-] Invalid project (source code or version): {self.name}")
            return False

        work_dir_path = os.path.join(self.source_path, self.name)
        use_configure, use_cmake = False, False

        ##! CMakefile
        result_find_cmakefile = find_files("CMakeLists.txt", work_dir_path)
        if result_find_cmakefile:
            use_cmake = True
            cmake_file_path = result_find_cmakefile[0]
        else:
            #! No CMakefile
            use_cmake = False

        ##! configure file
        result_find_configure = find_files("configure", work_dir_path)
        if result_find_configure:
            configure_file_path = result_find_configure[0]
            use_configure = True
            contain_autoconf_header = validate_configure_file(configure_file_path)
        else:
            #! No configure file
            use_configure = False
            contain_autoconf_header = False

        # summary = 0b000
        # if use_cmake:
        #     summary |= 0b100
        # if use_configure:
        #     summary |= 0b010
        # if contain_autoconf_header:
        #     summary |= 0b001
        # print(f"[*] {self.name:<35}: {summary:03b}")

        if not use_cmake and not use_configure:
            return False

        valid_cmake = use_cmake
        valid_configure = use_configure and contain_autoconf_header

        if valid_cmake and valid_configure:
            return True # use configure
        elif valid_cmake and not valid_configure:
            self.use_cmake = True
            return True
        elif not valid_cmake and valid_configure:
            return True
        else:
            return False


def has_autoconf_generated_line(filepath):
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for i, line in enumerate(f):
            if i >= 100:
                break
            if line.startswith("# Generated by GNU Autoconf "):
                return True
    return False

def validate_configure_file(configure_file_path):
    # print("[*] Configure file path:", configure_file_path)
    contain_autoconf_header = has_autoconf_generated_line(configure_file_path)
    return contain_autoconf_header


def find_files(file_name, root_path):
    matches = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        if file_name in filenames:
            matches.append(os.path.join(dirpath, file_name))
    return matches

def cleanup_projects(projects: List[Project]):
    projects[:] = [project for project in projects if not project.has_issue()]

# EOF
