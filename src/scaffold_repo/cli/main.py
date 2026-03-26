# src/scaffold_repo/cli/main.py
from __future__ import annotations

import argparse
import posixpath
import subprocess
import sys
import yaml
from pathlib import Path
from typing import Any
from datetime import date

from ..repo_sync import verify_repo
from ..build_libs import build_all_libs
from ..core.config import ConfigReader
from ..utils.text import slug, snake
from ..utils.collections import deep_merge
from .workspace import init_scaffoldrc, find_scaffoldrc, append_stack_to_workspace
from .ui import interactive_select, print_text_result

def _warn_if_local_templates_unpushed(reader: ConfigReader) -> None:
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

def _get_active_git_project(current_dir: Path) -> str | None:
    try:
        res = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=current_dir, capture_output=True, text=True, check=True)
        return Path(res.stdout.strip()).name
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

def _auto_clone_target(dest: Path, item: dict, global_cfg: dict, skip_sync: bool = False) -> None:
    if dest.exists():
        if not skip_sync and (dest / ".git").exists():
            print(f"  [Auto-Sync] Pulling latest changes for {dest.name}...")
            res = subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=dest, capture_output=True, text=True)
            if res.returncode != 0:
                print(f"  ! [Auto-Sync] Warning: Could not pull latest for {dest.name}.")
        return

    url = item.get("url")
    branch = item.get("branch")
    shallow = item.get("shallow")

    if not url:
        gh_proj = global_cfg.get("github_project")
        name = item.get("name") or dest.name
        if gh_proj:
            url = f"https://github.com/{gh_proj}/{name}.git"

    if url:
        clone_cmd = ["git", "clone"]
        if shallow:
            clone_cmd.extend(["--depth", "1"])
        if branch:
            clone_cmd.extend(["--branch", branch, "--single-branch"])
        clone_cmd.extend([url, str(dest)])

        import shlex
        cmd_str = " ".join(shlex.quote(c) for c in clone_cmd)
        print(f"  [Auto-Clone] Executing: $ {cmd_str}")

        res = subprocess.run(clone_cmd, capture_output=True, text=True)
        if res.returncode == 0:
            print(f"  [Auto-Clone] Successfully cloned {url}.")
            return
        else:
            print(f"  \033[91m[Auto-Clone] Clone failed. Error: {res.stderr.strip()}\033[0m")
            print(f"  [Auto-Clone] Initializing empty repo instead.")
    else:
        print("  [Auto-Clone] No URL defined. Initializing empty repo.")

    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=dest, capture_output=True)

def _resume_feature(dest: Path, name: str, global_cfg: dict) -> None:
    if not (dest / ".git").exists():
        return
    approved_prefixes = global_cfg.get("branch_prefixes", {"feat": "", "fix": "", "docs": "", "chore": "", "refactor": "", "test": ""})
    if isinstance(approved_prefixes, list):
        approved_prefixes = {p: "" for p in approved_prefixes}

    res = subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=dest, capture_output=True, text=True)
    branches = [b.strip() for b in res.stdout.splitlines() if b.strip()]
    features = [b for b in branches if "/" in b and b.split("/")[0] in approved_prefixes]

    if not features:
        print(f"  [Features] No active feature branches to resume for '{name}'.")
        return
    try:
        idx = interactive_select(f"  [Features] Select a feature branch to resume for '{name}':", features)
        selected = features[idx]
        subprocess.run(["git", "checkout", selected], cwd=dest, capture_output=True)
        print(f"  ✓ Resumed work on \033[96m{selected}\033[0m.")
    except KeyboardInterrupt:
        print("\n  - Aborted feature selection.")

def _start_feature(dest: Path, name: str, item: dict, global_cfg: dict, feature_raw: str, assume_yes: bool = False) -> bool:
    if not (dest / ".git").exists(): return True
    status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True)
    if status.stdout.strip():
        print(f"  \033[93m! Warning: '{name}' has uncommitted changes.\033[0m")
        return False

    approved_prefixes = global_cfg.get("branch_prefixes", {"feat": "A new feature", "fix": "A bug fix"})
    if isinstance(approved_prefixes, list): approved_prefixes = {p: "" for p in approved_prefixes}

    if "/" in feature_raw:
        prefix, branch_name = feature_raw.split("/", 1)
        if prefix not in approved_prefixes:
            print(f"  \033[91m! Error: Branch prefix '{prefix}' is not approved.\033[0m")
            return False
        feature_name = f"{prefix}/{branch_name}"
    else:
        if assume_yes:
            feature_name = f"{next(iter(approved_prefixes.keys()))}/{feature_raw}"
        else:
            prefixes_list = list(approved_prefixes.keys())
            display_opts = [f"{k:<10} - {v}" for k, v in approved_prefixes.items()]
            try:
                idx = interactive_select(f"  > Select a prefix for branch '{feature_raw}':", display_opts)
                feature_name = f"{prefixes_list[idx]}/{feature_raw}"
            except KeyboardInterrupt:
                return False

    res_main = subprocess.run(["git", "branch", "--list", "main"], cwd=dest, capture_output=True, text=True)
    default_branch = "main" if "main" in res_main.stdout else "master"
    current_version = str(item.get("version", "0.0.1")).strip()
    parts = current_version.replace("v", "").split(".")
    next_version = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}" if len(parts) == 3 and parts[2].isdigit() else f"{current_version}-next"
    dev_branch = f"dev-v{next_version}"

    check_dev = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{dev_branch}"], cwd=dest)
    if check_dev.returncode != 0:
        subprocess.run(["git", "checkout", default_branch], cwd=dest, capture_output=True)
        subprocess.run(["git", "pull", "--rebase"], cwd=dest, capture_output=True)
        subprocess.run(["git", "checkout", "-b", dev_branch], cwd=dest, capture_output=True)
    else:
        subprocess.run(["git", "checkout", dev_branch], cwd=dest, capture_output=True)

    check_feat = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{feature_name}"], cwd=dest)
    if check_feat.returncode == 0:
        subprocess.run(["git", "checkout", feature_name], cwd=dest, capture_output=True)
    else:
        subprocess.run(["git", "checkout", "-b", feature_name], cwd=dest, capture_output=True)
    return True

def _update_yaml_version(tmpl_dir: Path, raw_token: str, new_version: str) -> None:
    if not tmpl_dir: return
    possible_paths = [
        tmpl_dir / "libraries" / f"{raw_token}.yaml", tmpl_dir / "libraries" / f"{raw_token}.yml",
        tmpl_dir / "apps" / f"{raw_token}.yaml", tmpl_dir / "apps" / f"{raw_token}.yml",
        ]
    target_file = next((p for p in possible_paths if p.exists()), None)
    if not target_file: return

    lines = target_file.read_text(encoding="utf-8").splitlines()
    updated = False
    for i, line in enumerate(lines):
        if line.startswith("version:"):
            lines[i] = f"version: {new_version}"
            updated = True
            break
    if updated:
        target_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

def _resolve_projects(reader: ConfigReader, projects: list[str]) -> list[tuple[str, str, str, dict]]:
    for p in projects:
        if p.startswith(("http://", "https://", "git@")):
            continue # Skip YAML loading for direct URLs
        if "/" in p:
            for pth in [f"libraries/{p}.yaml", f"apps/{p}.yaml"]:
                data = reader.tmpl_src._load_logical_path(pth)
                if data:
                    reader.cfg = deep_merge(reader.cfg, data)
                    break
        else:
            for f in reader.tmpl_src.find_registry_yamls(f"libraries/{p}"):
                data = reader.tmpl_src._load_logical_path(f)
                if data: reader.cfg = deep_merge(reader.cfg, data)
            for f in reader.tmpl_src.find_registry_yamls(f"apps/{p}"):
                data = reader.tmpl_src._load_logical_path(f)
                if data: reader.cfg = deep_merge(reader.cfg, data)

    idx = reader._build_library_index(reader.effective_config)
    by_name_lower = {v["name"].lower(): k for k, v in idx.items()}
    by_snake = {v["snake"]: k for k, v in idx.items()}

    def needs_scaffolding(item: dict) -> bool: return bool(item.get("template"))

    out: list[tuple[str, str, str, dict]] = []
    for p in projects:
        if p.startswith(("http://", "https://", "git@")):
            url = p
            clean_url = url.split("+", 1)[-1] if "+" in str(url) else str(url)
            clean_url = clean_url.split("@", 1)[0]
            name = clean_url.split("/")[-1].replace(".git", "")
            project_slug = slug(name)
            out.append((name, project_slug, name, {"url": url}))
            continue

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
        project_slug = slug(p)
        key = project_slug if project_slug in idx else by_snake.get(snake(p), by_name_lower.get(p.lower()))
        if key:
            out.append((idx[key]["name"], key, idx[key].get("raw_key", p), idx[key]["item"]))

    seen: set[str] = set()
    uniq: list[tuple[str, str, str, dict]] = []
    for name, project_slug, raw_token, item in out:
        if project_slug not in seen:
            seen.add(project_slug)
            uniq.append((name, project_slug, raw_token, item))
    return uniq

def create_project(project_slug: str, workspace_dir: Path, reader: ConfigReader, existing_cfg: dict) -> int:
    from jinja2 import Environment
    jenv = Environment()

    print(f"\n\033[1m=== Creating New Project: {project_slug} ===\033[0m\n")

    stacks = set()
    for rel, _, _, _ in reader.tmpl_src.iter_files():
        if rel.startswith("stacks/"):
            parts = rel.split("/")
            if len(parts) >= 2 and parts[1] and not parts[1].startswith("."):
                stacks.add(parts[1])
    stacks = sorted(list(stacks))

    if not stacks:
        print("❌ No stacks found in templates/stacks/.")
        return 1

    stack_idx = interactive_select("Select primary stack:", stacks)
    selected_stack = stacks[stack_idx]

    types = set()
    for rel, _, _, _ in reader.tmpl_src.iter_files():
        if rel.startswith(f"stacks/{selected_stack}/"):
            parts = rel.split("/")
            if len(parts) >= 3 and parts[2] != "base" and not parts[2].startswith("."):
                types.add(parts[2])
    types = sorted(list(types))

    selected_type = "base"
    if types:
        if len(types) == 1:
            selected_type = types[0]
            print(f"Select {selected_stack} environment: \033[96m{selected_type}\033[0m (Auto-selected)")
        else:
            type_idx = interactive_select(f"Select {selected_stack} environment:", types)
            selected_type = types[type_idx]

    ns_key = f"{selected_stack}_{selected_type}".lower()
    if ns_key not in existing_cfg:
        existing_cfg = append_stack_to_workspace(selected_stack, selected_type, workspace_dir, reader, existing_cfg)

    answers = {}
    data = reader.tmpl_src.get_stacked_defaults(f"stacks/{selected_stack}/{selected_type}/_")
    create_prompts = data.get("create_prompts", [])
    for p in create_prompts:
        var_name = p.get("var")
        prompt_str = p.get("prompt", f"Set {var_name}:")
        def_val = p.get("default", "")
        options = p.get("options", [])

        if options:
            if len(options) == 1 and "Custom" not in options[0]:
                answers[var_name] = options[0]
                print(f"{prompt_str} \033[96m{options[0]}\033[0m (Auto-selected)")
            else:
                start_idx = options.index(def_val) if def_val in options else 0
                ans_idx = interactive_select(prompt_str, options, default_idx=start_idx)
                ans = options[ans_idx]
                if "Custom" in ans:
                    answers[var_name] = input(f"  > Enter {var_name} [\033[92m{def_val}\033[0m]: ").strip() or def_val
                else:
                    answers[var_name] = ans
        else:
            answers[var_name] = input(f"{prompt_str} [\033[92m{def_val}\033[0m]: ").strip() or def_val

    profiles = set()
    for rel, _, _, _ in reader.tmpl_src.iter_files():
        if rel.startswith("profiles/") and rel.endswith(".yaml"):
            profiles.add(rel[len("profiles/"): -5])
    profiles = sorted(list(profiles))

    default_prof = existing_cfg.get("default_profile")
    selected_profile = default_prof

    if profiles:
        if len(profiles) == 1:
            selected_profile = profiles[0]
            print(f"Select project profile: \033[96m{selected_profile}\033[0m (Auto-selected)")
        else:
            start_idx = profiles.index(default_prof) if default_prof in profiles else 0
            prof_idx = interactive_select("Select project profile:", profiles, default_idx=start_idx)
            selected_profile = profiles[prof_idx]

    project_dir = workspace_dir / project_slug
    if project_dir.exists() and (project_dir / "scaffold.yaml").exists():
        print(f"⚠️  Project {project_slug} already exists.")
        return 1

    project_dir.mkdir(parents=True, exist_ok=True)
    scaffold_file = project_dir / "scaffold.yaml"
    subprocess.run(["git", "init"], cwd=project_dir, capture_output=True)

    current_date = date.today().isoformat()

    yaml_lines = [
        f"project_title: {project_slug}",
        f'version: "0.1.0"',
        f'description: "A dynamically scaffolded {selected_stack}/{selected_type} project"',
        f'stack: {selected_stack}/{selected_type}',
        f'date_created: {current_date}'
    ]

    if selected_profile:
        yaml_lines.append(f'# profile: {selected_profile}')

    yaml_lines.append("")

    registry_url = existing_cfg.get("template_registry_url")
    if registry_url:
        yaml_lines.extend([
            "# ── 1. Scaffolding Engine Source ──",
            "# The absolute source of truth for your organizational templates.",
            "base_templates:",
            f"  repo: {registry_url}",
            f"  ref: {existing_cfg.get('template_registry_ref', 'main')}",
            ""
        ])

    yaml_lines.append("")

    if answers:
        yaml_lines.append("# ── Dynamic Setup Variables ──")
        for k, v in answers.items():
            val_str = f'"{v}"' if isinstance(v, str) and not v.isdigit() else v
            yaml_lines.append(f"{k}: {val_str}")
        yaml_lines.append("")

    yaml_lines.extend([
        "# ── 3. Licenses & Legal (Uncomment to apply) ──",
        "# license_profile: apache-2.0",
        "# license_overrides:",
        "#   \"src/vendor/**\": mit",
        "",
        "# ── 4. Dependencies ──",
        "# NOTE: 'depends_on' is for internal dependencies and external Git/System libraries.",
        "# Standard packages (PyPI, npm, apt) go in their respective package manager files.",
        "depends_on: []",
        "#  - https://github.com/contactandyc/a-memory-library.git",
        "#  - system/CURL",
        "",
        "# ── 5. Sources (Auto-Discovered) ──",
        "# By default, the engine globs all source files in `src/` to build your library.",
        "#",
        "# IMPORTANT: You must run `scaffold-repo --update` whenever you add,",
        "# rename, or remove source files to regenerate the build scripts!",
        "#",
        "# Define explicitly ONLY if you need to exclude files or use a custom structure.",
        "# library_sources:",
        "#   - src/main.c",
        "#   - src/utils.c",
        "",
        "# ── 6. Tests (Auto-Discovered) ──",
        "# By default, the engine globs `tests/src/test_*.c` and creates an individual",
        "# standalone test executable for each file found.",
        "#",
        "# Define explicitly if you need to group multiple files into a single test binary:",
        "# tests:",
        "#   targets:",
        "#     - name: test_custom",
        "#       sources: ",
        "#         - tests/src/test_custom.c",
        "#         - tests/src/mock_data.c",
        "",
        "# ── 7. Apps & Examples (Optional) ──",
        "# Define standalone binaries, CLI tools, or examples here.",
        "# Notice how you can use wildcards (*) and shared contexts.",
        "# apps:",
        "#   context:",
        "#     dest: examples",
        "#     depends_on:",
        "#       - system/ZLIB",
        "#   01_basic_example:",
        "#     binaries:",
        "#       basic_app:",
        "#         - src/main.c",
        "#         - src/utils.c",
        "#   02_advanced_example:",
        "#     binaries:",
        "#       advanced_app:",
        "#         - src/*.c",
        "",
        "# ── 8. Feature Flags & Overrides ──",
        "# Toggle optional scaffolding mixins on or off.",
        "# packages:",
        "#   changie: true     # Generate Changelog automation",
        "#   site: false       # Disable Jekyll site generation",
        ""
    ])

    yaml_str = "\n".join(yaml_lines)

    scaffold_file.write_text(yaml_str, encoding="utf-8")
    print(f"✅ Initialized {project_slug}/scaffold.yaml")

    return 0

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scaffold-repo",
        description="The Declarative Fleet Manager: Scaffold repos, enforce OSS, orchestrate builds, and sync to Git."
    )

    ap.add_argument("--init", action="store_true", help="Initialize a .scaffoldrc workspace configuration")
    ap.add_argument("projects", nargs="*", help="One or more projects/namespaces to scaffold or build")

    grp_ws = ap.add_argument_group("Workspace Options")
    grp_ws.add_argument("-C", "--cwd", type=Path, default=Path("."), help="Run as if started in <PATH>")
    grp_ws.add_argument("--start-feature", nargs="?", const="", metavar="NAME", help="Start a feature branch")

    grp_sc = ap.add_argument_group("Scaffolding Options")
    grp_sc.add_argument("--create", metavar="SLUG", help="Create a new project in the workspace")
    grp_sc.add_argument("--update", action="store_true", help="Explicitly apply template updates to the repositories")
    grp_sc.add_argument("--diff", action="store_true", help="Print unpaginated Git diffs")
    grp_sc.add_argument("-y", "--assume-yes", action="store_true", help="Apply template updates without prompting")
    grp_sc.add_argument("--show-diffs", action="store_true", help="Print inline diffs before applying updates")

    grp_dp = ap.add_argument_group("Dependency Lifecycle")
    grp_dp.add_argument("--clone-deps", action="store_true", help="Fetch external dependencies")
    grp_dp.add_argument("--build-deps", action="store_true", help="Fetch, compile, and install dependencies")
    grp_dp.add_argument("--clean-deps", action="store_true", help="Wipe build caches for dependencies")

    grp_tl = ap.add_argument_group("Target Lifecycle")
    grp_tl.add_argument("--clean", action="store_true", help="Run './build.sh clean' on the target")
    grp_tl.add_argument("--build", action="store_true", help="Run './build.sh build'")
    grp_tl.add_argument("--install", action="store_true", help="Run './build.sh install'")

    grp_git = ap.add_argument_group("Git Orchestration")
    grp_git.add_argument("--commit", type=str, metavar="MSG", help="Commit changes")
    grp_git.add_argument("--publish-feature", action="store_true", help="Merge feature to dev")
    grp_git.add_argument("--publish-release", action="store_true", help="Merge dev to main and tag")
    grp_git.add_argument("--drop-feature", action="store_true", help="Discard feature branch")
    grp_git.add_argument("--push", action="store_true", help="Push commits")

    args = ap.parse_args(argv)
    root = args.cwd.resolve()

    if args.init:
        return init_scaffoldrc()

    rc = find_scaffoldrc(root)
    ws_str = rc.get("workspace_dir") or "../repos"
    workspace_dir = Path(ws_str).expanduser()
    if not workspace_dir.is_absolute():
        workspace_dir = (root / workspace_dir).resolve()

    reader = ConfigReader(
        root,
        project_name=None,
        base_templates_dir=None
    )
    reader.load()
    reader.effective_config["workspace_dir"] = str(workspace_dir)

    is_create_run = False
    if args.create:
        code = create_project(args.create, workspace_dir, reader, rc)
        if code != 0: return code

        project_tokens = [args.create]
        args.update = True
        is_create_run = True

        reader = ConfigReader(
            workspace_dir / args.create,
            project_name=None,
            base_templates_dir=None,
            is_init=True
        )
        reader.load()
        reader.effective_config["workspace_dir"] = str(workspace_dir)

    else:
        project_tokens = args.projects
        if not project_tokens and not args.diff:
            active_proj = _get_active_git_project(root)
            if active_proj:
                print(f"🎯 Auto-detected context: \033[96m{active_proj}\033[0m")
                project_tokens = [active_proj]

    results: list[dict[str, Any]] = []
    exit_code = 0

    try:
        aliases = {}
        alias_text = reader.tmpl_src.read_resource_text("resources/aliases.yaml")
        if alias_text:
            try:
                alias_data = yaml.safe_load(alias_text) or {}
                if isinstance(alias_data, dict):
                    aliases.update(alias_data)
            except Exception as e:
                print(f"Warning: Failed to parse aliases file: {e}", file=sys.stderr)

        expanded_tokens = []
        for p in project_tokens:
            if p in aliases:
                val = aliases[p]
                if isinstance(val, (list, tuple)):
                    expanded_tokens.extend(str(x) for x in val)
                else:
                    expanded_tokens.append(str(val))
            else:
                expanded_tokens.append(p)

        if is_create_run:
            targets = [(args.create, slug(args.create), args.create, {})]
        else:
            targets = _resolve_projects(reader, expanded_tokens) if expanded_tokens else []

        if project_tokens and not targets:
            print(f"\n❌ Error: Could not resolve any valid projects from: {project_tokens}")
            print(f"   Check your spelling or ensure your templates/resources/aliases.yaml is defined correctly.")
            return 1

        if args.diff:
            print("\n\033[1m=== Fleet Git Diffs ===\033[0m")
            if not targets: targets = [("Root Repo", "root", None, {})]
            for name, project_slug, raw_token, item in targets:
                dest = root if not raw_token else workspace_dir / slug(posixpath.basename(raw_token))
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

        skip_templates = True
        if args.update or args.assume_yes or args.show_diffs:
            skip_templates = False

        if targets:
            for name, project_slug, raw_token, item in targets:
                dest = workspace_dir / slug(posixpath.basename(raw_token))

                if args.start_feature == "":
                    _resume_feature(dest, name, reader.effective_config)
                    continue

                print(f"\n\033[95m=== Initializing {name} ({project_slug}) into {dest} ===\033[0m")
                dest = dest.resolve()

                _auto_clone_target(dest, item, reader.effective_config, skip_sync=skip_templates)

                if args.update and (dest / ".git").exists():
                    res = subprocess.run(["git", "branch", "--show-current"], cwd=dest, capture_output=True, text=True)
                    current_branch = res.stdout.strip()
                    res_main = subprocess.run(["git", "branch", "--list", "main"], cwd=dest, capture_output=True, text=True)
                    default_branch = "main" if "main" in res_main.stdout else "master"

                    scaffold_branch = "chore/update-scaffolding"

                    if current_branch == default_branch:
                        current_version = str(item.get("version", "0.0.1")).strip()
                        parts = current_version.replace("v", "").split(".")
                        guess_version = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}" if len(parts) == 3 and parts[2].isdigit() else f"{current_version}-next"
                        target_dev = f"dev-v{guess_version}"

                        print(f"  - On '{default_branch}'. Establishing integration branch '{target_dev}'...")

                        check_dev = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{target_dev}"], cwd=dest)
                        if check_dev.returncode != 0:
                            subprocess.run(["git", "branch", target_dev, default_branch], cwd=dest, capture_output=True)

                        subprocess.run(["git", "checkout", target_dev], cwd=dest, capture_output=True)

                        print(f"  - Moving to '{scaffold_branch}' from '{target_dev}'.")
                        check_feat = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{scaffold_branch}"], cwd=dest)
                        if check_feat.returncode == 0:
                            subprocess.run(["git", "checkout", scaffold_branch], cwd=dest, capture_output=True)
                        else:
                            subprocess.run(["git", "checkout", "-b", scaffold_branch], cwd=dest, capture_output=True)

                    elif current_branch.startswith("dev-"):
                        print(f"  - Protected integration branch '{current_branch}' detected. Moving to '{scaffold_branch}'.")
                        check_feat = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{scaffold_branch}"], cwd=dest)
                        if check_feat.returncode == 0:
                            subprocess.run(["git", "checkout", scaffold_branch], cwd=dest, capture_output=True)
                        else:
                            subprocess.run(["git", "checkout", "-b", scaffold_branch], cwd=dest, capture_output=True)

                    elif current_branch == scaffold_branch:
                        print(f"  - Already on '{scaffold_branch}'. Applying updates.")

                    else:
                        print(f"  - Already on working branch '{current_branch}'. Applying updates directly.")

                if args.start_feature is not None:
                    if not _start_feature(dest, name, item, reader.effective_config, args.start_feature, args.assume_yes):
                        print("  \033[91m! Skipping scaffolding for this repo.\033[0m")
                        exit_code = max(exit_code, 1)
                        continue

        if not skip_templates or args.clone_deps or args.build_deps or args.clean_deps:
            target_dirs = [workspace_dir / slug(posixpath.basename(t[2])) for t in targets] if targets else [root]
            for t_dir in target_dirs:
                if t_dir.exists():
                    build_all_libs(
                        repo=t_dir, workspace_dir=workspace_dir, project_tokens=[],
                        base_templates_dir=None,
                        do_clone=True, do_build=False, do_install=False, do_clean=False
                    )

        if targets:
            for name, project_slug, raw_token, item in targets:
                dest = workspace_dir / slug(posixpath.basename(raw_token))
                if skip_templates or not dest.exists():
                    print(f"  - Bypassing template verification for {name} (Read-only / Git operation).")
                    continue
                code, res = verify_repo(
                    dest, fix_licenses=True, no_prompt=args.assume_yes, project_name=name,
                    base_templates_dir=None,
                    assume_yes=True if is_create_run else args.assume_yes,
                    show_diffs=args.show_diffs,
                    is_init=is_create_run
                )
                exit_code = max(exit_code, code)
                results.append(res)
        elif not project_tokens:
            print(f"\n\033[95m=== Scaffolding in-place ({root}) ===\033[0m")
            if not skip_templates:
                code, res = verify_repo(
                    root, fix_licenses=True, no_prompt=args.assume_yes, project_name=None,
                    base_templates_dir=None,
                    assume_yes=True if is_create_run else args.assume_yes,
                    show_diffs=args.show_diffs,
                    is_init=is_create_run
                )
                exit_code = max(exit_code, code)
                results.append(res)

        if args.build_deps or args.clean_deps:
            for t_dir in target_dirs:
                if t_dir.exists():
                    build_all_libs(
                        repo=t_dir, workspace_dir=workspace_dir, project_tokens=[],
                        base_templates_dir=None,
                        do_clone=False,
                        do_build=args.build_deps,
                        do_install=args.build_deps,
                        do_clean=args.clean_deps
                    )

        if targets and (args.build or args.install or args.clean):
            print("\n\033[1m=== Phase 5: First-Party Build Orchestration ===\033[0m")
            from ..build_libs import execute_build

            for name, project_slug, raw_token, item in targets:
                dest = workspace_dir / slug(posixpath.basename(raw_token))
                if not dest.exists(): continue

                print(f"\n🚀 Orchestrating execution for {name}...")
                execute_build(
                    project_slug, dest, item, workspace_dir,
                    do_build=args.build,
                    do_install=args.install,
                    do_clean=args.clean
                )

        if targets and (args.commit or args.publish_feature or args.publish_release or args.drop_feature or args.push):
            print("\n\033[1m=== Phase 6: Git Orchestration ===\033[0m")

            if args.push or args.publish_feature or args.publish_release:
                _warn_if_local_templates_unpushed(reader)

            for name, project_slug, raw_token, item in targets:
                dest = workspace_dir / slug(posixpath.basename(raw_token))
                if not (dest / ".git").exists(): continue

                print(f"\n📦 Syncing {name} to Git...")

                res = subprocess.run(["git", "branch", "--show-current"], cwd=dest, capture_output=True, text=True)
                current_branch = res.stdout.strip()
                res_main = subprocess.run(["git", "branch", "--list", "main"], cwd=dest, capture_output=True, text=True)
                default_branch = "main" if "main" in res_main.stdout else "master"

                status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True)
                is_dirty = bool(status.stdout.strip())

                if (args.publish_feature or args.commit) and current_branch == "chore/update-scaffolding" and is_dirty:
                    print(f"  [Auto-Commit] Committing scaffolding updates on '{current_branch}' before publishing...")
                    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
                    subprocess.run(["git", "commit", "-m", args.commit if args.commit else "chore: apply scaffolding updates"], cwd=dest, check=True)
                    is_dirty = False

                if args.commit and current_branch != "chore/update-scaffolding":
                    if current_branch == default_branch or current_branch.startswith("dev-"):
                        print(f"  ! Cannot commit directly to '{current_branch}'. Start a feature branch first.")
                    elif is_dirty:
                        subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
                        subprocess.run(["git", "commit", "-m", args.commit], cwd=dest, check=True)
                        print(f"  ✓ Committed changes to '{current_branch}': '{args.commit}'")
                        is_dirty = False
                    else:
                        print(f"  - No changes to commit on '{current_branch}'.")

                if args.push and not (args.publish_release or args.publish_feature or args.drop_feature):
                    if current_branch == default_branch or current_branch.startswith("dev-"):
                        print(f"  ! Cannot push directly to '{current_branch}'. Use a feature branch or publish commands.")
                    else:
                        print(f"  - Pushing branch '{current_branch}' to origin...")
                        res = subprocess.run(["git", "push", "-u", "origin", current_branch], cwd=dest, capture_output=True, text=True)
                        if res.returncode == 0: print(f"  ✓ Pushed '{current_branch}'.")
                        else: print(f"  ! Failed to push: {res.stderr.strip()}")

                if args.publish_feature:
                    if current_branch == default_branch or current_branch.startswith("dev-"): continue
                    if is_dirty:
                        print(f"  \033[91m❌ Blocked: Cannot publish '{name}' because it has uncommitted changes on '{current_branch}'.\033[0m")
                        continue

                    res_all = subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=dest, capture_output=True, text=True)
                    all_branches = [b.strip() for b in res_all.stdout.splitlines() if b.strip()]

                    dev_branches = sorted([b for b in all_branches if b.startswith("dev-")], reverse=True)

                    target_dev = None
                    if dev_branches:
                        min_distance = float('inf')
                        for cand in dev_branches:
                            mb_res = subprocess.run(["git", "merge-base", current_branch, cand], cwd=dest, capture_output=True, text=True)
                            if mb_res.returncode == 0:
                                mb = mb_res.stdout.strip()
                                dist_res = subprocess.run(["git", "rev-list", "--count", f"{mb}..{current_branch}"], cwd=dest, capture_output=True, text=True)
                                if dist_res.returncode == 0:
                                    dist = int(dist_res.stdout.strip())
                                    if dist < min_distance:
                                        min_distance = dist
                                        target_dev = cand

                    if not target_dev:
                        current_version = str(item.get("version", "0.0.1")).strip()
                        parts = current_version.replace("v", "").split(".")
                        guess_version = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}" if len(parts) == 3 and parts[2].isdigit() else f"{current_version}-next"
                        target_dev = f"dev-v{guess_version}"

                    if not args.assume_yes:
                        ans = input(f"  > Merge '{current_branch}' into '{target_dev}'? [Y/n]: ").strip().lower()
                        if ans in ("n", "no"):
                            target_dev = input(f"  > Enter target branch manually: ").strip()

                    if target_dev not in all_branches:
                        print(f"  - Creating required integration branch '{target_dev}' from '{default_branch}'...")
                        subprocess.run(["git", "branch", target_dev, default_branch], cwd=dest, capture_output=True)

                    subprocess.run(["git", "checkout", target_dev], cwd=dest, capture_output=True)
                    subprocess.run(["git", "pull", "--rebase", "origin", target_dev], cwd=dest, capture_output=True)

                    ahead_check = subprocess.run(["git", "rev-list", "--count", f"{target_dev}..{current_branch}"], cwd=dest, capture_output=True, text=True)
                    ahead_count = int(ahead_check.stdout.strip()) if ahead_check.returncode == 0 and ahead_check.stdout.strip().isdigit() else 0

                    if ahead_count == 0:
                        print(f"  - No new changes in '{current_branch}'. Dropping branch.")
                        subprocess.run(["git", "branch", "-d", current_branch], cwd=dest, capture_output=True)
                        if args.push:
                            subprocess.run(["git", "push", "origin", "--delete", current_branch], cwd=dest, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        continue

                    print(f"  - Merging '{current_branch}' into '{target_dev}'...")
                    if subprocess.run(["git", "merge", current_branch, "--no-ff", "-m", f"Merge feature '{current_branch}'"], cwd=dest, capture_output=True, text=True).returncode != 0:
                        subprocess.run(["git", "merge", "--abort"], cwd=dest, capture_output=True)
                        print(f"  \033[91m! Merge conflict. Aborting.\033[0m")
                        continue

                    subprocess.run(["git", "branch", "-d", current_branch], cwd=dest, capture_output=True)

                    if args.push:
                        print(f"  - Pushing branch '{target_dev}' to origin...")
                        subprocess.run(["git", "push", "-u", "origin", target_dev], cwd=dest, capture_output=True)
                        subprocess.run(["git", "push", "origin", "--delete", current_branch], cwd=dest, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                if args.publish_release:
                    if is_dirty:
                        print(f"  \033[91m❌ Blocked: Cannot publish release for '{name}' because it has uncommitted changes.\033[0m")
                        continue

                    res_all = subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=dest, capture_output=True, text=True)
                    all_branches = [b.strip() for b in res_all.stdout.splitlines() if b.strip()]

                    if current_branch.startswith("dev-"):
                        release_branch = current_branch
                    else:
                        dev_branches = sorted([b for b in all_branches if b.startswith("dev-")], reverse=True)
                        if not dev_branches:
                            print(f"  \033[93m! No integration branches found for '{name}'. Skipping.\033[0m")
                            continue
                        release_branch = dev_branches[0]

                    if not args.assume_yes:
                        ans = input(f"  > Merge '{release_branch}' into '{default_branch}' and tag release? [Y/n]: ").strip().lower()
                        if ans in ("n", "no"): continue

                    tag_name = release_branch[4:] if release_branch.startswith("dev-") else f"v{release_branch}"

                    subprocess.run(["git", "checkout", default_branch], cwd=dest, capture_output=True)
                    subprocess.run(["git", "pull", "--rebase", "origin", default_branch], cwd=dest, capture_output=True)

                    ahead_check = subprocess.run(["git", "rev-list", "--count", f"{default_branch}..{release_branch}"], cwd=dest, capture_output=True, text=True)
                    ahead_count = int(ahead_check.stdout.strip()) if ahead_check.returncode == 0 and ahead_check.stdout.strip().isdigit() else 0

                    if ahead_count == 0:
                        print(f"  - No new changes in '{release_branch}' to release. Skipping.")
                        continue

                    print(f"  - Releasing '{release_branch}' to '{default_branch}' as '{tag_name}'...")
                    if subprocess.run(["git", "merge", release_branch, "--no-ff", "-m", f"Release {tag_name}"], cwd=dest, capture_output=True, text=True).returncode != 0:
                        subprocess.run(["git", "merge", "--abort"], cwd=dest, capture_output=True)
                        print(f"  \033[91m! Merge conflict. Aborting.\033[0m")
                        continue

                    check_tag = subprocess.run(["git", "tag", "-l", tag_name], cwd=dest, capture_output=True, text=True)
                    if tag_name not in check_tag.stdout.split():
                        subprocess.run(["git", "tag", "-a", tag_name, "-m", f"Release {tag_name}"], cwd=dest, capture_output=True, text=True)

                    if reader.tmpl_src and reader.tmpl_src._pkg_root:
                        new_yaml_version = tag_name[1:] if tag_name.startswith("v") else tag_name
                        _update_yaml_version(reader.tmpl_src._pkg_root, raw_token, new_yaml_version)

                    if args.push:
                        print(f"  - Pushing branch '{default_branch}' and tag '{tag_name}' to origin...")
                        subprocess.run(["git", "push", "origin", default_branch], cwd=dest, capture_output=True, text=True)
                        subprocess.run(["git", "push", "origin", tag_name], cwd=dest, capture_output=True, text=True)

                if args.drop_feature:
                    if current_branch == default_branch or current_branch.startswith("dev-"): continue

                    res_all = subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=dest, capture_output=True, text=True)
                    all_branches = [b.strip() for b in res_all.stdout.splitlines() if b.strip()]
                    dev_branches = sorted([b for b in all_branches if b.startswith("dev-")], reverse=True)
                    candidates = dev_branches + [default_branch]

                    target_dev = default_branch
                    min_distance = float('inf')
                    for cand in candidates:
                        mb_res = subprocess.run(["git", "merge-base", current_branch, cand], cwd=dest, capture_output=True, text=True)
                        if mb_res.returncode == 0:
                            mb = mb_res.stdout.strip()
                            dist_res = subprocess.run(["git", "rev-list", "--count", f"{mb}..{current_branch}"], cwd=dest, capture_output=True, text=True)
                            if dist_res.returncode == 0:
                                dist = int(dist_res.stdout.strip())
                                if dist < min_distance:
                                    min_distance = dist
                                    target_dev = cand

                    if not args.assume_yes:
                        ans_confirm = input(f"  > \033[91mWARNING: This will permanently delete '{current_branch}' and return to '{target_dev}'. Proceed? [y/N]:\033[0m ").strip().lower()
                        if ans_confirm not in ("y", "yes"): continue

                    subprocess.run(["git", "reset", "--hard"], cwd=dest, capture_output=True)
                    subprocess.run(["git", "checkout", target_dev], cwd=dest, capture_output=True)
                    subprocess.run(["git", "branch", "-D", current_branch], cwd=dest, capture_output=True)
                    subprocess.run(["git", "push", "origin", "--delete", current_branch], cwd=dest, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    except subprocess.CalledProcessError as e:
        print(f"\n❌ Command Failed (exit code {e.returncode})", file=sys.stderr)
        return e.returncode
    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        return 1

    if not (args.diff or args.start_feature == "" or skip_templates):
        for res in results:
            print_text_result(res)

    return exit_code

if __name__ == "__main__":
    sys.exit(main())
