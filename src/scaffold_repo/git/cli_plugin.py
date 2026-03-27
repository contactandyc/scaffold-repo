# src/scaffold_repo/git/cli_plugin.py
from __future__ import annotations

import argparse
import posixpath
import subprocess
import sys
from pathlib import Path

from .orchestrator import GitFleetManager
from ..core.config import ConfigReader
from ..utils.text import slug


def _warn_if_local_templates_unpushed(reader: ConfigReader) -> None:
    """Warns the user if their local template registry has unpushed changes before publishing."""
    if not reader.tmpl_src or not reader.tmpl_src._pkg_root:
        return

    tmpl_root = reader.tmpl_src._pkg_root
    try:
        res = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=tmpl_root, capture_output=True, text=True, check=True)
        git_root = Path(res.stdout.strip())
    except subprocess.CalledProcessError:
        return

    if ".cache" in str(git_root):
        return

    status = subprocess.run(["git", "status", "--porcelain"], cwd=git_root, capture_output=True, text=True)
    if status.stdout.strip():
        print(f"\n  \033[93m⚠️  WARNING: You have uncommitted changes in your local template repo: {git_root.name}\033[0m")
        print(f"  \033[93m   Other developers will not get these template changes until you commit and push them.\033[0m")
        return

    try:
        has_up = subprocess.run(["git", "rev-parse", "--abbrev-ref", "@{u}"], cwd=git_root, capture_output=True, text=True)
        if has_up.returncode == 0:
            unpushed = subprocess.run(["git", "log", "@{u}..HEAD", "--oneline"], cwd=git_root, capture_output=True, text=True)
            if unpushed.stdout.strip():
                print(f"\n  \033[93m⚠️  WARNING: You have unpushed commits in your local template repo: {git_root.name}\033[0m")
                print(f"  \033[93m   Make sure to push them so the rest of the fleet uses the updated templates.\033[0m")
    except Exception:
        pass


def add_git_arguments(parser: argparse.ArgumentParser) -> None:
    """Appends ALL Git orchestration arguments (Transport, Branching & Authoring)."""
    grp_git = parser.add_argument_group("Git Orchestration")

    # Transport Phase
    grp_git.add_argument("--clone", action="store_true", help="Clone the targeted repositories")
    grp_git.add_argument("--pull", action="store_true", help="Pull latest changes for targeted repositories")
    grp_git.add_argument("--clone-deps", action="store_true", help="Fetch external dependencies for targeted repos")

    # Branching Phase
    grp_git.add_argument("--start-feature", nargs="?", const="", metavar="NAME", help="Start a feature branch")

    # Authoring Phase
    grp_git.add_argument("--commit", type=str, metavar="MSG", help="Commit changes")
    grp_git.add_argument("--publish-feature", action="store_true", help="Merge feature to dev")
    grp_git.add_argument("--publish-release", action="store_true", help="Merge dev to main and tag")
    grp_git.add_argument("--drop-feature", action="store_true", help="Discard feature branch")
    grp_git.add_argument("--push", action="store_true", help="Push commits")


def execute_git_transport_phases(
        args: argparse.Namespace,
        root: Path,
        workspace_dir: Path,
        reader: ConfigReader,
        targets: list[tuple[str, str, str, dict]]
) -> int:
    """Executes network-dependent phases (clone, pull, clone-deps)."""
    if not any([args.clone, args.pull, args.clone_deps]):
        return 0

    print("\n\033[1m=== Git Transport ===\033[0m")
    orchestrator = GitFleetManager(workspace_dir, reader.effective_config)
    exit_code = 0

    for name, project_slug, raw_token, item in targets:
        dest = root if not raw_token else workspace_dir / slug(posixpath.basename(raw_token))

        if args.clone:
            if not orchestrator.clone(dest, item):
                exit_code = max(exit_code, 1)

        if args.pull and (dest / ".git").exists():
            if not orchestrator.pull(dest, name):
                exit_code = max(exit_code, 1)

        if args.clone_deps:
            # Pass the destination, the item (in case we need to clone it), and the reader
            orchestrator.clone_dependencies(dest, item, reader)

    return exit_code

def execute_git_branching_phases(
        args: argparse.Namespace,
        root: Path,
        workspace_dir: Path,
        reader: ConfigReader,
        targets: list[tuple[str, str, str, dict]]
) -> int:
    """Executes pre-generation branching (e.g., checking out feature branches)."""
    if getattr(args, 'start_feature', None) is None:
        return 0

    print("\n\033[1m=== Git Branching ===\033[0m")
    orchestrator = GitFleetManager(workspace_dir, reader.effective_config)
    exit_code = 0
    assume_yes = getattr(args, "assume_yes", False)

    for name, project_slug, raw_token, item in targets:
        dest = root if not raw_token else workspace_dir / slug(posixpath.basename(raw_token))

        if not (dest / ".git").exists():
            continue

        try:
            print(f"\n📦 Preparing branch on {name}...")
            if not orchestrator.start_feature(dest, name, item, args.start_feature, assume_yes):
                exit_code = max(exit_code, 1)
        except subprocess.CalledProcessError as e:
            print(f"  \033[91m❌ Git Command Failed (exit code {e.returncode})\033[0m", file=sys.stderr)
            exit_code = max(exit_code, e.returncode)

    return exit_code


def execute_git_authoring_phases(
        args: argparse.Namespace,
        root: Path,
        workspace_dir: Path,
        reader: ConfigReader,
        targets: list[tuple[str, str, str, dict]]
) -> int:
    """Executes post-generation version control (committing, merging, pushing)."""
    if not any([args.commit, args.publish_feature, args.publish_release, args.drop_feature, args.push]):
        return 0

    print("\n\033[1m=== Git Authoring ===\033[0m")
    orchestrator = GitFleetManager(workspace_dir, reader.effective_config)
    exit_code = 0
    assume_yes = getattr(args, "assume_yes", False)

    if args.push or args.publish_feature or args.publish_release:
        _warn_if_local_templates_unpushed(reader)

    for name, project_slug, raw_token, item in targets:
        dest = root if not raw_token else workspace_dir / slug(posixpath.basename(raw_token))

        if not (dest / ".git").exists():
            continue

        try:
            print(f"\n📦 Syncing {name} to Git...")
            res = subprocess.run(["git", "branch", "--show-current"], cwd=dest, capture_output=True, text=True)
            current_branch = res.stdout.strip()
            status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True)
            is_dirty = bool(status.stdout.strip())

            # The auto-commit hook for scaffolding updates
            if (args.publish_feature or args.commit) and current_branch == "chore/update-scaffolding" and is_dirty:
                msg = args.commit if args.commit else "chore: apply scaffolding updates"
                print(f"  [Auto-Commit] Committing scaffolding updates on '{current_branch}'...")
                subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
                subprocess.run(["git", "commit", "-m", msg], cwd=dest, check=True)

            if args.commit and current_branch != "chore/update-scaffolding":
                if not orchestrator.commit(dest, name, args.commit):
                    exit_code = max(exit_code, 1)

            if args.push and not (args.publish_release or args.publish_feature or args.drop_feature):
                if not orchestrator.push(dest, name):
                    exit_code = max(exit_code, 1)

            if args.publish_feature:
                if not orchestrator.publish_feature(dest, name, push=args.push):
                    exit_code = max(exit_code, 1)

            if args.publish_release:
                tmpl_root = reader.tmpl_src._pkg_root if reader.tmpl_src else None
                if not orchestrator.publish_release(dest, name, item, tmpl_root, raw_token, push=args.push, assume_yes=assume_yes):
                    exit_code = max(exit_code, 1)

            if args.drop_feature:
                if not orchestrator.drop_feature(dest, name, assume_yes=assume_yes):
                    exit_code = max(exit_code, 1)

        except subprocess.CalledProcessError as e:
            print(f"  \033[91m❌ Git Command Failed (exit code {e.returncode})\033[0m", file=sys.stderr)
            exit_code = max(exit_code, e.returncode)

    return exit_code
