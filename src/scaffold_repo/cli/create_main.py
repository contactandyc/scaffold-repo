# src/scaffold_repo/cli/create_main.py
import argparse
import sys
from pathlib import Path

from ..core.config import ConfigReader
from .workspace import find_scaffoldrc
from ..create.cli_plugin import add_create_arguments, run_create

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scaffold-create", description="Bootstrap a new repository.")

    # We only need the global CWD arg and the create plugin args here
    ap.add_argument("-C", "--cwd", type=Path, default=Path("."), help="Run as if started in <PATH>")
    add_create_arguments(ap)

    args = ap.parse_args(argv)
    root = args.cwd.resolve()

    if not args.create:
        print("❌ Error: You must provide a project name using --create <SLUG>")
        return 1

    rc = find_scaffoldrc(root)
    ws_str = rc.get("workspace_dir") or "../repos"
    workspace_dir = Path(ws_str).expanduser()
    if not workspace_dir.is_absolute():
        workspace_dir = (root / workspace_dir).resolve()

    reader = ConfigReader(root, project_name=None)
    reader.load()

    return run_create(args.create, workspace_dir, reader, rc)

if __name__ == "__main__":
    sys.exit(main())
