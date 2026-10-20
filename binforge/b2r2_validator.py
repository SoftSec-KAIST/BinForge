"""B2R2-backed validation for binaries collected by BinForge."""

import json
from functools import lru_cache
from pathlib import Path
import shutil
import subprocess

from binforge import config
from binforge.logging_helper import logger


@lru_cache(maxsize=1)
def find_dotnet() -> str:
    candidates = [Path.home() / ".dotnet" / "dotnet"]
    if command := shutil.which("dotnet"):
        candidates.append(Path(command))
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file():
            continue
        result = subprocess.run(
            [str(candidate), "--version"], capture_output=True, text=True, check=False
        )
        try:
            compatible = int(result.stdout.split(".", 1)[0]) >= 10
        except ValueError:
            compatible = False
        if result.returncode == 0 and compatible:
            return str(candidate)
    raise RuntimeError(".NET SDK 10 or later is required for B2R2 validation")


def validate_binary_paths(
    paths: list[str],
    arch: str,
    *,
    dotnet: str | None = None,
    validator: str | Path | None = None,
) -> bool:
    """Return whether B2R2 identifies every path as the requested ELF ISA."""
    if not paths:
        return True

    dotnet = dotnet or find_dotnet()
    validator_path = Path(validator or config.B2R2_VALIDATOR_PATH)
    if not validator_path.is_file():
        raise RuntimeError(
            f"B2R2 validator is not installed: {validator_path}; "
            "activate bfenv, then run python scripts/setup_b2r2.py"
        )

    result = subprocess.run(
        [dotnet, str(validator_path), arch],
        input="".join(f"{path}\n" for path in paths),
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        summary = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        logger.error("B2R2 validator returned invalid output: %s", result.stdout.strip())
        if result.stderr:
            logger.error("B2R2 validator error: %s", result.stderr.strip())
        return False

    checked = summary.get("checkedFiles")
    invalid = summary.get("invalidFiles")
    valid = result.returncode == 0 and checked == len(paths) and invalid == 0
    if not valid:
        logger.error(
            "B2R2 validation failed for %s: checked=%s expected=%d invalid=%s exit=%d",
            arch, checked, len(paths), invalid, result.returncode,
        )
        if result.stderr:
            logger.error("B2R2 mismatches:\n%s", result.stderr.strip())
    return valid
