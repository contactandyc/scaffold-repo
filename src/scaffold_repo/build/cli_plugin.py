# src/scaffold_repo/build/cli_plugin.py
from __future__ import annotations

import argparse
import posixpath
from pathlib import Path

from ..build_libs import build_all_libs, execute_build
from ..utils.text import slug

def add_build_arguments(parser: argparse.ArgumentParser) -> None:
    """Appends all compilation and dependency lifecycle arguments."""
    grp_dp = parser.add_argument_group("Dependency Lifecycle")
    grp_dp.add_argument("--build-deps", action="store_true", help="Fetch, compile, and install dependencies")
    grp_dp.add_argument("--clean-deps", action="store_true", help="Wipe build caches for dependencies")

    grp_tl = parser.add_argument_group("Target Lifecycle")
    grp_tl.add_argument("--clean", action="store_true", help="Run './build.sh clean' on the target")
    grp_tl.add_argument("--build", action="store_true", help="Run './build.sh build'")
    grp_tl.add_argument("--install", action="store_true", help="Run './build.sh install'")


def run_build(
        args: argparse.Namespace,
        root: Path,
        workspace_dir: Path,
        targets: list[tuple]
) -> int:
    """Executes dependency and first-party build orchestration."""

    # Phase 4: Build Dependencies
    if getattr(args, 'build_deps', False) or getattr(args, 'clean_deps', False):
        target_dirs = [workspace_dir / slug(posixpath.basename(t[2])) for t in targets] if targets else [root]
        for t_dir in target_dirs:
            if t_dir.exists():
                build_all_libs(
                    repo=t_dir, workspace_dir=workspace_dir, project_tokens=[],
                    base_templates_dir=None,
                    do_clone=False, # Handled natively by the Transport phase in the master CLI
                    do_build=getattr(args, 'build_deps', False),
                    do_install=getattr(args, 'build_deps', False),
                    do_clean=getattr(args, 'clean_deps', False)
                )

    # Phase 5: First-Party Build Orchestration
    if targets and any([getattr(args, 'build', False), getattr(args, 'install', False), getattr(args, 'clean', False)]):
        print("\n\033[1m=== Phase 5: First-Party Build Orchestration ===\033[0m")

        for name, project_slug, raw_token, item in targets:
            dest = workspace_dir / slug(posixpath.basename(raw_token))
            if not dest.exists():
                continue

            print(f"\n🚀 Orchestrating execution for {name}...")
            execute_build(
                project_slug, dest, item, workspace_dir,
                do_build=getattr(args, 'build', False),
                do_install=getattr(args, 'install', False),
                do_clean=getattr(args, 'clean', False)
            )

    return 0
