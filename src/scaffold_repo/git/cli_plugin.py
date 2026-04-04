# src/scaffold_repo/git/cli_plugin.py
import argparse
import subprocess
from pathlib import Path
from .orchestrator import GitFleetManager

def add_git_arguments(parser: argparse.ArgumentParser):
    """Adds Git-specific arguments to the main CLI parser."""
    grp_git = parser.add_argument_group("Version Control & Publishing")

    # Information Phase (Only add if not already added by sync module)
    existing_actions = [action.option_strings for action in parser._actions]
    existing_flags = [flag for sublist in existing_actions for flag in sublist]

    if "--diff" not in existing_flags:
        grp_git.add_argument("--diff", action="store_true", help="Show git diff for each project")

    if "--diff-target" not in existing_flags:
        grp_git.add_argument("--diff-target", type=str, metavar="BRANCH", help="Compare current branch against TARGET (use 'AUTO' for smart detection)")

    # Preparation Phase
    grp_git.add_argument("--start-feature", type=str, metavar="NAME", help="Create a standardized feature branch (e.g., 'feat/my-change')")

    # Authoring Phase
    grp_git.add_argument("--commit", type=str, metavar="MSG", help="Commit changes")

    # Updated help text to explicitly mention pushing
    grp_git.add_argument("--publish", action="store_true", help="Smart publish: resolves feature -> dev -> main and pushes to origin")

    grp_git.add_argument("--publish-feature", action="store_true", help="Merge feature to dev")
    grp_git.add_argument("--publish-release", action="store_true", help="Merge dev to main and tag")
    grp_git.add_argument("--drop-feature", action="store_true", help="Discard feature branch")
    grp_git.add_argument("--push", action="store_true", help="Push commits")
    grp_git.add_argument("--prune-remote", action="store_true", help="Delete all remote branches except main/master")


def execute_git_transport_phases(
        args: argparse.Namespace,
        root: Path,
        workspace_dir: Path,
        reader,
        targets: list[tuple[str, str, str, dict]]
) -> int:
    """Executes the pre-generation version control steps (cloning, pulling)."""
    if not getattr(args, 'clone', False) and not getattr(args, 'pull', False):
        return 0

    print("\n\033[1m=== Git Transport ===\033[0m")
    orchestrator = GitFleetManager(workspace_dir, reader.effective_config)
    exit_code = 0

    for raw_token, stack_type, name, item in targets:
        dest = workspace_dir / name
        try:
            # 1. Ensure dependencies exist
            orchestrator.clone_dependencies(dest, item, reader)

            # 2. Clone or pull the target repository
            if getattr(args, 'clone', False) or getattr(args, 'pull', False):
                if not orchestrator.clone(dest, item, skip_if_exists=not getattr(args, 'pull', False)):
                    exit_code = max(exit_code, 1)
        except subprocess.CalledProcessError as e:
            print(f"  \033[91m! Git command failed on '{name}': {e}\033[0m")
            exit_code = max(exit_code, 1)

    return exit_code


def execute_git_branching_phases(
        args: argparse.Namespace,
        root: Path,
        workspace_dir: Path,
        reader,
        targets: list[tuple[str, str, str, dict]]
) -> int:
    """Executes the pre-generation branch creation steps."""
    if not args.start_feature:
        return 0

    print("\n\033[1m=== Git Branching ===\033[0m")
    orchestrator = GitFleetManager(workspace_dir, reader.effective_config)
    assume_yes = getattr(args, 'assume_yes', False)

    exit_code = 0
    for raw_token, stack_type, name, item in targets:
        dest = workspace_dir / name
        print(f"\n📦 Preparing branch on {name}...")
        try:
            if not orchestrator.start_feature(dest, name, item, args.start_feature, assume_yes):
                exit_code = max(exit_code, 1)
        except subprocess.CalledProcessError as e:
            print(f"  \033[91m! Git command failed on '{name}': {e}\033[0m")
            exit_code = max(exit_code, 1)

    return exit_code


def execute_git_authoring_phases(
        args: argparse.Namespace,
        root: Path,
        workspace_dir: Path,
        reader,
        targets: list[tuple[str, str, str, dict]]
) -> int:
    """Executes post-generation version control (committing, merging, pushing)."""
    if not any([args.commit, args.publish, args.publish_feature, args.publish_release, args.drop_feature, args.push, args.prune_remote]):
        return 0

    print("\n\033[1m=== Git Authoring ===\033[0m")
    orchestrator = GitFleetManager(workspace_dir, reader.effective_config)
    assume_yes = getattr(args, 'assume_yes', False)

    exit_code = 0
    for raw_token, stack_type, name, item in targets:
        dest = workspace_dir / name
        print(f"\n📦 Processing {name}...")
        try:
            if args.commit:
                if not orchestrator.commit(dest, name, args.commit):
                    exit_code = max(exit_code, 1)

            # Skip standard push if we are publishing, because publish will handle it
            if args.push and not args.publish:
                if not orchestrator.push(dest, name):
                    exit_code = max(exit_code, 1)

            if args.publish:
                tmpl_src_root = getattr(reader, 'tmpl_dir', None)
                # Hardcoded push=True here to enforce the default behavior you want
                if not orchestrator.publish(dest, name, item, tmpl_src_root, raw_token, push=True, assume_yes=assume_yes):
                    exit_code = max(exit_code, 1)
            else:
                if args.publish_feature:
                    if not orchestrator.publish_feature(dest, name, push=args.push):
                        exit_code = max(exit_code, 1)

                if args.publish_release:
                    tmpl_src_root = getattr(reader, 'tmpl_dir', None)
                    if not orchestrator.publish_release(dest, name, item, tmpl_src_root, raw_token, push=args.push, assume_yes=assume_yes):
                        exit_code = max(exit_code, 1)

            if args.drop_feature:
                if not orchestrator.drop_feature(dest, name, assume_yes=assume_yes):
                    exit_code = max(exit_code, 1)

            if args.prune_remote:
                if not orchestrator.prune_remote(dest, name, assume_yes=assume_yes):
                    exit_code = max(exit_code, 1)

        except subprocess.CalledProcessError as e:
            print(f"  \033[91m! Git command failed on '{name}': {e}\033[0m")
            exit_code = max(exit_code, 1)

    return exit_code
