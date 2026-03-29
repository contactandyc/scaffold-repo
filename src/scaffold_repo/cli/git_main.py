# src/scaffold_repo/cli/git_main.py
from __future__ import annotations

import argparse
import sys
import posixpath
from pathlib import Path

from .resolver import load_workspace_and_targets
from ..git.cli_plugin import (
    add_git_arguments,
    execute_git_transport_phases,
    execute_git_branching_phases,
    execute_git_authoring_phases
)
from ..git.orchestrator import GitFleetManager
from ..utils.text import slug


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fleet-git",
        description="The Git Orchestrator: Manage branching and publishing across a polyglot repository fleet."
    )

    ap.add_argument("projects", nargs="*", help="One or more projects/namespaces to target")

    grp_ws = ap.add_argument_group("Workspace Options")
    grp_ws.add_argument("-C", "--cwd", type=Path, default=Path("."), help="Run as if started in <PATH>")
    grp_ws.add_argument("-y", "--assume-yes", action="store_true", help="Bypass prompts and confirmations")
    grp_ws.add_argument("--diff", nargs="?", const="AUTO", metavar="BRANCH", help="Print Git diffs against the parent branch (or a specified branch)")

    # --- PLUGIN HOOK (Handles Transport, Branching & Authoring) ---
    add_git_arguments(ap)

    args = ap.parse_args(argv)
    root = args.cwd.resolve()

    try:
        workspace_dir, reader, targets = load_workspace_and_targets(root, args.projects)
    except Exception as e:
        print(f"\n❌ Failed to load workspace configuration: {e}", file=sys.stderr)
        return 1

    if args.projects and not targets:
        print(f"\n❌ Error: Could not resolve any valid projects from: {args.projects}")
        return 1

    if not targets and not getattr(args, 'diff', False):
        print("🎯 No targets specified. Executing against the root repository.")
        targets = [("Root Repo", "root", None, {})]

    # 0. Status Phase (Early Exit)
    if getattr(args, 'diff', False):
        print("\n\033[1m=== Fleet Git Diffs & Status ===\033[0m")
        orchestrator = GitFleetManager(workspace_dir, reader.effective_config)
        diff_target = args.diff if isinstance(args.diff, str) else "AUTO"
        if not targets: targets = [("Root Repo", "root", None, {})]
        for name, project_slug, raw_token, item in targets:
            dest = root if not raw_token else workspace_dir / slug(posixpath.basename(raw_token))
            orchestrator.status_report(dest, name, raw_token, diff_target)
        return 0

    # 1. Transport Phase
    transport_exit = execute_git_transport_phases(args, root, workspace_dir, reader, targets)

    # 2. Branching Phase
    branching_exit = execute_git_branching_phases(args, root, workspace_dir, reader, targets)

    # 3. Authoring Phase
    authoring_exit = execute_git_authoring_phases(args, root, workspace_dir, reader, targets)

    return max(transport_exit, branching_exit, authoring_exit)

if __name__ == "__main__":
    sys.exit(main())
