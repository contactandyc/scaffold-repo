# src/scaffold_repo/sync/cli_plugin.py
from __future__ import annotations

import argparse
import posixpath
from pathlib import Path

from ..core.config import ConfigReader
from ..repo_sync import verify_repo
from ..cli.ui import print_text_result
from ..utils.text import slug

def add_sync_arguments(parser: argparse.ArgumentParser) -> None:
    """Appends all template synchronization and diffing arguments."""
    grp_sc = parser.add_argument_group("Scaffolding Options")
    grp_sc.add_argument("--update", action="store_true", help="Explicitly apply template updates to the repositories")
    grp_sc.add_argument("--diff", action="store_true", help="Print unpaginated Git diffs")
    grp_sc.add_argument("-y", "--assume-yes", action="store_true", help="Apply template updates without prompting")
    grp_sc.add_argument("--show-diffs", action="store_true", help="Print inline diffs before applying updates")


def run_sync(
        args: argparse.Namespace,
        root: Path,
        workspace_dir: Path,
        targets: list[tuple],
        is_create_run: bool = False
) -> int:
    """Executes the template generation and verification phase."""
    exit_code = 0
    results = []

    skip_templates = True
    if getattr(args, 'update', False) or getattr(args, 'assume_yes', False) or getattr(args, 'show_diffs', False):
        skip_templates = False

    if targets:
        for name, project_slug, raw_token, item in targets:
            dest = workspace_dir / slug(posixpath.basename(raw_token))

            if skip_templates or not dest.exists():
                if not dest.exists():
                    print(f"  - Skipping template verification for {name} (Directory does not exist).")
                else:
                    print(f"  - Bypassing template verification for {name} (Read-only / Git operation).")
                continue

            print(f"\n\033[95m=== Scaffolding {name} ({project_slug}) into {dest} ===\033[0m")
            code, res = verify_repo(
                dest,
                fix_licenses=True,
                no_prompt=getattr(args, 'assume_yes', False),
                project_name=name,
                base_templates_dir=None,
                assume_yes=True if is_create_run else getattr(args, 'assume_yes', False),
                show_diffs=getattr(args, 'show_diffs', False),
                is_init=is_create_run
            )
            exit_code = max(exit_code, code)
            results.append(res)

    elif not getattr(args, 'projects', []):
        print(f"\n\033[95m=== Scaffolding in-place ({root}) ===\033[0m")
        if not skip_templates:
            code, res = verify_repo(
                root,
                fix_licenses=True,
                no_prompt=getattr(args, 'assume_yes', False),
                project_name=None,
                base_templates_dir=None,
                assume_yes=True if is_create_run else getattr(args, 'assume_yes', False),
                show_diffs=getattr(args, 'show_diffs', False),
                is_init=is_create_run
            )
            exit_code = max(exit_code, code)
            results.append(res)

    # Print summaries
    if not (getattr(args, 'diff', False) or getattr(args, 'start_feature', None) == "" or skip_templates):
        for res in results:
            print_text_result(res)

    return exit_code
