# src/scaffold_repo/cli/sync_main.py
import argparse
import sys
from pathlib import Path

from .resolver import load_workspace_and_targets
from ..sync.cli_plugin import add_sync_arguments, run_sync

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scaffold-sync", description="Verify and update Jinja templates and OSS licenses.")
    ap.add_argument("projects", nargs="*", help="Projects or aliases to sync")
    ap.add_argument("-C", "--cwd", type=Path, default=Path("."), help="Run as if started in <PATH>")

    # Wire up the plugin arguments!
    add_sync_arguments(ap)

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

    # Unpack the tuple to get the exit code
    exit_code, _has_drift = run_sync(args, root, workspace_dir, targets, is_create_run=False, dry_run=getattr(args, 'dry_run', False))
    return exit_code

if __name__ == "__main__":
    sys.exit(main())
