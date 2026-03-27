# src/scaffold_repo/git/orchestrator.py
import subprocess
import sys
from pathlib import Path
from ..cli.ui import interactive_select
from ..utils.git import ensure_clone

class GitFleetManager:
    def __init__(self, workspace_dir: Path, global_cfg: dict):
        self.workspace_dir = workspace_dir
        self.global_cfg = global_cfg

        approved = self.global_cfg.get("branch_prefixes", {"feat": "A new feature", "fix": "A bug fix"})
        if isinstance(approved, list):
            self.approved_prefixes = {p: "" for p in approved}
        else:
            self.approved_prefixes = approved

    # ==========================================
    # 1. TRANSPORT LAYER
    # ==========================================

    def clone(self, dest: Path, item: dict, skip_if_exists: bool = True) -> bool:
        if dest.exists() and (dest / ".git").exists():
            if skip_if_exists:
                print(f"  - {dest.name} already exists. Skipping clone.")
                return True
            return self.pull(dest, dest.name)

        url = item.get("url")
        branch = item.get("branch")
        shallow = item.get("shallow", False)

        if not url:
            gh_proj = self.global_cfg.get("github_project")
            name = item.get("name") or dest.name
            if gh_proj:
                url = f"https://github.com/{gh_proj}/{name}.git"

        if url:
            print(f"  - Cloning {dest.name} from {url}...")
            dest.parent.mkdir(parents=True, exist_ok=True)
            ensure_clone(url, dest, branch=branch, shallow=shallow)
            return True
        else:
            print(f"  ! Cannot clone {dest.name}: No URL defined in registry.")
            return False

    def pull(self, dest: Path, name: str) -> bool:
        if not (dest / ".git").exists():
            print(f"  ! {name} is not a git repository.")
            return False

        print(f"  - Pulling latest changes for {name}...")
        res = subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=dest, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"  \033[91m! Warning: Could not pull latest for {name}.\033[0m\n    {res.stderr.strip()}")
            return False
        return True

    def clone_dependencies(self, dest: Path, item: dict, reader) -> None:
        if not dest.exists():
            print(f"\n📦 Target '{dest.name}' is missing. Cloning it to read its dependencies...")
            if not self.clone(dest, item, skip_if_exists=True):
                return

        import yaml
        from ..utils.collections import coerce_list
        from ..core.config import _extract_dep_name
        from ..utils.text import slug, snake
        from ..templating.source import _fetch_remote_yaml

        global_registry = reader._build_library_index(reader.effective_config)
        by_snake = {v["snake"]: k for k, v in global_registry.items()}

        visited_slugs = set()
        to_clone = {}

        def _discover(item_name: str, current_item: dict):
            item_slug = slug(item_name)
            if item_slug in visited_slugs:
                return
            visited_slugs.add(item_slug)

            dep_dest = self.workspace_dir / item_name
            manifest_data = {}

            if (dep_dest / "scaffold.yaml").exists():
                try:
                    manifest_data = yaml.safe_load((dep_dest / "scaffold.yaml").read_text(encoding="utf-8")) or {}
                except Exception:
                    pass
            else:
                to_clone[item_slug] = (dep_dest, current_item)
                url = current_item.get("url")
                branch = current_item.get("branch")
                if url:
                    print(f"  🔍 Peeking at remote manifest for {item_name}...")
                    manifest_data = _fetch_remote_yaml(url, ref=branch)

            raw_deps = list(coerce_list(manifest_data.get("depends_on", [])))
            apps = manifest_data.get("apps", {})
            if isinstance(apps, dict):
                app_ctx = apps.get("context", {})
                if isinstance(app_ctx, dict):
                    raw_deps.extend(coerce_list(app_ctx.get("depends_on", [])))
                for k, app in apps.items():
                    if k == "context": continue
                    if isinstance(app, dict):
                        raw_deps.extend(coerce_list(app.get("depends_on", [])))
                        for b in coerce_list(app.get("binaries", [])):
                            if isinstance(b, dict):
                                raw_deps.extend(coerce_list(b.get("depends_on", [])))

            for raw in raw_deps:
                dep_item = None

                if isinstance(raw, str) and not raw.startswith(("http://", "https://", "git@")):
                    dep_name = _extract_dep_name(raw)
                    dep_slug = slug(dep_name)
                    if dep_slug not in global_registry:
                        sn = snake(dep_name)
                        if sn in by_snake: dep_slug = by_snake[sn]

                    if dep_slug in global_registry:
                        dep_item = global_registry[dep_slug]["item"]
                        if str(dep_item.get("kind", "local")).lower() in ("system", "apt"): continue
                    else: continue

                elif isinstance(raw, dict):
                    url = raw.get("url") or raw.get("source")
                    if url and url.startswith(("http://", "https://", "git@")): dep_item = raw

                elif isinstance(raw, str) and raw.startswith(("http://", "https://", "git@")):
                    dep_item = {"url": raw}

                if dep_item:
                    url = dep_item.get("url")
                    nm = dep_item.get("name") or _extract_dep_name(raw)
                    if not url:
                        gh_proj = self.global_cfg.get("github_project")
                        if gh_proj:
                            url = f"https://github.com/{gh_proj}/{nm}.git"
                            dep_item["url"] = url

                    if url:
                        resolved_name = _extract_dep_name(url)
                        _discover(resolved_name, dep_item)

        root_name = item.get("name") or dest.name
        print(f"\n🕸️  Resolving dependency graph for {root_name}...")
        _discover(root_name, item)

        if to_clone:
            print(f"\n📦 Graph resolved. Cloning {len(to_clone)} missing dependencies...")
            for s, (dep_dest, dep_item) in to_clone.items():
                self.clone(dep_dest, dep_item, skip_if_exists=True)
        else:
            print(f"✅ All dependencies for {root_name} are already cloned.")


    # ==========================================
    # 2. CONTEXT-BOUNDED VERSIONING
    # ==========================================
    def _calculate_context_bounded_version(self, dest: Path, current_version: str, max_bytes: int = 20000) -> str:
        """
        Bumps the version based on the raw byte size of the Git diff.
        Safely handles remote tracking, missing tags, and shallow clones.
        """
        parts = current_version.replace("v", "").split(".")
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit() or not parts[2].isdigit():
            # Just return the original string since bumping will not work for this!
            return current_version

        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])

        # 1. Hydrate the tags from the remote origin to fix shallow/single-branch clones
        subprocess.run(["git", "fetch", "--tags", "origin"], cwd=dest, capture_output=True)

        # 2. Find the most recent tag
        tag_res = subprocess.run(["git", "describe", "--tags", "--abbrev=0"], cwd=dest, capture_output=True, text=True)
        last_tag = tag_res.stdout.strip()

        bytes_changed = 0
        if tag_res.returncode == 0 and last_tag:
            # 3. Capture the actual raw diff as bytes
            diff_res = subprocess.run(["git", "diff", f"{last_tag}..HEAD"], cwd=dest, capture_output=True)

            if diff_res.returncode == 0:
                bytes_changed = len(diff_res.stdout)
            else:
                print(f"  [Versioning] Warning: Repo is shallow or missing history. Falling back to standard PATCH bump.")
                return f"{major}.{minor}.{patch + 1}"
        else:
            print(f"  [Versioning] No previous tags found in repository. Bumping PATCH.")
            return f"{major}.{minor}.{patch + 1}"

        # 4. Apply the threshold logic
        if bytes_changed >= max_bytes:
            print(f"  [Versioning] {bytes_changed:,} bytes changed since '{last_tag}'. Exceeds {max_bytes} limit. Bumping MINOR.")
            return f"{major}.{minor + 1}.0"
        else:
            print(f"  [Versioning] {bytes_changed:,} bytes changed since '{last_tag}'. Bumping PATCH.")
            return f"{major}.{minor}.{patch + 1}"


    # ==========================================
    # 3. AUTHORING LAYER
    # ==========================================

    def status_report(self, dest: Path, name: str, raw_token: str):
        display_label = raw_token if raw_token else name
        if (dest / ".git").exists():
            status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True)
            if status.stdout.strip():
                print(f"\n\033[93mREPO-CHANGED:\033[0m {display_label}")
                subprocess.run(["git", "add", "--intent-to-add", "."], cwd=dest, capture_output=True)
                subprocess.run(["git", "--no-pager", "diff"], cwd=dest)
            else:
                print(f"\033[92mREPO-UNCHANGED:\033[0m {display_label}")
        else:
            print(f"\033[90mREPO-MISSING:\033[0m {display_label} (Not cloned/No .git directory)")

    def start_feature(self, dest: Path, name: str, item: dict, feature_raw: str, assume_yes: bool = False) -> bool:
        if not (dest / ".git").exists():
            print(f"  ! {name} is not a git repository.")
            return False

        status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True)
        if status.stdout.strip():
            print(f"  \033[93m! Warning: '{name}' has uncommitted changes.\033[0m")
            return False

        # 1. Resolve prefix and branch name
        if "/" in feature_raw:
            prefix, branch_name = feature_raw.split("/", 1)
            if prefix not in self.approved_prefixes:
                print(f"  \033[91m! Error: Branch prefix '{prefix}' is not approved.\033[0m")
                return False
            feature_name = f"{prefix}/{branch_name}"
        else:
            if assume_yes:
                feature_name = f"{next(iter(self.approved_prefixes.keys()))}/{feature_raw}"
            else:
                prefixes_list = list(self.approved_prefixes.keys())
                display_opts = [f"{k:<10} - {v}" for k, v in self.approved_prefixes.items()]
                try:
                    idx = interactive_select(f"  > Select a prefix for branch '{feature_raw}':", display_opts)
                    feature_name = f"{prefixes_list[idx]}/{feature_raw}"
                except KeyboardInterrupt:
                    return False

        # 2. Determine target version via Byte-Bounded context
        res_main = subprocess.run(["git", "branch", "--list", "main"], cwd=dest, capture_output=True, text=True)
        default_branch = "main" if "main" in res_main.stdout else "master"

        current_version = str(item.get("version", "0.0.1")).strip()
        next_version = self._calculate_context_bounded_version(dest, current_version, max_bytes=20000)
        dev_branch = f"dev-v{next_version}"

        # 3. Setup Integration Branch (Auto-advance from main)
        subprocess.run(["git", "fetch", "origin", default_branch], cwd=dest, capture_output=True)

        check_dev = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{dev_branch}"], cwd=dest)
        if check_dev.returncode != 0:
            print(f"  - Creating integration branch '{dev_branch}' from '{default_branch}'...")
            subprocess.run(["git", "checkout", default_branch], cwd=dest, capture_output=True)
            subprocess.run(["git", "pull", "--rebase", "origin", default_branch], cwd=dest, capture_output=True)
            subprocess.run(["git", "checkout", "-b", dev_branch], cwd=dest, capture_output=True)
        else:
            print(f"  - Syncing integration branch '{dev_branch}' with latest '{default_branch}'...")
            subprocess.run(["git", "checkout", dev_branch], cwd=dest, capture_output=True)
            subprocess.run(["git", "pull", "--rebase", "origin", dev_branch], cwd=dest, capture_output=True)
            # Rebase on top of the latest main to catch hotfixes
            subprocess.run(["git", "rebase", f"origin/{default_branch}"], cwd=dest, capture_output=True)

        # 4. Checkout Feature Branch
        check_feat = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{feature_name}"], cwd=dest)
        if check_feat.returncode == 0:
            subprocess.run(["git", "checkout", feature_name], cwd=dest, capture_output=True)
            print(f"  ✓ Resumed existing feature branch: {feature_name}")
        else:
            subprocess.run(["git", "checkout", "-b", feature_name], cwd=dest, capture_output=True)
            print(f"  ✓ Created new feature branch: {feature_name}")

        # 5. Auto-bump version in scaffold.yaml
        manifest_path = dest / "scaffold.yaml"

        target_version = next_version

        if manifest_path.exists():
            lines = manifest_path.read_text(encoding="utf-8").splitlines()
            updated = False
            for i, line in enumerate(lines):
                if line.startswith("version:"):
                    current_val = line.split(":", 1)[1].strip().strip('"\'')
                    if current_val != target_version:
                        lines[i] = f'version: "{target_version}"' if '"' in line else f"version: {target_version}"
                        updated = True
                    break

            if updated:
                manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                print(f"  - Bumped local scaffold.yaml version to '{target_version}'")
                subprocess.run(["git", "add", "scaffold.yaml"], cwd=dest, capture_output=True)
                subprocess.run(["git", "commit", "-m", f"chore: bump version to {target_version}"], cwd=dest, capture_output=True)

        return True

    def commit(self, dest: Path, name: str, message: str) -> bool:
        res = subprocess.run(["git", "branch", "--show-current"], cwd=dest, capture_output=True, text=True)
        current_branch = res.stdout.strip()
        res_main = subprocess.run(["git", "branch", "--list", "main"], cwd=dest, capture_output=True, text=True)
        default_branch = "main" if "main" in res_main.stdout else "master"

        status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True)
        is_dirty = bool(status.stdout.strip())

        if current_branch == default_branch or current_branch.startswith("dev-"):
            print(f"  ! Cannot commit directly to '{current_branch}'. Start a feature branch first.")
            return False

        if is_dirty:
            subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
            subprocess.run(["git", "commit", "-m", message], cwd=dest, check=True)
            print(f"  ✓ Committed changes to '{current_branch}': '{message}'")
            return True

        print(f"  - No changes to commit on '{current_branch}'.")
        return False

    def push(self, dest: Path, name: str) -> bool:
        res = subprocess.run(["git", "branch", "--show-current"], cwd=dest, capture_output=True, text=True)
        current_branch = res.stdout.strip()
        res_main = subprocess.run(["git", "branch", "--list", "main"], cwd=dest, capture_output=True, text=True)
        default_branch = "main" if "main" in res_main.stdout else "master"

        if current_branch == default_branch or current_branch.startswith("dev-"):
            print(f"  ! Cannot push directly to '{current_branch}'. Use a feature branch or publish commands.")
            return False

        print(f"  - Pushing branch '{current_branch}' to origin...")
        res = subprocess.run(["git", "push", "-u", "origin", current_branch], cwd=dest, capture_output=True, text=True)
        if res.returncode == 0:
            print(f"  ✓ Pushed '{current_branch}'.")
            return True
        else:
            print(f"  ! Failed to push: {res.stderr.strip()}")
            return False

    def publish_feature(self, dest: Path, name: str, push: bool = False) -> bool:
        res = subprocess.run(["git", "branch", "--show-current"], cwd=dest, capture_output=True, text=True)
        current_branch = res.stdout.strip()
        res_main = subprocess.run(["git", "branch", "--list", "main"], cwd=dest, capture_output=True, text=True)
        default_branch = "main" if "main" in res_main.stdout else "master"

        status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True)
        if status.stdout.strip():
            print(f"  \033[91m❌ Blocked: Cannot publish '{name}' because it has uncommitted changes on '{current_branch}'.\033[0m")
            return False

        if current_branch == default_branch or current_branch.startswith("dev-"):
            print(f"  - Already on integration branch '{current_branch}'. Skipping.")
            return False

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
            print(f"  ! Could not find a suitable 'dev-*' branch to merge into.")
            return False

        subprocess.run(["git", "checkout", target_dev], cwd=dest, capture_output=True)
        subprocess.run(["git", "pull", "--rebase", "origin", target_dev], cwd=dest, capture_output=True)

        ahead_check = subprocess.run(["git", "rev-list", "--count", f"{target_dev}..{current_branch}"], cwd=dest, capture_output=True, text=True)
        ahead_count = int(ahead_check.stdout.strip()) if ahead_check.returncode == 0 and ahead_check.stdout.strip().isdigit() else 0

        if ahead_count == 0:
            print(f"  - No new changes in '{current_branch}'. Dropping branch.")
            subprocess.run(["git", "branch", "-d", current_branch], cwd=dest, capture_output=True)
            if push:
                subprocess.run(["git", "push", "origin", "--delete", current_branch], cwd=dest, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True

        print(f"  - Merging '{current_branch}' into '{target_dev}'...")
        if subprocess.run(["git", "merge", current_branch, "--no-ff", "-m", f"Merge feature '{current_branch}'"], cwd=dest, capture_output=True, text=True).returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=dest, capture_output=True)
            print(f"  \033[91m! Merge conflict. Aborting.\033[0m")
            return False

        subprocess.run(["git", "branch", "-d", current_branch], cwd=dest, capture_output=True)

        if push:
            print(f"  - Pushing branch '{target_dev}' to origin...")
            subprocess.run(["git", "push", "-u", "origin", target_dev], cwd=dest, capture_output=True)
            subprocess.run(["git", "push", "origin", "--delete", current_branch], cwd=dest, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        return True

    def publish_release(self, dest: Path, name: str, item: dict, tmpl_src_root: Path | None, raw_token: str, push: bool = False, assume_yes: bool = False) -> bool:
        res = subprocess.run(["git", "branch", "--show-current"], cwd=dest, capture_output=True, text=True)
        current_branch = res.stdout.strip()
        res_main = subprocess.run(["git", "branch", "--list", "main"], cwd=dest, capture_output=True, text=True)
        default_branch = "main" if "main" in res_main.stdout else "master"

        status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True)
        if status.stdout.strip():
            print(f"  \033[91m❌ Blocked: Cannot publish release for '{name}' because it has uncommitted changes.\033[0m")
            return False

        res_all = subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=dest, capture_output=True, text=True)
        all_branches = [b.strip() for b in res_all.stdout.splitlines() if b.strip()]

        if current_branch.startswith("dev-"):
            release_branch = current_branch
        else:
            dev_branches = sorted([b for b in all_branches if b.startswith("dev-")], reverse=True)
            if not dev_branches:
                print(f"  \033[93m! No integration branches found for '{name}'. Skipping.\033[0m")
                return False
            release_branch = dev_branches[0]

        if not assume_yes:
            ans = input(f"  > Merge '{release_branch}' into '{default_branch}' and tag release? [Y/n]: ").strip().lower()
            if ans in ("n", "no"): return False

        tag_name = release_branch[4:] if release_branch.startswith("dev-") else f"v{release_branch}"

        subprocess.run(["git", "checkout", default_branch], cwd=dest, capture_output=True)
        subprocess.run(["git", "pull", "--rebase", "origin", default_branch], cwd=dest, capture_output=True)

        ahead_check = subprocess.run(["git", "rev-list", "--count", f"{default_branch}..{release_branch}"], cwd=dest, capture_output=True, text=True)
        ahead_count = int(ahead_check.stdout.strip()) if ahead_check.returncode == 0 and ahead_check.stdout.strip().isdigit() else 0

        if ahead_count == 0:
            print(f"  - No new changes in '{release_branch}' to release. Skipping.")
            return False

        print(f"  - Releasing '{release_branch}' to '{default_branch}' as '{tag_name}'...")
        if subprocess.run(["git", "merge", release_branch, "--no-ff", "-m", f"Release {tag_name}"], cwd=dest, capture_output=True, text=True).returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=dest, capture_output=True)
            print(f"  \033[91m! Merge conflict. Aborting.\033[0m")
            return False

        check_tag = subprocess.run(["git", "tag", "-l", tag_name], cwd=dest, capture_output=True, text=True)
        if tag_name not in check_tag.stdout.split():
            subprocess.run(["git", "tag", "-a", tag_name, "-m", f"Release {tag_name}"], cwd=dest, capture_output=True, text=True)

        if tmpl_src_root:
            new_yaml_version = tag_name[1:] if tag_name.startswith("v") else tag_name
            self._update_yaml_version(tmpl_src_root, raw_token, new_yaml_version)

        if push:
            print(f"  - Pushing branch '{default_branch}' and tag '{tag_name}' to origin...")
            subprocess.run(["git", "push", "origin", default_branch], cwd=dest, capture_output=True, text=True)
            subprocess.run(["git", "push", "origin", tag_name], cwd=dest, capture_output=True, text=True)

        return True

    def drop_feature(self, dest: Path, name: str, assume_yes: bool = False) -> bool:
        res = subprocess.run(["git", "branch", "--show-current"], cwd=dest, capture_output=True, text=True)
        current_branch = res.stdout.strip()
        res_main = subprocess.run(["git", "branch", "--list", "main"], cwd=dest, capture_output=True, text=True)
        default_branch = "main" if "main" in res_main.stdout else "master"

        if current_branch == default_branch or current_branch.startswith("dev-"):
            print(f"  ! Cannot drop protected branch '{current_branch}'.")
            return False

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

        if not assume_yes:
            ans_confirm = input(f"  > \033[91mWARNING: This will permanently delete '{current_branch}' and return to '{target_dev}'. Proceed? [y/N]:\033[0m ").strip().lower()
            if ans_confirm not in ("y", "yes"): return False

        subprocess.run(["git", "reset", "--hard"], cwd=dest, capture_output=True)
        subprocess.run(["git", "checkout", target_dev], cwd=dest, capture_output=True)
        subprocess.run(["git", "branch", "-D", current_branch], cwd=dest, capture_output=True)
        subprocess.run(["git", "push", "origin", "--delete", current_branch], cwd=dest, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"  ✓ Dropped '{current_branch}'.")
        return True

    def _update_yaml_version(self, tmpl_dir: Path, raw_token: str, new_version: str) -> None:
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