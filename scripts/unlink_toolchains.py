"""Remove only BinForge's GCC update-alternatives registrations."""

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.link_toolchains import (
    arch_prefix_list,
    managed_internal_symlinks,
    remove_internal_links,
    run_alternative_command,
    show_link_status,
)


def unlink_status(remove_internal: bool = False) -> bool:
    alternatives_removed = show_link_status("unlinked")
    if not remove_internal:
        return alternatives_removed
    remaining = managed_internal_symlinks()
    print(f"Managed internal links remaining: {len(remaining)}")
    return alternatives_removed and not remaining


def unlink_toolchains(remove_internal: bool = False) -> None:
    for prefix in arch_prefix_list:
        run_alternative_command(prefix, "remove")
    if remove_internal:
        removed = remove_internal_links()
        print(f"Removed {removed} managed internal links.")
    if not unlink_status(remove_internal):
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove BinForge GCC system alternatives.")
    parser.add_argument("--status", action="store_true", help="Check that BinForge system links have been removed")
    parser.add_argument("--all", action="store_true", help="Also remove matching internal sysroot and libmvec links")
    args = parser.parse_args()
    if args.status:
        raise SystemExit(0 if unlink_status(args.all) else 1)
    unlink_toolchains(args.all)


if __name__ == "__main__":
    main()
