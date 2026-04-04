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
    grp_sc.add_argument("--diff", nargs="?", const="AUTO", metavar="BRANCH", help="Print Git diffs against the parent branch (or a specified branch)")
    grp_sc.add_argument("-y", "--assume-yes", action="store_true", help="Apply template updates without prompting")
    grp_sc.add_argument("--show-diffs", action="store_true", help="Print inline diffs before applying updates")
    grp_sc.add_argument("--dry-run", action="store_true", help="Print what would change without modifying files or branching")

def run_sync(
        args: argparse.Namespace,
        root: Path,
        workspace_dir: Path,
        targets: list[tuple],
        is_create_run: bool = False,
        dry_run: bool = False,
        quiet: bool = False
) -> tuple[int, bool]:
    """Executes the template generation and verification phase. Returns (exit_code, has_drift)"""
    exit_code = 0
    results = []
    global_has_drift = False

    skip_templates = True
    if getattr(args, 'update', False) or getattr(args, 'assume_yes', False) or getattr(args, 'show_diffs', False) or dry_run:
        skip_templates = False

    if targets:
        for name, project_slug, raw_token, item in targets:
            # ── SAFE PATH RESOLUTION ──
            dest = root if not raw_token else workspace_dir / slug(posixpath.basename(raw_token))

            if skip_templates or not dest.exists():
                if not dest.exists() and not quiet:
                    print(f"  - Skipping template verification for {name} (Directory does not exist).")
                elif not quiet:
                    print(f"  - Bypassing template verification for {name} (Read-only / Git operation).")
                continue

            if not quiet:
                print(f"\n\033[95m=== Scaffolding {name} ({project_slug}) into {dest} ===\033[0m")
                if dry_run: print("  [Dry Run Mode] Calculating platform drift...")

            code, res, has_drift = verify_repo(
                dest,
                fix_licenses=True,
                no_prompt=getattr(args, 'assume_yes', False) or dry_run,
                project_name=name,
                base_templates_dir=None,
                assume_yes=True if (is_create_run or dry_run) else getattr(args, 'assume_yes', False),
                show_diffs=getattr(args, 'show_diffs', False),
                is_init=is_create_run,
                dry_run=dry_run,
                quiet=quiet
            )
            if has_drift: global_has_drift = True
            exit_code = max(exit_code, code)
            results.append(res)

    elif not getattr(args, 'projects', []):
        if not quiet:
            print(f"\n\033[95m=== Scaffolding in-place ({root}) ===\033[0m")
            if dry_run: print("  [Dry Run Mode] Calculating platform drift...")

        if not skip_templates:
            code, res, has_drift = verify_repo(
                root,
                fix_licenses=True,
                no_prompt=getattr(args, 'assume_yes', False) or dry_run,
                project_name=None,
                base_templates_dir=None,
                assume_yes=True if (is_create_run or dry_run) else getattr(args, 'assume_yes', False),
                show_diffs=getattr(args, 'show_diffs', False),
                is_init=is_create_run,
                dry_run=dry_run,
                quiet=quiet
            )
            if has_drift: global_has_drift = True
            exit_code = max(exit_code, code)
            results.append(res)

    # Print summaries
    if not quiet and not (getattr(args, 'diff', False) or getattr(args, 'start_feature', None) == "" or skip_templates):
        for res in results:
            print_text_result(res)

    return exit_code, global_has_drift

