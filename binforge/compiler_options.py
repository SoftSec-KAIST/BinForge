"""Interpret the three configured compiler options and their combinations."""

from __future__ import annotations

from itertools import product
from typing import Any


OPTION_KEYS = ("pie", "no_inline", "stack_protector")


def _as_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off", ""):
            return False
    raise ValueError(f"build.extra_compiler_options.{key} must be true or false")


def configured_compiler_options(build: dict[str, Any]) -> tuple[dict[str, bool], bool]:
    """Return the selected options and whether their subsets should be built."""
    if "extra_compiler_option_modes" in build:
        raise ValueError(
            "build.extra_compiler_option_modes is no longer supported; use "
            "extra_compiler_options with combine_extra_compiler_options"
        )
    raw = build.get("extra_compiler_options") or {}
    if not isinstance(raw, dict):
        raise ValueError("build.extra_compiler_options must be a mapping")
    unknown = set(raw) - set(OPTION_KEYS)
    if unknown:
        raise ValueError(
            "Unknown build.extra_compiler_options keys: "
            + ", ".join(sorted(map(str, unknown)))
        )
    options = {key: _as_bool(raw.get(key, False), key) for key in OPTION_KEYS}
    combine = build.get("combine_extra_compiler_options", False)
    if isinstance(combine, str):
        normalized = combine.strip().lower()
        if normalized in ("true", "false"):
            combine = normalized == "true"
    if not isinstance(combine, bool):
        raise ValueError("build.combine_extra_compiler_options must be true or false")
    return options, combine


def option_set_slug(options: dict[str, bool]) -> str:
    """Provide a readable, filesystem-safe name for an option combination."""
    return "_".join(
        f"{key.replace('_', '-')}-{'on' if options[key] else 'off'}"
        for key in OPTION_KEYS
    )


def option_set_label(name: str) -> str:
    """Format a suite directory name for the compact monitor flag column."""
    if name in ("off", "on"):
        # Existing completed runs used these two directory names.
        return "F/F/F" if name == "off" else "T/T/T"
    parts = name.split("_")
    if len(parts) != len(OPTION_KEYS):
        return name
    values = []
    for key, part in zip(OPTION_KEYS, parts):
        prefix = key.replace("_", "-") + "-"
        if not part.startswith(prefix) or part[len(prefix):] not in ("off", "on"):
            return name
        values.append("T" if part[len(prefix):] == "on" else "F")
    return "/".join(values)


def compiler_option_sets(build: dict[str, Any]) -> list[tuple[str, dict[str, bool]]]:
    """Expand enabled options into every on/off combination, including all-off."""
    selected, combine = configured_compiler_options(build)
    if not combine:
        return [("", selected)]
    enabled = [key for key in OPTION_KEYS if selected[key]]
    result = []
    for values in product((False, True), repeat=len(enabled)):
        options = dict.fromkeys(OPTION_KEYS, False)
        options.update(zip(enabled, values))
        result.append((option_set_slug(options), options))
    return result
