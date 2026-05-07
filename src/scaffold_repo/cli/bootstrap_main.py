# src/scaffold_repo/cli/bootstrap_main.py
import argparse
import sys
from pathlib import Path

# Import the orchestrator we just created
from ..bootstrap.orchestrator import run_bootstrap

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scaffold-bootstrap",
        description="Hermetically bootstrap a cloned project and its dependencies."
    )
    ap.add_argument("-C", "--cwd", type=Path, default=Path("."), help="Run as if started in <PATH>")

    args = ap.parse_args(argv)

    # Pass the current working directory to the orchestrator
    return run_bootstrap(args.cwd)

if __name__ == "__main__":
    sys.exit(main())
