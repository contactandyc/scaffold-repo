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
        targets = [(args.create, slug(args.create), args.create, {})]
        args.update = True
        is_create_run = True

    # Validate Targets
    if args.projects and not targets and not args.create:
        print(f"\n❌ Error: Could not resolve any valid projects from: {args.projects}")
        print(f"   Check your spelling or ensure your templates/resources/aliases.yaml is defined correctly.")
        return 1

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
        is_dry_run_cli = getattr(args, 'dry_run', False)
        skip_templates = not (args.update or getattr(args, 'assume_yes', False) or getattr(args, 'show_diffs', False) or is_dry_run_cli)

        args.clone = True
        if not skip_templates:
            args.pull = True

        # Phase 1: Git Transport
        if not is_create_run:
            transport_exit = execute_git_transport_phases(args, root, workspace_dir, reader, targets)
            exit_code = max(exit_code, transport_exit)

        # ── PHASE 1.5: THE IDEMPOTENCY CHECK ──
        if (getattr(args, 'update', False) or is_dry_run_cli) and not is_create_run:
            print("\n🔍 Checking for platform drift and local configuration changes...")

            drifting_targets = []
            for t in targets:
                name, project_slug, raw_token, item = t
                dest = root if not raw_token else workspace_dir / slug(posixpath.basename(raw_token))

                # Check for template drift
                _, has_drift = run_sync(args, root, workspace_dir, [t], is_create_run, dry_run=True, quiet=True)

                # Check for local scaffold.yaml modifications
                local_config_changed = False
                manifest_path = dest / "scaffold.yaml"
                if manifest_path.exists() and (dest / ".git").exists():
                    status = subprocess.run(
                        ["git", "status", "--porcelain", "scaffold.yaml"],
                        cwd=dest, capture_output=True, text=True
                    )
                    if status.stdout.strip():
                        local_config_changed = True
                        if not getattr(args, 'quiet', False):
                            print(f"  - Detected local uncommitted changes to {name}/scaffold.yaml")

                if has_drift or local_config_changed or getattr(args, 'force', False):
                    drifting_targets.append(t)

            if not drifting_targets:
                print("\n✅ All projects are completely up-to-date. Skipping update.")
                return 0 # Perfect exit!

            if is_dry_run_cli:
                # Run loud on the filtered targets
                run_sync(args, root, workspace_dir, drifting_targets, is_create_run, dry_run=True, quiet=False)
                print("\n👻 Dry run complete. The templates and licenses above would be modified.")
                return 0 # Stop before branching!

            # OVERWRITE TARGETS: Only proceed with repos that actually need updates!
            targets = drifting_targets

        # Phase 2: Git Branching
        if not is_create_run:
            if getattr(args, 'update', False) and getattr(args, 'start_feature', None) is None:
                args.start_feature = "chore/update-scaffolding"
                args.assume_yes = True

            branching_exit = execute_git_branching_phases(args, root, workspace_dir, reader, targets)
            exit_code = max(exit_code, branching_exit)

        # Phase 3: Scaffolding (The Actual Write)
        # We run it quietly because Phase 1.5 already printed the summary!
        sync_exit, _ = run_sync(args, root, workspace_dir, targets, is_create_run, dry_run=False, quiet=not is_create_run)
        exit_code = max(exit_code, sync_exit)

        # Phase 4 & 5: Build Orchestration
        build_exit = run_build(args, root, workspace_dir, targets)
        exit_code = max(exit_code, build_exit)

        # Phase 6: Git Authoring (Commit/Merge/Publish)
        if not is_create_run:
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
