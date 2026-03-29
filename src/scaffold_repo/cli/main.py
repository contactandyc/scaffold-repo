# src/scaffold_repo/cli/main.py
from __future__ import annotations

import argparse
import sys
import subprocess
import posixpath
from pathlib import Path

# --- Resolvers & Workspace Tools ---
from .workspace import find_scaffoldrc
from .resolver import load_workspace_and_targets
from ..core.config import ConfigReader

# --- Domain Plugins ---
from ..init.cli_plugin import add_init_arguments, run_init
from ..create.cli_plugin import add_create_arguments, run_create
from ..sync.cli_plugin import add_sync_arguments, run_sync
from ..build.cli_plugin import add_build_arguments, run_build
from ..git.cli_plugin import (
    add_git_arguments,
    execute_git_transport_phases,
    execute_git_branching_phases,
    execute_git_authoring_phases
)
from ..utils.text import slug

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scaffold-repo",
        description="The Declarative Fleet Manager: Scaffold repos, enforce OSS, orchestrate builds, and sync to Git."
    )

    # 1. Global / Core Arguments
    ap.add_argument("projects", nargs="*", help="One or more projects/namespaces to scaffold or build")
    grp_ws = ap.add_argument_group("Workspace Options")
    grp_ws.add_argument("-C", "--cwd", type=Path, default=Path("."), help="Run as if started in <PATH>")

    # 2. Wire up the Domain Plugins!
    add_init_arguments(ap)
    add_create_arguments(ap)
    add_sync_arguments(ap)
    add_build_arguments(ap)
    add_git_arguments(ap)

    args = ap.parse_args(argv)
    root = args.cwd.resolve()

    # Route 1: Workspace Initialization
    if args.init:
        return run_init()

    # Shared Setup: Load Workspace Configuration
    try:
        workspace_dir, reader, targets = load_workspace_and_targets(root, args.projects)
    except Exception as e:
        print(f"\n❌ Failed to load workspace configuration: {e}", file=sys.stderr)
        return 1

    is_create_run = False

    # Route 2: Repository Creation
    if args.create:
        rc = find_scaffoldrc(root)
        code = run_create(args.create, workspace_dir, reader, rc)
        if code != 0: return code

        # If creation succeeds, override targets to immediately scaffold the new project
        # (Removed the rogue local import here!)
        targets = [(args.create, slug(args.create), args.create, {})]
        args.update = True
        is_create_run = True

    # Validate Targets
    if args.projects and not targets and not args.create:
        print(f"\n❌ Error: Could not resolve any valid projects from: {args.projects}")
        print(f"   Check your spelling or ensure your templates/resources/aliases.yaml is defined correctly.")
        return 1

    # ── THE NEW DIFF SHORT-CIRCUIT ──
    if getattr(args, 'diff', False):
        print("\n\033[1m=== Fleet Git Diffs ===\033[0m")
        from ..git.orchestrator import GitFleetManager
        orchestrator = GitFleetManager(workspace_dir, reader.effective_config)
        diff_target = args.diff if isinstance(args.diff, str) else "AUTO"

        diff_targets = targets if targets else [("Workspace Root", "root", None, {})]
        for name, project_slug, raw_token, item in diff_targets:
            dest = root if not raw_token else workspace_dir / slug(posixpath.basename(raw_token))
            orchestrator.status_report(dest, name, raw_token, diff_target)
        return 0

    exit_code = 0

    try:
        # Determine if we should bypass Jinja templating
        skip_templates = not (args.update or getattr(args, 'assume_yes', False) or getattr(args, 'show_diffs', False))

        # Implicitly force cloning (and pulling, if updating templates)
        args.clone = True
        if not skip_templates:
            args.pull = True

        # Phase 1: Git Transport
        transport_exit = execute_git_transport_phases(args, root, workspace_dir, reader, targets)
        exit_code = max(exit_code, transport_exit)

        # Phase 2: Git Branching
        if getattr(args, 'update', False) and getattr(args, 'start_feature', None) is None:
            # Auto-branch to protect the integration tree during scaffolding updates
            args.start_feature = "chore/update-scaffolding"
            args.assume_yes = True

        branching_exit = execute_git_branching_phases(args, root, workspace_dir, reader, targets)
        exit_code = max(exit_code, branching_exit)

        # Phase 3: Scaffolding (Verify/Update Repos & Licenses)
        sync_exit = run_sync(args, root, workspace_dir, targets, is_create_run)
        exit_code = max(exit_code, sync_exit)

        # Phase 4 & 5: Build Orchestration
        build_exit = run_build(args, root, workspace_dir, targets)
        exit_code = max(exit_code, build_exit)

        # Phase 6: Git Authoring (Commit/Merge/Publish)
        authoring_exit = execute_git_authoring_phases(args, root, workspace_dir, reader, targets)
        exit_code = max(exit_code, authoring_exit)

    except subprocess.CalledProcessError as e:
        print(f"\n❌ Command Failed (exit code {e.returncode})", file=sys.stderr)
        return e.returncode
    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        return 1

    return exit_code

if __name__ == "__main__":
    sys.exit(main())
