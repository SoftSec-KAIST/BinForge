import os

from concurrent.futures import ThreadPoolExecutor

from binforge import config
from binforge.models import Project


def download_projects(projects: list[Project], targets: list[str]=[]) -> bool:

    if targets:
        _projects, projects = projects, []
        for proj in _projects:
            if proj.name in targets:
                projects.append(proj)

    NUM_JOBS = int(os.environ.get("BINFORGE_SOURCE_DOWNLOAD_JOBS", "8"))

    with ThreadPoolExecutor(max_workers=NUM_JOBS) as executor:
        futures = [executor.submit(project.download_source_file) for project in projects]
        for future in futures:
            future.result()

    return True


def extract_projects(projects: list[Project]) -> None:
    NUM_JOBS = int(config.NUM_JOBS)
    with ThreadPoolExecutor(max_workers=NUM_JOBS) as executor:
        futures = [executor.submit(project.extract_source_file) for project in projects]
        for future in futures:
            future.result()


def download_and_extract_projects(projects: list[Project], targets: list[str]=[]) -> None:
    selected = [project for project in projects if not targets or project.name in targets]
    download_projects(selected)
    extract_projects(selected)


# EOF
