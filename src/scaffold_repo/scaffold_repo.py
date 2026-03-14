# src/scaffold_repo/scaffold_repo.py
from __future__ import annotations

import argparse
import posixpath
import subprocess
import sys
from pathlib import Path
from typing import Any

from .repo_sync import verify_repo
from .build_libs import build_all_libs
from .config_reader import ConfigReader, _slug, _snake, _deep_merge


def _interactive_select(prompt: str, options: list[str]) -> int:
    """Renders an interactive arrow-key menu. Returns the index of the selected item."""
    try:
        import sys, tty, termios
    except ImportError:
        # Fallback for Windows
        print(prompt)
        for i, opt in enumerate(options):
            print(f"  {i+1}) {opt}")
        while True:
            ans = input(f"Select [1-{len(options)}]: ").strip()
            if ans.isdigit() and 1 <= int(ans) <= len(options):
                return int(ans) - 1
            print("Invalid selection.")

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    selected = 0
    sys.stdout.write("\033[?25l")  # Hide cursor
    try:
        tty.setraw(fd)
        while True:
            sys.stdout.write(f"\r{prompt}\n")
            for i, opt in enumerate(options):
                prefix = "  ❯ " if i == selected else "    "
                color = "\033[96m" if i == selected else ""
                reset = "\033[0m"
                sys.stdout.write(f"\r{prefix}{color}{opt}{reset}\033[K\n")

            sys.stdout.flush()
            ch = sys.stdin.read(1)
            if ch == '\x1b':  # Escape sequence
                ch2 = sys.stdin.read(2)
                if ch2 == '[A': selected = max(0, selected - 1)  # Up arrow
                elif ch2 == '[B': selected = min(len(options) - 1, selected + 1)  # Down arrow
            elif ch in ('\r', '\n'):  # Enter key
                break
            elif ch == '\x03':  # Ctrl-C
                raise KeyboardInterrupt

            sys.stdout.write(f"\033[{len(options) + 1}A")  # Move cursor back up
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\033[?25h")  # Show cursor
        sys.stdout.flush()

    # Clean up the menu output so the terminal stays clean
    sys.stdout.write(f"\033[{len(options) + 1}A")  # Move up to prompt line
    sys.stdout.write("\r\033[K")  # Clear the prompt line
    sys.stdout.write("\033[J")  # Clear everything below
    sys.stdout.flush()

    return selected


def _auto_clone_target(dest: Path, item: dict, global_cfg: dict, skip_sync: bool = False) -> None:
    """If the target folder doesn't exist, try to clone it. If it exists, pull latest (unless skipped)."""
    if dest.exists():
        if not skip_sync and (dest / ".git").exists():
            print(f"  [Auto-Sync] Pulling latest changes for {dest.name}...")
            res = subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=dest, capture_output=True, text=True)
            if res.returncode != 0:
                print(f"  ! [Auto-Sync] Warning: Could not pull latest for {dest.name}. (Might be offline or have conflicts).")
        return

    url = item.get("url")
    if not url:
        gh_proj = global_cfg.get("github_project")
        name = item.get("name") or dest.name
        if gh_proj:
            url = f"https://github.com/{gh_proj}/{name}.git"

    if url:
        print(f"  [Auto-Clone] Attempting to clone {url}...")
        res = subprocess.run(["git", "clone", url, str(dest)], capture_output=True, text=True)
        if res.returncode == 0:
            print(f"  [Auto-Clone] Successfully cloned {url}.")
            return
        else:
            print(f"  [Auto-Clone] Clone failed or repository not found. Initializing empty repo.")
    else:
        print("  [Auto-Clone] No URL or github_project defined. Initializing empty repo.")

    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=dest, capture_output=True)


def _resume_feature(dest: Path, name: str, global_cfg: dict) -> None:
    """Finds open feature branches and lets the user interactively select one to checkout."""
    if not (dest / ".git").exists():
        return

    approved_prefixes = global_cfg.get("branch_prefixes", {
        "feat": "", "fix": "", "docs": "", "chore": "", "refactor": "", "test": ""
    })
    if isinstance(approved_prefixes, list):
        approved_prefixes = {p: "" for p in approved_prefixes}

    res = subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=dest, capture_output=True, text=True)
    branches = [b.strip() for b in res.stdout.splitlines() if b.strip()]

    features = [b for b in branches if "/" in b and b.split("/")[0] in approved_prefixes]

    if not features:
        print(f"  [Features] No active feature branches to resume for '{name}'.")
        return

    try:
        idx = _interactive_select(f"  [Features] Select a feature branch to resume for '{name}':", features)
        selected = features[idx]
        subprocess.run(["git", "checkout", selected], cwd=dest, capture_output=True)
        print(f"  ✓ Resumed work on \033[96m{selected}\033[0m.")
    except KeyboardInterrupt:
        print("\n  - Aborted feature selection.")


def _start_feature(dest: Path, name: str, item: dict, global_cfg: dict, feature_raw: str, assume_yes: bool = False) -> bool:
    """Enforces the main -> dev-vX.X.X -> feat/name branching topology. Returns True if successful."""
    if not (dest / ".git").exists():
        return True

    # --- SAFETY CHECK: Prevent branching with uncommitted changes ---
    status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True)
    if status.stdout.strip():
        print(f"  \033[93m! Warning: '{name}' has uncommitted changes.\033[0m")
        print("    Please commit or stash your work before starting a new feature.")
        return False
    # ----------------------------------------------------------------

    # 1. Fetch Configured Standards
    default_prefixes = {
        "feat": "A new feature",
        "fix": "A bug fix",
        "docs": "Documentation only changes",
        "chore": "Build process or tool changes"
    }
    approved_prefixes = global_cfg.get("branch_prefixes", default_prefixes)

    if isinstance(approved_prefixes, list):
        approved_prefixes = {p: "" for p in approved_prefixes}

    # 2. Normalize and Validate feature branch name
    if "/" in feature_raw:
        prefix, branch_name = feature_raw.split("/", 1)

        if prefix not in approved_prefixes:
            print(f"  \033[91m! Error: Branch prefix '{prefix}' is not approved.\033[0m")
            print("    Approved options:")
            for p_key, p_desc in approved_prefixes.items():
                desc_str = f"- {p_desc}" if p_desc else ""
                print(f"      {p_key:<10} {desc_str}")
            return False

        feature_name = f"{prefix}/{branch_name}"
    else:
        # INTERACTIVE WIZARD
        if assume_yes:
            default_prefix = next(iter(approved_prefixes.keys())) if approved_prefixes else "feat"
            feature_name = f"{default_prefix}/{feature_raw}"
            print(f"  - Auto-selected prefix '{default_prefix}' due to --assume-yes")
        else:
            prefixes_list = list(approved_prefixes.keys())
            display_opts = []
            for p_key in prefixes_list:
                p_desc = approved_prefixes[p_key]
                desc_str = f"- {p_desc}" if p_desc else ""
                display_opts.append(f"{p_key:<10} {desc_str}")

            try:
                idx = _interactive_select(f"  > Select a prefix for your new branch '{feature_raw}':", display_opts)
                selected_prefix = prefixes_list[idx]
            except KeyboardInterrupt:
                print("\n  - Aborted branch creation.")
                return False

            feature_name = f"{selected_prefix}/{feature_raw}"
            print(f"  ✓ Using branch name: \033[96m{feature_name}\033[0m")
    # 3. Determine default and target dev branch
    res_main = subprocess.run(["git", "branch", "--list", "main"], cwd=dest, capture_output=True, text=True)
    default_branch = "main" if "main" in res_main.stdout else "master"

    current_version = str(item.get("version", "0.0.1")).strip()
    parts = current_version.replace("v", "").split(".")

    if len(parts) == 3 and parts[2].isdigit():
        next_version = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    else:
        next_version = f"{current_version}-next"

    dev_branch = f"dev-v{next_version}"

    print(f"  [Branch] Preparing topology for {name}...")

    # 4. Make sure dev branch exists locally
    check_dev = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{dev_branch}"], cwd=dest)
    if check_dev.returncode != 0:
        subprocess.run(["git", "checkout", default_branch], cwd=dest, capture_output=True)
        subprocess.run(["git", "pull", "--rebase"], cwd=dest, capture_output=True)
        subprocess.run(["git", "checkout", "-b", dev_branch], cwd=dest, capture_output=True)
        print(f"  - Created integration branch '{dev_branch}' from '{default_branch}'.")
    else:
        subprocess.run(["git", "checkout", dev_branch], cwd=dest, capture_output=True)

    # 5. Create and checkout the feature branch from the dev branch
    check_feat = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{feature_name}"], cwd=dest)
    if check_feat.returncode == 0:
        subprocess.run(["git", "checkout", feature_name], cwd=dest, capture_output=True)
        print(f"  ✓ Checked out existing feature branch '{feature_name}'.")
    else:
        subprocess.run(["git", "checkout", "-b", feature_name], cwd=dest, capture_output=True)
        print(f"  ✓ Created new feature branch '{feature_name}' off of '{dev_branch}'.")

    return True


def _update_yaml_version(tmpl_dir: Path, raw_token: str, new_version: str) -> None:
    """Finds the source YAML file and intelligently updates its version line."""
    if not tmpl_dir:
        return

    possible_paths = [
        tmpl_dir / "libraries" / f"{raw_token}.yaml",
        tmpl_dir / "libraries" / f"{raw_token}.yml",
        tmpl_dir / "apps" / f"{raw_token}.yaml",
        tmpl_dir / "apps" / f"{raw_token}.yml",
        ]

    target_file = next((p for p in possible_paths if p.exists()), None)
    if not target_file:
        return

    lines = target_file.read_text(encoding="utf-8").splitlines()
    updated = False
    for i, line in enumerate(lines):
        if line.startswith("version:"):
            lines[i] = f"version: {new_version}"
            updated = True
            break

    if updated:
        target_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            rel_path = target_file.relative_to(Path.cwd())
        except ValueError:
            rel_path = target_file.name
        print(f"  ✓ Updated registry ({rel_path}) to version: {new_version}")


def _resolve_projects(reader: ConfigReader, projects: list[str]) -> list[tuple[str, str, str, dict]]:
    """Resolve project tokens to (display_name, slug, raw_token, item_dict)."""
    for p in projects:
        if "/" in p:
            for pth in [f"libraries/{p}.yaml", f"apps/{p}.yaml"]:
                data = reader.tmpl_src._load_logical_path(pth)
                if data:
                    reader.cfg = _deep_merge(reader.cfg, data)
                    break
        else:
            for f in reader.tmpl_src.find_registry_yamls(f"libraries/{p}"):
                data = reader.tmpl_src._load_logical_path(f)
                if data: reader.cfg = _deep_merge(reader.cfg, data)
            for f in reader.tmpl_src.find_registry_yamls(f"apps/{p}"):
                data = reader.tmpl_src._load_logical_path(f)
                if data: reader.cfg = _deep_merge(reader.cfg, data)

    idx = reader._build_library_index(reader.effective_config)
    by_name_lower = {v["name"].lower(): k for k, v in idx.items()}
    by_snake = {v["snake"]: k for k, v in idx.items()}

    def needs_scaffolding(item: dict) -> bool:
        return bool(item.get("template"))

    out: list[tuple[str, str, str, dict]] = []
    for p in projects:
        if p.lower() == "all":
            cands = [s for s, v in idx.items() if needs_scaffolding(v["item"])]
            ordered = reader._toposort_subset(idx, cands)
            out.extend([(idx[s]["name"], s, idx[s].get("raw_key", s), idx[s]["item"]) for s in ordered])
            continue

        namespace_cands = [s for s, v in idx.items() if str(v.get("raw_key", "")).startswith(f"{p}/") and needs_scaffolding(v["item"])]
        if namespace_cands:
            ordered = reader._toposort_subset(idx, namespace_cands)
            out.extend([(idx[s]["name"], s, idx[s]["raw_key"], idx[s]["item"]) for s in ordered])
            continue

        slug = _slug(p)
        key = slug if slug in idx else by_snake.get(_snake(p), by_name_lower.get(p.lower()))
        if key:
            out.append((idx[key]["name"], key, idx[key].get("raw_key", p), idx[key]["item"]))

    seen: set[str] = set()
    uniq: list[tuple[str, str, str, dict]] = []
    for name, slug, raw_token, item in out:
        if slug not in seen:
            seen.add(slug)
            uniq.append((name, slug, raw_token, item))
    return uniq


def _print_text_result(res: dict[str, Any]) -> None:
    s = res.get("summary", {})
    print(f"\n\033[1m=== {res['repo']} ===\033[0m")
    print(
        "files_checked: {fc}  headers_added: {ha}  headers_updated: {hu}  unchanged: {un}".format(
            fc=s.get("files_checked", 0),
            ha=s.get("headers_added", 0),
            hu=s.get("headers_updated", 0),
            un=s.get("unchanged", 0),
        )
    )
    if s.get("profiles_used"):
        print(f"profiles_used: {', '.join(s['profiles_used'])}")
    for it in res.get("issues", []):
        kind = it.get("type", "issue")
        if "file" in it:
            print(f"- {kind}: {it['file']}")
        else:
            print(f"- {kind}: {it.get('message', '')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scaffold-repo",
        description="The Declarative Fleet Manager: Scaffold repos, enforce OSS, orchestrate builds, and sync to Git."
    )

    ap.add_argument(
        "projects",
        nargs="*",
        help="One or more projects/namespaces to scaffold or build (e.g., 'andy-curtis/a-bitset-library', 'common', 'all')"
    )

    grp_ws = ap.add_argument_group("Workspace Options")
    grp_ws.add_argument("-C", "--cwd", type=Path, default=Path("."), help="Run as if started in <PATH> (default: current dir)")
    grp_ws.add_argument("--templates-dir", type=Path, default=None, help="Override templates directory (auto-detects ./templates)")
    grp_ws.add_argument("--start-feature", nargs="?", const="", metavar="NAME", help="Start a feature branch (leave empty to list features)")

    grp_sc = ap.add_argument_group("Scaffolding Options")
    grp_sc.add_argument("--diff", action="store_true", help="Print unpaginated Git diffs for all targeted repos")
    grp_sc.add_argument("-y", "--assume-yes", action="store_true", help="Apply template updates without prompting")
    grp_sc.add_argument("--show-diffs", action="store_true", help="Print inline diffs before applying file updates")
    grp_sc.add_argument("--no-prompt", action="store_true", help="Do not prompt during SPDX license header fixups")

    grp_dp = ap.add_argument_group("Dependency Lifecycle")
    grp_dp.add_argument("--clone-deps", action="store_true", help="Fetch external dependencies without compiling")
    grp_dp.add_argument("--build-deps", action="store_true", help="Fetch and compile external dependencies")
    grp_dp.add_argument("--install-deps", action="store_true", help="Fetch, compile, and install external dependencies")

    grp_tl = ap.add_argument_group("Target Lifecycle (Your Code)")
    grp_tl.add_argument("--build", action="store_true", help="Run './build.sh build' on the scaffolded projects")
    grp_tl.add_argument("--install", action="store_true", help="Run './build.sh install' on the scaffolded projects")

    grp_git = ap.add_argument_group("Git Orchestration (Your Code)")
    grp_git.add_argument("--commit", type=str, metavar="MSG", help="Commit changes in target projects (blocked on 'main' and 'dev-*')")
    grp_git.add_argument("--publish-feature", action="store_true", help="Merge current feature branch into dev branch and delete feature branch")
    grp_git.add_argument("--publish-release", action="store_true", help="Merge an integration branch into main, tag it, and bump YAML")
    grp_git.add_argument("--drop-feature", action="store_true", help="Discard current feature branch (and uncommitted changes), return to dev")
    grp_git.add_argument("--push", action="store_true", help="Push commits to origin (Pushes tags and handles remote deletion if publishing)")

    args = ap.parse_args(argv)
    root = args.cwd.resolve()

    tmpl_dir = args.templates_dir.resolve() if args.templates_dir else None
    if not tmpl_dir and (root / "templates").is_dir():
        tmpl_dir = root / "templates"

    project_tokens = args.projects
    results: list[dict[str, Any]] = []
    exit_code = 0

    try:
        reader = ConfigReader(root, project_name=None, templates_dir=(tmpl_dir.as_posix() if tmpl_dir else None))
        reader.load()
        targets = _resolve_projects(reader, project_tokens) if project_tokens else []
        workspace_dir = reader.effective_config.get("workspace_dir", "../repos")

        # --- SHORT CIRCUIT: PURE GIT DIFF OUTPUT ---
        if args.diff:
            print("\n\033[1m=== Fleet Git Diffs ===\033[0m")
            if not targets:
                targets = [("Root Repo", "root", None, {})]

            for name, slug, raw_token, item in targets:
                dest = root if not raw_token else root / workspace_dir / _slug(posixpath.basename(raw_token))

                # Use raw_token for the display label (e.g. 'andy-curtis/a-bitset-library')
                display_label = raw_token if raw_token else name

                if (dest / ".git").exists():
                    status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True)
                    if status.stdout.strip():
                        print(f"\n\033[93mREPO-CHANGED:\033[0m {display_label}")
                        print(f"\033[96m--- Diff for {name} ({dest.name}) ---\033[0m")
                        subprocess.run(["git", "add", "--intent-to-add", "."], cwd=dest, capture_output=True)
                        subprocess.run(["git", "--no-pager", "diff"], cwd=dest)
                    else:
                        print(f"\033[92mREPO-UNCHANGED:\033[0m {display_label}")
                else:
                    print(f"\033[90mREPO-MISSING:\033[0m {display_label} (Not cloned/No .git directory)")
            return 0

        # --- PRE-COMPUTE PIPELINE BYPASSES ---
        skip_templates = False
        if args.drop_feature or args.publish_feature or args.publish_release:
            skip_templates = True
        elif (args.commit or args.push) and not (args.assume_yes or args.show_diffs or (args.start_feature is not None)):
            skip_templates = True

        # --- PHASE 1: Scaffolding Target Projects ---
        if targets:
            for name, slug, raw_token, item in targets:
                folder_name = _slug(posixpath.basename(raw_token))
                dest = root / workspace_dir / folder_name

                # If the user just wants to list features, don't do the heavy sync and scaffold steps
                if args.start_feature == "":
                    _resume_feature(dest, name, reader.effective_config)
                    continue

                print(f"\n\033[95m=== Scaffolding {name} ({slug}) into {dest} ===\033[0m")
                dest = dest.resolve()

                # 1. Ensure repo exists and is synced (Bypassed for Git-only runs to prevent rebase errors on dirty trees)
                _auto_clone_target(dest, item, reader.effective_config, skip_sync=skip_templates)

                # 2. Enforce the Main -> Dev -> Feature topology
                if args.start_feature is not None:
                    if not _start_feature(dest, name, item, reader.effective_config, args.start_feature, args.assume_yes):
                        print("  \033[91m! Skipping scaffolding for this repo.\033[0m")
                        exit_code = max(exit_code, 1)
                        continue

                # 3. Apply templates (Bypassed if pure Git operation)
                if skip_templates:
                    print("  - Bypassing template verification (Git operation only).")
                else:
                    code, res = verify_repo(
                        dest,
                        fix_licenses=True,
                        no_prompt=args.no_prompt,
                        project_name=raw_token,
                        templates_dir=tmpl_dir.as_posix() if tmpl_dir else None,
                        assume_yes=args.assume_yes,
                        show_diffs=args.show_diffs,
                    )
                    exit_code = max(exit_code, code)
                    results.append(res)

        else:
            print(f"\n\033[95m=== Scaffolding in-place ({root}) ===\033[0m")
            if skip_templates:
                print("  - Bypassing template verification (Git operation only).")
            else:
                code, res = verify_repo(
                    root,
                    fix_licenses=True,
                    no_prompt=args.no_prompt,
                    project_name=None,
                    templates_dir=tmpl_dir.as_posix() if tmpl_dir else None,
                    assume_yes=args.assume_yes,
                    show_diffs=args.show_diffs,
                )
                exit_code = max(exit_code, code)
                results.append(res)

        # --- PHASE 2: External Dependencies ---
        if args.clone_deps or args.build_deps or args.install_deps:
            build_all_libs(
                repo=root,
                project_tokens=project_tokens,
                templates_dir=tmpl_dir.as_posix() if tmpl_dir else None,
                do_clone=args.clone_deps,
                do_build=args.build_deps,
                do_install=args.install_deps,
            )

        # --- PHASE 3: First-Party Build Orchestration ---
        if targets and (args.build or args.install):
            print("\n\033[1m=== Phase 3: First-Party Build Orchestration ===\033[0m")
            for name, slug, raw_token, item in targets:
                dest = root / workspace_dir / _slug(posixpath.basename(raw_token))
                cmd = "install" if args.install else "build"

                print(f"\n🚀 Running './build.sh {cmd}' for {name}...")
                subprocess.run(["./build.sh", cmd], cwd=dest, check=True)

        # --- PHASE 4: Git Orchestration ---
        if targets and (args.commit or args.publish_feature or args.publish_release or args.drop_feature or args.push):
            print("\n\033[1m=== Phase 4: Git Orchestration ===\033[0m")

            for name, slug, raw_token, item in targets:
                dest = root / workspace_dir / _slug(posixpath.basename(raw_token))

                if not (dest / ".git").exists():
                    print(f"  [Git] Skipping {name}: Not a git repository.")
                    continue

                print(f"\n📦 Syncing {name} to Git...")

                res = subprocess.run(["git", "branch", "--show-current"], cwd=dest, capture_output=True, text=True)
                current_branch = res.stdout.strip()

                res_main = subprocess.run(["git", "branch", "--list", "main"], cwd=dest, capture_output=True, text=True)
                default_branch = "main" if "main" in res_main.stdout else "master"

                # 1. COMMIT LOGIC (Protected)
                if args.commit:
                    if current_branch == default_branch or current_branch.startswith("dev-"):
                        print(f"  ! Cannot commit directly to '{current_branch}'. Start a feature branch first.")
                    else:
                        status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True)
                        if status.stdout.strip():
                            subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
                            subprocess.run(["git", "commit", "-m", args.commit], cwd=dest, check=True)
                            print(f"  ✓ Committed changes to '{current_branch}': '{args.commit}'")
                        else:
                            print(f"  - No changes to commit on '{current_branch}'.")

                # 2. STANDARD PUSH LOGIC (Protected, non-merge)
                if args.push and not (args.publish_release or args.publish_feature or args.drop_feature):
                    if current_branch == default_branch or current_branch.startswith("dev-"):
                        print(f"  ! Cannot push directly to '{current_branch}'. Use a feature branch or publish commands.")
                    else:
                        print(f"  - Pushing branch '{current_branch}' to origin...")
                        res = subprocess.run(["git", "push", "-u", "origin", current_branch], cwd=dest, capture_output=True, text=True)
                        if res.returncode == 0:
                            print(f"  ✓ Pushed '{current_branch}'.")
                        else:
                            print(f"  ! Failed to push: {res.stderr.strip()}")

                # 3. MERGE FEATURE TO DEV (Publish Feature)
                if args.publish_feature:
                    if current_branch == default_branch or current_branch.startswith("dev-"):
                        print(f"  ! Cannot publish from '{current_branch}'. Must be on an active feature branch.")
                        continue

                    current_version = str(item.get("version", "0.0.1")).strip()
                    parts = current_version.replace("v", "").split(".")
                    guess_version = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}" if len(parts) == 3 and parts[2].isdigit() else f"{current_version}-next"
                    default_dev = f"dev-v{guess_version}"

                    if args.assume_yes:
                        target_dev = default_dev
                        print(f"  - Auto-selected integration branch: '{target_dev}'")
                    else:
                        ans = input(f"  > Merge '{current_branch}' into which dev branch? [{default_dev}]: ").strip()
                        target_dev = ans if ans else default_dev

                    check_b = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{target_dev}"], cwd=dest)
                    if check_b.returncode != 0:
                        print(f"  ! Integration branch '{target_dev}' not found locally. Skipping.")
                        continue

                    print(f"  - Merging '{current_branch}' into '{target_dev}'...")
                    subprocess.run(["git", "checkout", target_dev], cwd=dest, capture_output=True)
                    subprocess.run(["git", "pull", "--rebase", "origin", target_dev], cwd=dest, capture_output=True)
                    merge_res = subprocess.run(["git", "merge", current_branch, "--no-ff", "-m", f"Merge feature '{current_branch}'"], cwd=dest, capture_output=True, text=True)

                    if merge_res.returncode != 0:
                        print(f"  ! Merge conflict in {name}. Aborting merge.")
                        subprocess.run(["git", "merge", "--abort"], cwd=dest, capture_output=True)
                        continue
                    print(f"  ✓ Merged '{current_branch}' into '{target_dev}'.")

                    subprocess.run(["git", "branch", "-d", current_branch], cwd=dest, capture_output=True)
                    print(f"  ✓ Deleted local feature branch '{current_branch}'.")

                    if args.push:
                        print(f"  - Pushing '{target_dev}' to origin...")
                        res_dev = subprocess.run(["git", "push", "origin", target_dev], cwd=dest, capture_output=True, text=True)
                        if res_dev.returncode == 0:
                            print(f"  ✓ Pushed '{target_dev}'.")
                        else:
                            print(f"  ! Failed to push '{target_dev}'.")

                        # Try to delete remote feature branch silently
                        subprocess.run(
                            ["git", "push", "origin", "--delete", current_branch],
                            cwd=dest,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL
                        )

                # 4. MERGE DEV TO MAIN & TAG (Publish Release)
                if args.publish_release:
                    current_version = str(item.get("version", "0.0.1")).strip()
                    parts = current_version.replace("v", "").split(".")

                    guess_version = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}" if len(parts) == 3 and parts[2].isdigit() else f"{current_version}-next"
                    default_dev = f"dev-v{guess_version}"

                    if args.assume_yes:
                        release_branch = default_dev
                        print(f"  - Auto-selected release branch: '{release_branch}'")
                    else:
                        ans = input(f"  > Which integration branch are we releasing? [{default_dev}]: ").strip()
                        release_branch = ans if ans else default_dev

                    check_b = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{release_branch}"], cwd=dest)
                    if check_b.returncode != 0:
                        print(f"  ! Integration branch '{release_branch}' not found locally. Skipping.")
                        continue

                    tag_name = release_branch[4:] if release_branch.startswith("dev-") else f"v{guess_version}"

                    print(f"  - Merging '{release_branch}' into '{default_branch}'...")

                    subprocess.run(["git", "checkout", default_branch], cwd=dest, capture_output=True)
                    subprocess.run(["git", "pull", "--rebase", "origin", default_branch], cwd=dest, capture_output=True)
                    merge_res = subprocess.run(["git", "merge", release_branch, "--no-ff", "-m", f"Release {tag_name}"], cwd=dest, capture_output=True, text=True)

                    if merge_res.returncode != 0:
                        print(f"  ! Merge conflict in {name}. Aborting merge.")
                        subprocess.run(["git", "merge", "--abort"], cwd=dest, capture_output=True)
                        continue
                    print(f"  ✓ Merged '{release_branch}' into '{default_branch}'.")

                    # Apply Tag
                    check_tag = subprocess.run(["git", "tag", "-l", tag_name], cwd=dest, capture_output=True, text=True)
                    if tag_name in check_tag.stdout.split():
                        print(f"  - Tag '{tag_name}' already exists locally. Skipping tag creation.")
                    else:
                        res = subprocess.run(["git", "tag", "-a", tag_name, "-m", f"Release {tag_name}"], cwd=dest, capture_output=True, text=True)
                        if res.returncode == 0:
                            print(f"  ✓ Created tag '{tag_name}'")
                        else:
                            print(f"  ! Failed to create tag '{tag_name}'.")

                    # UPDATE YAML REGISTRY
                    if tmpl_dir:
                        new_yaml_version = tag_name[1:] if tag_name.startswith("v") else tag_name
                        _update_yaml_version(tmpl_dir, raw_token, new_yaml_version)

                    # Push Release
                    if args.push:
                        print(f"  - Pushing '{default_branch}' and '{tag_name}' to origin...")
                        res_main = subprocess.run(["git", "push", "origin", default_branch], cwd=dest, capture_output=True, text=True)
                        res_tag = subprocess.run(["git", "push", "origin", tag_name], cwd=dest, capture_output=True, text=True)

                        if res_main.returncode == 0 and res_tag.returncode == 0:
                            print(f"  ✓ Pushed release successfully.")
                        else:
                            print(f"  ! Failed to push release.")

                # 5. DROP FEATURE
                if args.drop_feature:
                    if current_branch == default_branch or current_branch.startswith("dev-"):
                        print(f"  ! Cannot drop '{current_branch}'. Must be on an active feature branch.")
                        continue

                    current_version = str(item.get("version", "0.0.1")).strip()
                    parts = current_version.replace("v", "").split(".")
                    guess_version = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}" if len(parts) == 3 and parts[2].isdigit() else f"{current_version}-next"
                    default_dev = f"dev-v{guess_version}"

                    if args.assume_yes:
                        target_dev = default_dev
                        print(f"  - Auto-selected integration branch: '{target_dev}'")
                    else:
                        ans = input(f"  > Drop '{current_branch}' and return to which dev branch? [{default_dev}]: ").strip()
                        target_dev = ans if ans else default_dev

                    check_b = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{target_dev}"], cwd=dest)
                    if check_b.returncode != 0:
                        print(f"  ! Integration branch '{target_dev}' not found locally. Skipping.")
                        continue

                    if not args.assume_yes:
                        ans_confirm = input(f"  > \033[91mWARNING: This will permanently delete '{current_branch}' and all uncommitted changes. Proceed? [y/N]:\033[0m ").strip().lower()
                        if ans_confirm not in ("y", "yes"):
                            print("  - Aborted dropping feature.")
                            continue

                    print(f"  - Dropping '{current_branch}' and switching to '{target_dev}'...")

                    subprocess.run(["git", "reset", "--hard"], cwd=dest, capture_output=True)
                    subprocess.run(["git", "checkout", target_dev], cwd=dest, capture_output=True)
                    subprocess.run(["git", "branch", "-D", current_branch], cwd=dest, capture_output=True)

                    print(f"  ✓ Deleted local feature branch '{current_branch}'.")

                    subprocess.run(
                        ["git", "push", "origin", "--delete", current_branch],
                        cwd=dest,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )

    except subprocess.CalledProcessError as e:
        print(f"\n❌ Command Failed (exit code {e.returncode})", file=sys.stderr)
        return e.returncode
    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        return 1

    if not (args.diff or args.start_feature == "" or skip_templates):
        for res in results:
            _print_text_result(res)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
