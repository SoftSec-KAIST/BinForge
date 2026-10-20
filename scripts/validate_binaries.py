#!/usr/bin/env python3
"""Validate generated ELF files and target ISAs with B2R2."""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VALIDATOR = (
    PROJECT_ROOT / "tools" / "b2r2" / "validator" / "BinForge.B2R2Validator.dll"
)
ARCHITECTURES = (
    "x86_32",
    "x86_64",
    "arm_32",
    "arm_64",
    "mips_32",
    "mips_64",
    "mipseb_32",
    "mipseb_64",
    "ppceb_32",
    "ppceb_64",
    "ppc_64",
)
ARCH_PATTERN = re.compile(r"_(" + "|".join(map(re.escape, ARCHITECTURES)) + r")_")


def infer_architecture(path: Path) -> str | None:
    match = ARCH_PATTERN.search(path.name)
    return match.group(1) if match else None


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
    raise RuntimeError(".NET SDK 10 or later is required; no compatible dotnet was found")


def start_validators(dotnet: str, validator: Path) -> dict[str, subprocess.Popen[str]]:
    return {
        arch: subprocess.Popen(
            [dotnet, str(validator), arch],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        for arch in ARCHITECTURES
    }


def close_validator(process: subprocess.Popen[str]) -> tuple[int, str]:
    assert process.stdin is not None
    process.stdin.close()
    process.stdin = None
    stdout, _ = process.communicate()
    return process.returncode, stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bins", type=Path, help="A canonical bins/ directory to validate")
    parser.add_argument(
        "--validator", type=Path, default=DEFAULT_VALIDATOR,
        help=f"B2R2 validator executable (default: {DEFAULT_VALIDATOR})",
    )
    args = parser.parse_args()

    bins = args.bins.expanduser().resolve()
    validator = args.validator.expanduser().resolve()
    if not bins.is_dir():
        parser.error(f"bins directory does not exist: {bins}")
    if not validator.is_file():
        parser.error(
            f"B2R2 validator is not installed: {validator}; "
            "activate bfenv, then run python scripts/setup_b2r2.py"
        )

    try:
        dotnet = find_dotnet()
    except RuntimeError as error:
        parser.error(str(error))
    processes = start_validators(dotnet, validator)
    counts = {arch: 0 for arch in ARCHITECTURES}
    unclassified: list[str] = []
    try:
        for path in bins.rglob("*"):
            if not path.is_file():
                continue
            arch = infer_architecture(path)
            if arch is None:
                unclassified.append(str(path))
                continue
            stream = processes[arch].stdin
            assert stream is not None
            stream.write(f"{path}\n")
            counts[arch] += 1
    except BaseException:
        for process in processes.values():
            process.kill()
        raise

    failures = []
    summaries = {}
    for arch, process in processes.items():
        returncode, stdout = close_validator(process)
        try:
            summary = json.loads(stdout.strip())
        except json.JSONDecodeError:
            summary = {"rawOutput": stdout.strip()}
        summaries[arch] = summary
        if returncode != 0 or int(summary.get("checkedFiles", -1)) != counts[arch]:
            failures.append(arch)
    report = {
        "bins": str(bins),
        "validator": "B2R2 0.10.1 FormatDetector",
        "files": sum(counts.values()),
        "byArchitecture": counts,
        "unclassified": len(unclassified),
        "failedArchitectures": failures,
        "results": summaries,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    if unclassified:
        print("Files without a BinForge architecture token:", file=sys.stderr)
        for path in unclassified[:20]:
            print(path, file=sys.stderr)
        if len(unclassified) > 20:
            print(f"... and {len(unclassified) - 20} more", file=sys.stderr)
    return 1 if failures or unclassified or report["files"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
