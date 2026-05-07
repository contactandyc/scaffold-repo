# src/scaffold_repo/bootstrap/orchestrator.py
from __future__ import annotations

import sys
import yaml
from pathlib import Path

from ..utils.git import ensure_clone
from ..core.config import ConfigReader
from ..build_libs import resolve_dependency_graph

def run_bootstrap(cwd: Path) -> int:
    target = cwd.resolve()
    manifest_file = target / "scaffold.yaml"
    manifest_data = yaml.safe_load(manifest_file.read_text(encoding="utf-8")) if manifest_file.exists() else {}

    print("\n⚙️  Bootstrapping hermetic workspace...")

    repos_dir = target / "repos"
    repos_dir.mkdir(exist_ok=True)

    # 1. Fetch Templates Hermetically into ./repos
    base_tmpl = manifest_data.get("base_templates", {})
    reg_url = base_tmpl.get("repo", "https://github.com/contactandyc/scaffold-templates.git")
    reg_ref = base_tmpl.get("ref", "main")

    template_repo_name = reg_url.split("/")[-1].replace(".git", "")
    template_dest = repos_dir / template_repo_name

    if template_dest.exists():
        print(f"📦 Template registry already exists at repos/{template_repo_name}. Leaving untouched.")
    else:
        print(f"📦 Fetching template registry into repos/{template_repo_name}...")
        try:
            ensure_clone(reg_url, template_dest, branch=reg_ref, shallow=True)
        except Exception as e:
            print(f"❌ Failed to fetch templates: {e}", file=sys.stderr)
            return 1

    base_tmpl_dir = (template_dest / "templates").as_posix() if (template_dest / "templates").is_dir() else template_dest.as_posix()

    # 2. Resolve & Clone Dependencies
    print("📦 Resolving and fetching dependency tree...")
    reader = ConfigReader(target, project_name=None, base_templates_dir=base_tmpl_dir)
    reader.load()
    idx = reader._build_library_index(reader.effective_config)

    # Natively clone all dependencies into the repos_dir
    # Note: resolve_dependency_graph inherently skips fetching if the directory exists!
    graph = resolve_dependency_graph(target, idx, repos_dir)

    # 3. Discover Stacks from local disk
    print("🔍 Scanning local manifests for stack footprints...")
    required_stacks = set()

    manifests_to_check = [manifest_file]
    for dep_info in graph.values():
        manifests_to_check.append(dep_info["path"] / "scaffold.yaml")

    for m_path in manifests_to_check:
        if m_path.exists():
            try:
                data = yaml.safe_load(m_path.read_text(encoding="utf-8")) or {}
                if "stack" in data:
                    required_stacks.add(str(data["stack"]).strip())
            except Exception: pass

    if required_stacks:
        print(f"   Found stacks: {', '.join(required_stacks)}")

    # 4. Extract Prompts from Templates
    prompts_queue = []

    global_txt = reader.tmpl_src.read_resource_text(".scaffold.yaml")
    if global_txt:
        try:
            loaded = (yaml.safe_load(global_txt) or {}).get("init_prompts", [])
            for lp in loaded: lp["__path"] = []
            prompts_queue.extend(loaded)
        except Exception: pass

    for req_stack in required_stacks:
        st = req_stack.split("/")[0] if "/" in req_stack else req_stack
        st_type = req_stack.split("/")[1] if "/" in req_stack else "base"

        for level, path_prefix in [
            ([st], f"stacks/{st}/.scaffold.yaml"),
            ([st, st_type], f"stacks/{st}/{st_type}/.scaffold.yaml")
        ]:
            sub_txt = reader.tmpl_src.read_resource_text(path_prefix)
            if sub_txt:
                try:
                    loaded = (yaml.safe_load(sub_txt) or {}).get("init_prompts", [])
                    for lp in loaded: lp["__path"] = list(level)
                    prompts_queue.extend(loaded)
                except Exception: pass

    # 5. Resolve Default Answers Headlessly
    answers = {}
    collected_answers = []
    seen_prompt_vars = set()

    for p in prompts_queue:
        p_list = p.get("__path", [])
        raw_var = p.get("var")
        prompt_key = (tuple(p_list), raw_var)
        if prompt_key in seen_prompt_vars: continue
        seen_prompt_vars.add(prompt_key)

        ns_key = "_".join(p_list).lower() if p_list else ""
        def_source = manifest_data.get(ns_key, {}) if ns_key else manifest_data
        if not def_source and ns_key:
            for k, v in manifest_data.items():
                if k.startswith(ns_key + "_") and isinstance(v, dict):
                    def_source = v
                    break
        if not def_source: def_source = manifest_data

        def_val = def_source.get(raw_var) or def_source.get(raw_var.lower()) or p.get("default", "")

        # Apply strict hermetic isolation mapping
        if def_val == "./install": def_val = str(repos_dir / "install")

        is_multi = p.get("multiselect", False)

        # --- FIX: Ensure multiselect variables (like 'stack') are properly coerced into lists ---
        if "choices_from_dir" in p:
            if is_multi:
                ans = [def_val] if isinstance(def_val, str) else (def_val if def_val else [])
            else:
                ans = def_val
        else:
            ans = [def_val] if is_multi and not isinstance(def_val, list) else def_val

        answers[raw_var] = ans
        collected_answers.append((p_list, raw_var, ans))

    # 6. Write Configuration Files to Root
    # --- FIX: Write the ABSOLUTE path to the repos directory ---
    yaml_config = {
        "workspace_dir": str(repos_dir),
        "template_registry_url": reg_url,
        "template_registry_ref": reg_ref,
    }
    if manifest_data.get("profile"):
        yaml_config["default_profile"] = manifest_data["profile"]

    for p_list, raw_var, ans in collected_answers:
        if not p_list: yaml_config[raw_var.lower()] = ans

    all_paths = {tuple(p_list) for p_list, _, _ in collected_answers if p_list}
    leaf_paths = [p1 for p1 in all_paths if all(len(p2) <= len(p1) or p2[:len(p1)] != p1 for p2 in all_paths if p2 != p1)]

    scoped_bash_files = {}
    for leaf in leaf_paths:
        leaf_ns = "_".join(leaf).lower()
        leaf_dict = {}
        for i in range(len(leaf) + 1):
            for p_list, raw_var, ans in collected_answers:
                if tuple(p_list) == leaf[:i] and raw_var.lower() not in ("stack", "stack_type"):
                    val_str = ",".join(ans) if isinstance(ans, list) else str(ans)
                    if val_str:
                        leaf_dict[raw_var] = val_str
        if leaf_dict:
            yaml_config[leaf_ns] = leaf_dict
            bash_lines = [f'export {k.upper()}="{v}"' for k, v in leaf_dict.items()]
            scoped_bash_files[f".scaffoldrc_{leaf_ns}"] = "\n".join(bash_lines) + "\n"

    # Write carefully, don't clobber active environments
    yaml_path = target / ".scaffoldrc.yaml"
    yaml_path.write_text(yaml.dump(yaml_config, sort_keys=False), encoding="utf-8")

    for filename, content in scoped_bash_files.items():
        (target / filename).write_text(content, encoding="utf-8")

    print(f"\n✅ Successfully bootstrapped workspace config at {target}")
    return 0