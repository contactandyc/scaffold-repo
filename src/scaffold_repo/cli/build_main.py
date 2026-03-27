# src/scaffold_repo/cli/build_main.py
import argparse
import sys
from pathlib import Path

from .resolver import load_workspace_and_targets
from ..build.cli_plugin import add_build_arguments, run_build

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scaffold-build", description="Orchestrate dependency and first-party compilation.")
    ap.add_argument("projects", nargs="*", help="Projects or aliases to build")
    ap.add_argument("-C", "--cwd", type=Path, default=Path("."), help="Run as if started in <PATH>")

    # Wire up the plugin arguments!
    add_build_arguments(ap)

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

    return run_build(args, root, workspace_dir, targets)

if __name__ == "__main__":
    sys.exit(main())
