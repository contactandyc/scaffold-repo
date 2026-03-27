# src/scaffold_repo/cli/init_main.py
import argparse
import sys
from ..init.cli_plugin import add_init_arguments, run_init

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scaffold-init", description="Initialize a .scaffoldrc workspace configuration.")
    add_init_arguments(ap)

    args = ap.parse_args(argv)

    # We only have one job here!
    return run_init()

if __name__ == "__main__":
    sys.exit(main())
