import os

from binforge.utils import get_version_from_url, load_yaml
from binforge.logging_helper import logger
from binforge import config
from binforge.models import Project, cleanup_projects

import traceback

import re
import argparse
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
from bs4.element import Tag


def get_url_by_name(name, projects):
    for entry in projects:
        if entry["name"] == name:
            return entry.get("url")
    return None


def parse_proejct_list_file(file_path: str, args: argparse.Namespace) -> list[Project]:
    target_project_list = load_yaml(file_path)

    dep_map = load_yaml("config/dep_map.yaml")["projects"]
    name_to_deps = {item['name']: item['deps'] for item in dep_map}

    projects = []
    for entry in target_project_list["projects"]:
        name = entry["name"]

        #! URL
        url = entry.get("url")
        if url is not None:
            url = url.strip()
        if not url:
            url = None

        #! Dependenceis
        deps1 = entry.get("deps") or []
        deps2 = name_to_deps.get(name) or []

        # print(f"[DEBUG] deps1: {deps1}")
        # print(f"[DEBUG] deps2: {deps2}")

        deps = sorted(set(deps1) | set(deps2))

        if not isinstance(deps, list):
            raise ValueError(f"Invalid deps for project '{name}': {deps}")

        #! Version
        version = entry.get("version")
        if version is not None:
            version = str(version).strip()
        if not version:
            version = None

        #! Use git clone
        use_git_clone = entry.get("use_git_clone", False) or False

        proj = Project(name=name, base_path=args.base_path)
        proj.download_url = url
        proj.dep_list = deps
        proj.version = version
        proj.use_git_clone = use_git_clone

        projects.append(proj)

    for p in projects:
        logger.info(f"Project: {p.name}")
        logger.info(f"  URL: {p.download_url}")
        logger.info(f"  Deps: {p.dep_list}")

    return projects


def parse_project_list(args: argparse.Namespace, target_projects_from_file: bool) -> list[Project] | None:
    if not target_projects_from_file:
        project_names = [item.strip() for item in args.project_list.split(',') if item.strip()]
        projects = [Project(target_project_name, args.base_path) for target_project_name in project_names]
    else:
        whitelist_file_path = args.project_list_file
        if not os.path.exists(whitelist_file_path):
            logger.error(f"Whitelist file '{whitelist_file_path}' does not exist.")
            return None

        projects = parse_proejct_list_file(whitelist_file_path, args)

    return projects


def set_project_info(project: Project, url: str) -> None:
    version = project.version
    if version is None:
        version = get_version_from_url(project.name, url)

    project.set_valid(True)
    project.set_version(version)
    project.set_download_url(url)
    project.set_full_name(project.name, project.version)


def get_gnu_url(project):
    base_gnu_url = "http://ftp.gnu.org/gnu/"
    #! For GNU projects
    project.set_valid(False)
    gnu_project_url = base_gnu_url + project.get_name()
    page = requests.get(gnu_project_url)
    available_link_tags = BeautifulSoup(page.text, "lxml").find_all("a")
    link_hrefs = [link["href"] for link in available_link_tags if isinstance(link, Tag)]

    #! Retrieve the names of all versions of the tar files
    source_archive_names = [
        href for href in link_hrefs
        if isinstance(href, str) and re.search(
            r"{0}[^-]*-[0-9.]+\.(tar.gz|tar.xz|tar.bz2)$".format(re.escape(project.name)),
            href,
            re.IGNORECASE
        )
    ]

    def extract_version(source_archive_name: str) -> list[int]:
        match = re.search(r"-([0-9\.]+)\.tar", source_archive_name, re.IGNORECASE)
        assert match is not None, f"Invalid tar filename: {source_archive_name}"
        return list(map(int, match.group(1).split(".")))

    source_archive_names.sort(key=extract_version)

    if not source_archive_names:
        os.makedirs(project.base_path, exist_ok=True)
        logger.info(f"Downlaod failed: {project.name}")
        exit()

    ##! select latest version of project
    source_archive_name = source_archive_names[-1]

    match = re.search(r"-([0-9\.]+)\.tar", source_archive_name)
    assert match is not None, f"Invalid tar file name format: {source_archive_name}"
    version = match.group(1)

    download_url = gnu_project_url + '/' + source_archive_name

    return download_url, version, source_archive_name

def set_gnu_project_info(project: Project):
    download_url, version, _ = get_gnu_url(project)

    project.set_valid(True)
    project.set_version(version)
    project.set_full_name(project.name, project.version)
    project.set_download_url(download_url)

    return project


def download_projects(projects: list[Project]) -> bool:
    #! GNU
    file_path = "config/known_gnu_projects.txt"
    with open(file_path, "r", encoding="utf-8") as f:
        KNOWN_GNU_PROJECTS = set([line.rstrip("\n") for line in f])

    #! Supported
    supported_project_data = load_yaml("config/supported_projects.yaml")
    supported_project_list = [entry["name"] for entry in supported_project_data["projects"]]

    for project in projects:
        name = project.name
        url = project.download_url

        # print(f"[DEBUG] Name: {name}")
        #! 1. If URL is given, use it.
        if url:
            set_project_info(project, url)
        #! 2. IF URL is not given, find name from supported projcets.
        elif not url and name in supported_project_list:
            # print(f"[DEBUG] supported project: {name}")
            url = get_url_by_name(name, supported_project_data["projects"]) or ""
            set_project_info(project, url)
        #! 3. If URL is not given, find name from known gnu projcets.
        elif not url and name in KNOWN_GNU_PROJECTS:
            # print(f"[DEBUG] GNU project: {name}")
            print(f"[DEBUG] Use latest version from ftp.gnu.org")
            set_gnu_project_info(project)
        else:
            logger.error(f"URL must be given: {name}")

    NUM_JOBS = int(config.NUM_JOBS)
    with ThreadPoolExecutor(max_workers=NUM_JOBS) as executor:
        futures = [executor.submit(project.download_source_file) for project in projects]

        for future in futures:
            try:
                future.result()
            except AssertionError as e:
                logger.error("[-] Assertion failed!")
                logger.error(f"  → Error message: {e}")
                logger.error("  → Traceback:")
                traceback.print_exc()
                exit(0)

    cleanup_projects(projects)

    return True


def extract_projects(projects: list[Project]) -> None:
    # Run extraction in parallel
    NUM_JOBS = int(config.NUM_JOBS)
    with ThreadPoolExecutor(max_workers=NUM_JOBS) as executor:
        executor.map(lambda project: project.extract_source_file(), projects)

    cleanup_projects(projects)


def download_and_extract_projects(projects: list[Project]) -> None:
    download_projects(projects)
    extract_projects(projects)


# EOF
