# src/scaffold_repo/init/cli_plugin.py
import argparse
from ..cli.workspace import init_scaffoldrc

def add_init_arguments(parser: argparse.ArgumentParser) -> None:
    """Appends workspace initialization arguments."""
    parser.add_argument("--init", action="store_true", help="Initialize a .scaffoldrc workspace configuration")

def run_init() -> int:
    """Executes the workspace initialization wizard."""
    return init_scaffoldrc()
