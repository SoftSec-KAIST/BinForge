"""Remove intermediate compiler build directories after installation."""

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from binforge import config
from binforge.logging_helper import logger


BUILD_DIRS = (
    "bootstrap/build",
    "clang/logs",
    "clang/llvm-project",
    "crosstool-ng",
    "ctng_tarballs",
    "gcc/build",
)


def cleanup_paths(tool_path: Path) -> list[Path]:
    return [tool_path / name for name in BUILD_DIRS]


def check_cleanup_paths(tool_path: Path) -> list[Path]:
    """Validate every target before allowing any deletion."""
    if tool_path.exists() and not tool_path.is_dir():
        raise ValueError(f"Expected a tool directory: {tool_path}")
    if tool_path.is_symlink() and not tool_path.exists():
        raise ValueError(f"Broken tool directory symlink: {tool_path}")

    paths = cleanup_paths(tool_path)
    for path in paths:
        current = tool_path
        for component in path.relative_to(tool_path).parts:
            current = current / component
            if current.is_symlink():
                raise ValueError(f"Refusing to follow symlink inside tools: {current}")
            if current.exists() and not current.is_dir():
                raise ValueError(f"Expected a directory: {current}")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove intermediate compiler build directories.")
    parser.add_argument("--status", action="store_true", help="List paths that would be removed without changing files")
    args = parser.parse_args()

    tool_path = Path(config.TOOL_PATH)
    try:
        paths = check_cleanup_paths(tool_path)
    except ValueError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc

    if tool_path.is_symlink():
        print(f"Tools directory: {tool_path} -> {tool_path.resolve()}", flush=True)
    for path in paths:
        if not path.exists():
            logger.info(f"Skip missing path: {path}")
            continue
        if args.status:
            print(f"Would remove: {path}")
        else:
            shutil.rmtree(path)
            logger.info(f"Removed: {path}")


if __name__ == "__main__":
    main()
