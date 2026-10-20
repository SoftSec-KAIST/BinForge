import sys, os
import hashlib
import shlex
import shutil
from glob import glob
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from binforge import config
from binforge.types_helper import *
from binforge.logging_helper import logger
from binforge.utils import execute_command, mkdir, get_compressed_extension, normalize_version, unzip_tar


class Project:
    name: str

    base_path: str
    source_path: str
    log_path: str
    bins_path: str
    benchmark_dir_path: str
    install_prefix: str | None

    source_type: str | None
    source_value: str | None
    source_commit: str | None

    conf_options: str | None
    cflags: str | None
    ldflags: str | None

    dep_list: list[str] | None

    #! ===================

    full_name: str | None
    version:  str | None

    source_archive_path: str | None

    dep_path: str | None

    #! Validation-related member variables
    use_cmake: bool = False
    use_configure: bool = False
    invoke_autoreconf: bool = False

    cmake_file_path: str | None
    configure_file_path: str | None

    resolve_file_path: str | None
    variant_tag: str

    def __init__(self, name, base_path: str=''):
        self.name = name
        self.base_path = base_path
        self.source_path = os.path.join(base_path, "sources", name)
        self.log_path = os.path.join(base_path, "log", name)
        self.bins_path = os.path.join(base_path, "bins", name)
        self.benchmark_dir_path = os.path.join(base_path, "benchmark")
        # proj: {base_path}/sources/{full_name}/{work_dir}/bb_install ({workd_dir} = {ful_name}_{comb})
        # dep: {dep_path}/install/{arch}/{name}
        self.install_prefix = None

        self.source_archive_path = None

        self.archive_group = None
        self.archive_name = None
        self.archive_md5 = None
        self.archive_root = config.ARCHIVE_ROOT
        self.source_mode = "auto"

        self.version = None
        self.full_name = None
        self.source_type = None # apt or url
        self.source_value = None # package name or url
        self.source_commit = None

        self.dep_list = None
        self.dep_path = config.EXTRA_DEP_PATH

        self.use_configure = False
        self.use_cmake = False
        self.invoke_autoreconf = False

        self.conf_options = None
        self.cflags = None
        self.ldflags = None

        self.resolve_file_path = None
        # Optional discriminator for callers that build the same compiler,
        # architecture, and optimization tuple with multiple flag sets.
        # The empty default preserves the established output names.
        self.variant_tag = ""

    def print_name(self):
        print(f"[INFO] name: {self.name}")

    def print_info(self):
        print(f"[INFO] name: {self.name}")

        print(f"[INFO] base_path: {self.base_path}")
        print(f"[INFO] source_path: {self.source_path}")
        print(f"[INFO] log_path: {self.log_path}")
        print(f"[INFO] bins_path: {self.bins_path}")
        # print(f"[INFO] benchmark_dir_path: {self.benchmark_dir_path}")
        # print(f"[INFO] install_prefix: {self.install_prefix}")

        print(f"[INFO] source_type: {self.source_type}")
        print(f"[INFO] source_value: {self.source_value}")

        print(f"[INFO] self.conf_options: {self.conf_options}")
        print(f"[INFO] self.cflags: {self.cflags}")
        print(f"[INFO] self.ldflags: {self.ldflags}")

        print(f"[INFO] self.dep_list: {self.dep_list}")

        print(f"[INFO] self.use_configure: {self.use_configure}")
        print(f"[INFO] self.use_cmake: {self.use_cmake}")
        print(f"[INFO] self.invoke_autoreconf: {self.invoke_autoreconf}")

        print(f"[INFO] self.dep_path: {self.dep_path}")
        print(f"[INFO] self.resolve_file_path: {self.resolve_file_path}")

        print()


    def set_full_name(self, name, version):
        if name != self.name or version != self.version:
            logger.error(f"Incorrect name and/or version!")
            sys.exit(1)
        self.full_name = f"{self.name}-{self.version}"

    def set_source_archive_path(self, path: str):
        self.source_archive_path = path

    def set_version(self, version: str):
        self.version = version

    def get_effective_archive_group(self) -> str | None:
        archive_group = getattr(self, "archive_group", None)
        if archive_group:
            return str(archive_group)
        if self.source_type == "apt":
            return "apt"
        return None

    def get_effective_archive_name(self) -> str | None:
        archive_name = getattr(self, "archive_name", None)
        if archive_name:
            return str(archive_name)

        if self.source_type == "url":
            extension = get_compressed_extension(self.source_value or "")
            return f"{self.name}.{extension}"

        if self.source_type in ("apt", "url_git"):
            return f"{self.name}.tar.gz"

        return None

    def get_effective_archive_version(self) -> str:
        return normalize_version(getattr(self, "version", None))

    def get_archive_local_path(self) -> str | None:
        """
        Expected layout:
        <archive_root>/<group>/<project_name>/<version>/<archive_name>
        """
        archive_root = getattr(self, "archive_root", config.ARCHIVE_ROOT)
        archive_group = self.get_effective_archive_group()
        archive_name = self.get_effective_archive_name()
        version = self.get_effective_archive_version()

        if not archive_group or not archive_name:
            return None

        return os.path.join(
            str(archive_root),
            str(archive_group),
            str(self.name),
            str(version),
            str(archive_name),
        )

    def find_existing_local_archive(self) -> str | None:
        local_archive_path = self.get_archive_local_path()
        if local_archive_path and os.path.isfile(local_archive_path):
            return local_archive_path

        archive_root = getattr(self, "archive_root", config.ARCHIVE_ROOT)
        archive_group = self.get_effective_archive_group()
        archive_name = self.get_effective_archive_name()
        version = self.get_effective_archive_version()

        if not archive_root or not archive_group or not archive_name or version != "latest":
            return None

        pattern = os.path.join(
            str(archive_root),
            str(archive_group),
            str(self.name),
            "*",
            str(archive_name),
        )
        candidates = [path for path in glob(pattern) if os.path.isfile(path)]
        if not candidates:
            return None

        candidates.sort(key=os.path.getmtime, reverse=True)
        return candidates[0]

    def create_source_archive_from_dir(
        self,
        source_root_dir: str,
        top_dir_name: str,
        source_archive_path: str,
        local_archive_path: str | None = None,
    ) -> None:
        archive_target_path = local_archive_path or source_archive_path
        archive_target_dir = os.path.dirname(archive_target_path)
        if archive_target_dir:
            os.makedirs(archive_target_dir, exist_ok=True)

        quoted_archive = shlex.quote(archive_target_path)
        quoted_root = shlex.quote(source_root_dir)
        quoted_top = shlex.quote(top_dir_name)
        execute_command(
            f"tar -czf {quoted_archive} -C {quoted_root} {quoted_top}",
            DEBUG_LEVEL=-1,
        )

        if archive_target_path != source_archive_path:
            self.copy_file(archive_target_path, source_archive_path)

        self.set_source_archive_path(source_archive_path)


    def calculate_file_md5(self, file_path: str, chunk_size: int = 1024 * 1024) -> str:
        digest = hashlib.md5()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()


    def verify_archive_md5(self, file_path: str) -> bool:
        expected_md5 = getattr(self, "archive_md5", None)
        if not expected_md5:
            return True

        actual_md5 = self.calculate_file_md5(file_path)
        if actual_md5.lower() == str(expected_md5).lower():
            return True

        logger.warning(f"Archive checksum mismatch for {self.name}: {file_path}")
        logger.warning(f"  expected md5: {expected_md5}")
        logger.warning(f"  actual md5:   {actual_md5}")
        return False


    def copy_file(self, src: str, dst: str, chunk_size: int = 1024 * 1024) -> None:
        """
        Copy file using only Python stdlib.
        """
        dst_dir = os.path.dirname(dst)
        if dst_dir:
            os.makedirs(dst_dir, exist_ok=True)
        with open(src, "rb") as fsrc:
            with open(dst, "wb") as fdst:
                while True:
                    chunk = fsrc.read(chunk_size)
                    if not chunk:
                        break
                    fdst.write(chunk)


    def download_file(self, url: str, dst: str, chunk_size: int = 1024 * 1024) -> None:
        """Download a URL; use GNU mirrors before the original GNU host."""
        dst_dir = os.path.dirname(dst)
        if dst_dir:
            os.makedirs(dst_dir, exist_ok=True)
        gnu_prefix = "https://ftp.gnu.org/gnu/"
        candidates = (["https://ftp.kaist.ac.kr/gnu/" + url[len(gnu_prefix):],
                       "https://ftpmirror.gnu.org/" + url[len(gnu_prefix):], url]
                      if url.startswith(gnu_prefix) else [url])
        temporary = dst + ".part"
        failures = []
        for candidate in candidates:
            try:
                logger.info(f"Downloading {self.name} from {candidate}")
                request = Request(candidate, headers={"User-Agent": "BinForge-source/1"})
                with urlopen(request, timeout=30) as response, open(temporary, "wb") as output:
                    for chunk in iter(lambda: response.read(chunk_size), b""):
                        output.write(chunk)
                if not self.verify_archive_md5(temporary):
                    raise ValueError("archive checksum mismatch")
                os.replace(temporary, dst)
                return
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                if os.path.exists(temporary):
                    os.remove(temporary)
                failures.append(f"{candidate}: {exc}")
        raise RuntimeError("; ".join(failures))

    def download_source_file(self) -> None:
        def get_single_subdir(path: str) -> str:
            subdirs = [d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d))]
            if len(subdirs) != 1:
                raise ValueError(f"{path}: {len(subdirs)} dirs")
            return subdirs[0]

        mkdir(self.source_path, exist_ok=True)

        #! TODO: Fix me
        if self.source_type == "apt":
            source_path = self.source_path or ""
            apt_source_name = self.source_value or self.name
            apt_source_version = normalize_version(getattr(self, "version", None), "")
            source_archive_name = self.get_effective_archive_name() or f"{self.name}.tar.gz"
            source_archive_path = os.path.join(source_path, source_archive_name)
            local_archive_path = self.find_existing_local_archive()
            source_dir_path = os.path.join(source_path, self.name)

            if os.path.exists(source_archive_path):
                if self.verify_archive_md5(source_archive_path):
                    logger.info(f"Source archive already exists: {source_archive_path} (skipping download)")
                    self.set_source_archive_path(source_archive_path)
                    return

                logger.warning(f"Removing invalid workspace archive: {source_archive_path}")
                os.remove(source_archive_path)

            if local_archive_path and os.path.isfile(local_archive_path):
                if self.verify_archive_md5(local_archive_path):
                    logger.info(f"Using local archive for {self.name}: {local_archive_path}")
                    self.copy_file(local_archive_path, source_archive_path)
                    self.set_source_archive_path(source_archive_path)
                    return

                logger.warning(f"Skipping invalid local archive: {local_archive_path}")

            if os.path.isdir(source_dir_path):
                self.create_source_archive_from_dir(
                    source_root_dir=source_path,
                    top_dir_name=self.name,
                    source_archive_path=source_archive_path,
                    local_archive_path=local_archive_path,
                )
                execute_command(f"rm -rf {shlex.quote(source_dir_path)}", DEBUG_LEVEL=-1)
                return

            with TemporaryDirectory(dir=source_path, prefix=f".{self.name}.") as temp_dir_path:
                apt_source_spec = apt_source_name
                if apt_source_version and apt_source_version != "latest":
                    apt_source_spec = f"{apt_source_name}={apt_source_version}"
                execute_command(
                    f"apt-get source {shlex.quote(apt_source_spec)}",
                    directory=temp_dir_path,
                    DEBUG_LEVEL=-1,
                )
                tmp_name = get_single_subdir(temp_dir_path)

                local_archive_path = self.get_archive_local_path()
                self.create_source_archive_from_dir(
                    source_root_dir=temp_dir_path,
                    top_dir_name=tmp_name,
                    source_archive_path=source_archive_path,
                    local_archive_path=local_archive_path,
                )


        elif self.source_type == "url":
            url = self.source_value or ""
            source_path = self.source_path or ""
            source_mode = getattr(self, "source_mode", "auto")
            if source_mode not in ("auto", "remote", "archive"):
                raise ValueError(f"Unknown source mode: {source_mode}")

            source_archive_name = self.get_effective_archive_name() or f"{self.name}.archive"

            source_archive_path = os.path.join(source_path, source_archive_name)
            local_archive_path = self.find_existing_local_archive()

            # 0) already exists in workspace
            if source_mode == "auto" and os.path.exists(source_archive_path):
                if self.verify_archive_md5(source_archive_path):
                    logger.info(f"Source archive already exists: {source_archive_path} (skipping download)")
                    self.set_source_archive_path(source_archive_path)
                    return

                logger.warning(f"Removing invalid workspace archive: {source_archive_path}")
                os.remove(source_archive_path)

            # 1) try local archive first
            if source_mode != "remote" and local_archive_path and os.path.isfile(local_archive_path):
                if self.verify_archive_md5(local_archive_path):
                    logger.info(f"Using local archive for {self.name}: {local_archive_path}")
                    self.copy_file(local_archive_path, source_archive_path)
                    if not self.verify_archive_md5(source_archive_path):
                        raise RuntimeError(f"Failed to validate copied archive for {self.name}")
                    self.set_source_archive_path(source_archive_path)
                    return

                if source_mode == "archive":
                    raise RuntimeError(f"Invalid local archive for {self.name}: {local_archive_path}")
                logger.warning(f"Skipping invalid local archive: {local_archive_path}")

            if source_mode == "archive":
                raise FileNotFoundError(f"Local archive missing for {self.name}: {self.get_archive_local_path()}")

            # 2) fallback to remote download
            try:
                download_target_path = source_archive_path
                archive_cache_path = self.get_archive_local_path()
                if source_mode == "auto" and archive_cache_path:
                    logger.info(f"Downloading archive for {self.name} into cache: {archive_cache_path}")
                    download_target_path = archive_cache_path
                else:
                    logger.info(f"Downloading from URL for {self.name}: {url}")

                self.download_file(url, download_target_path)
            except Exception as e:
                logger.error(f"Download failed: {url}")
                logger.error(f"Error: {e}")
                raise RuntimeError(f"Failed to fetch source archive for {self.name}")

            if not self.verify_archive_md5(download_target_path):
                os.remove(download_target_path)
                raise RuntimeError(f"Failed to validate downloaded archive for {self.name}")

            if download_target_path != source_archive_path:
                self.copy_file(download_target_path, source_archive_path)
                if not self.verify_archive_md5(source_archive_path):
                    os.remove(source_archive_path)
                    raise RuntimeError(f"Failed to validate copied archive for {self.name}")

            self.set_source_archive_path(source_archive_path)

        #! For github/gitlab
        elif self.source_type == "url_git":
            source_path = self.source_path or ""
            name = self.name
            url = self.source_value or ""
            commit = getattr(self, "source_commit", None)
            clone_url = url if url.endswith(".git") else f"{url}.git"

            source_archive_name = f"{name}.tar.gz"
            source_archive_path = os.path.join(source_path, source_archive_name)
            assert source_path is not None

            git_dir = os.path.join(source_path, name)
            if not os.path.exists(git_dir):
                execute_command(
                    f"git clone {shlex.quote(clone_url)} {shlex.quote(git_dir)} 2>&1",
                    DEBUG_LEVEL=-1,
                )
            else:
                logger.info(f"Git directory already exists: {git_dir} (skipping clone)")

            if commit:
                commit_quoted = shlex.quote(str(commit))
                execute_command(f"git fetch --all --tags 2>&1", directory=git_dir, DEBUG_LEVEL=-1)
                execute_command(
                    f"git -c advice.detachedHead=false checkout {commit_quoted} 2>&1",
                    directory=git_dir,
                    DEBUG_LEVEL=-1,
                )

            execute_command(
                f"tar --exclude={shlex.quote(name + '/.git')} -czf {shlex.quote(source_archive_name)} {shlex.quote(name)}",
                directory=source_path,
                DEBUG_LEVEL=-1,
            )
            self.set_source_archive_path(source_archive_path)

        else:
            logger.error(f"Project {self.name} has incorrect source_type.")
            exit(1)


    def extract_source_file(self):
        if self.source_type == "url_git":
            return

        assert self.source_archive_path is not None, "self.source_archive_path is None"
        if getattr(self, "source_mode", "auto") in ("remote", "archive"):
            destination = os.path.join(self.source_path, self.name)
            if os.path.islink(destination):
                raise ValueError(f"Refusing to replace symlinked source tree: {destination}")
            if os.path.isdir(destination):
                shutil.rmtree(destination)
        _ = unzip_tar(source_archive_path=self.source_archive_path, source_code_dir_name=self.name)


# EOF
