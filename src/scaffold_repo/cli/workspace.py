# src/scaffold_repo/cli/workspace.py
import sys
from pathlib import Path
import yaml
from .ui import interactive_select

def find_scaffoldrc(current_dir: Path) -> dict:
    """Walks upward to find .scaffoldrc.yaml, falling back to ~/.scaffoldrc.yaml."""
    target = current_dir.resolve()
    for parent in [target, *target.parents]:
        rc_candidate = parent / ".scaffoldrc.yaml"
        if rc_candidate.is_file():
            try:
                cfg = yaml.safe_load(rc_candidate.read_text(encoding="utf-8")) or {}
                cfg.setdefault("workspace_dir", str(parent))
                return cfg
            except Exception: pass
    global_rc = Path.home() / ".scaffoldrc.yaml"
    if global_rc.is_file():
        try: return yaml.safe_load(global_rc.read_text(encoding="utf-8")) or {}
        except Exception: pass
    return {}

def init_scaffoldrc() -> int:
    import yaml
    print("\n\033[1m=== Initializing scaffold-repo workspace ===\033[0m\n")

    target = Path.cwd().resolve()
    existing_cfg = find_scaffoldrc(target)

    # 1. Determine Workspace
    if existing_cfg.get("workspace_dir"): def_ws = existing_cfg["workspace_dir"]
    elif (target / "scaffold.yaml").is_file(): def_ws = str(target.parent)
    else: def_ws = str(target)

    ws_input = input(f"Workspace Directory [\033[92m{def_ws}\033[0m]: ").strip()
    workspace_dir = Path(ws_input).expanduser().resolve() if ws_input else Path(def_ws).expanduser().resolve()

    # 2. Determine Remote Base Templates
    print("\nWhere should this workspace pull its scaffolding standards from by default?")
    src_opts = [
        "Andy's Official Starter Templates (github.com/contactandyc/scaffold-templates)",
        "A Custom Corporate Registry (Provide a Git URL)",
        "None (Skip registry auto-population)"
    ]
    src_idx = interactive_select("Template Registry Source:", src_opts)

    reg_url = existing_cfg.get("template_registry_url", "https://github.com/contactandyc/scaffold-templates.git")
    reg_ref = existing_cfg.get("template_registry_ref", "main")
    base_tmpl_dir = None

    if src_idx in (0, 1):
        if src_idx == 1:
            reg_url = input(f"  > Git URL [\033[92m{reg_url}\033[0m]: ").strip() or reg_url
            reg_ref = input(f"  > Branch/Tag/Ref [\033[92m{reg_ref}\033[0m]: ").strip() or reg_ref
        elif src_idx == 0:
            reg_url = "https://github.com/contactandyc/scaffold-templates.git"
            reg_ref = "main"

        from ..utils.git import sync_git_template_repo
        cached_dir = sync_git_template_repo(reg_url, reg_ref, workspace_dir)
        if not cached_dir:
            print("\n❌ Failed to fetch remote registry. Aborting.")
            return 1

        base_tmpl_dir = (cached_dir / "templates").as_posix() if (cached_dir / "templates").is_dir() else cached_dir.as_posix()
        print(f"✅ Active base templates mounted from: \033[96m{reg_url}@{reg_ref}\033[0m")
    else:
        reg_url = ""
        reg_ref = ""

    # 3. Mount the ConfigReader
    from ..core.config import ConfigReader
    reader = ConfigReader(
        workspace_dir,
        project_name=None,
        base_templates_dir=base_tmpl_dir
    )

    prompts_queue = []
    data = reader.tmpl_src.get_stacked_defaults("_")
    loaded = data.get("init_prompts", [])
    for lp in loaded: lp["__path"] = []
    prompts_queue.extend(loaded)

    answers = {}
    collected_answers = []
    seen_prompt_vars = set()
    default_profile = None

    try:
        if prompts_queue:
            print("\n\033[1m--- Dynamic Registry Configuration ---\033[0m")
            from jinja2 import Environment
            jenv = Environment()

        while prompts_queue:
            p = prompts_queue.pop(0)

            p_list = p.get("__path", [])
            if not isinstance(p_list, list): p_list = list(p_list)

            raw_var = p.get("var")

            prompt_key = (tuple(p_list), raw_var)
            if prompt_key in seen_prompt_vars:
                continue
            seen_prompt_vars.add(prompt_key)

            ctx = {k.lower(): v for k, v in answers.items()}
            if len(p_list) > 0: ctx["stack"] = p_list[0]
            if len(p_list) > 1: ctx["stack_type"] = p_list[1]

            is_multi = p.get("multiselect", False)
            prompt_str = p.get("prompt", f"Set {raw_var}:")

            ns_key = "_".join(p_list).lower() if p_list else ""
            def_source = existing_cfg.get(ns_key, {}) if ns_key else existing_cfg
            def_val = def_source.get(raw_var.lower()) or p.get("default", "")
            options = p.get("options", [])

            dir_str = ""
            if "choices_from_dir" in p:
                dir_tmpl = p["choices_from_dir"]
                dir_str = jenv.from_string(dir_tmpl).render(**ctx).strip()

                options_from_dir = set()
                for rel, _, _, _ in reader.tmpl_src.iter_files():
                    if rel.startswith(f"{dir_str}/"):
                        parts = rel[len(dir_str)+1:].split("/")
                        if parts and parts[0] and not parts[0].startswith("."):
                            options_from_dir.add(parts[0])

                exclude = p.get("exclude", [])
                found = sorted([f for f in options_from_dir if f not in exclude])
                options.extend(found)

            if def_val == "./install": def_val = str(workspace_dir / "install")
            options = [o if o != "./install" else str(workspace_dir / "install") for o in options]

            if options:
                if len(options) == 1 and "Custom" not in options[0]:
                    ans = [options[0]] if is_multi else options[0]
                    print(f"{prompt_str} \033[96m{options[0]}\033[0m (Auto-selected)")
                else:
                    start_idx = options.index(def_val) if def_val in options and not is_multi else 0
                    ans_idx = interactive_select(prompt_str, options, default_idx=start_idx, multiselect=is_multi)
                    if is_multi:
                        ans = [options[i] for i in ans_idx]
                    else:
                        ans = options[ans_idx]
                        if "Custom" in ans:
                            ans = input(f"  > Enter {raw_var} [\033[92m{def_val}\033[0m]: ").strip() or def_val
            else:
                if "choices_from_dir" in p:
                    ans = [] if is_multi else def_val
                else:
                    ans_str = input(f"{prompt_str} [\033[92m{def_val}\033[0m]: ").strip() or def_val
                    ans = [x.strip() for x in ans_str.split(",")] if is_multi else ans_str

            answers[raw_var] = ans
            collected_answers.append((p_list, raw_var, ans))

            if "choices_from_dir" in p and dir_str and ans:
                ans_list = ans if isinstance(ans, list) else [ans]
                new_prompts = []
                for a in ans_list:
                    sub_defaults_text = reader.tmpl_src.read_resource_text(f"{dir_str}/{a}/.scaffold-defaults.yaml")
                    if sub_defaults_text:
                        try:
                            loaded_prompts = (yaml.safe_load(sub_defaults_text) or {}).get("init_prompts", [])
                            next_path = list(p_list) + [a]
                            for lp in loaded_prompts:
                                lp["__path"] = next_path
                            new_prompts.extend(loaded_prompts)
                        except Exception as e:
                            print(f"Warning: Failed to load prompts for {a}: {e}")

                prompts_queue = new_prompts + prompts_queue

        # --- THE PROFILES FIX ---
        profiles = set()
        for f in reader.tmpl_src.find_registry_yamls("profiles"):
            if f.endswith(".yaml"):
                profiles.add(f[len("profiles/"): -5])
            elif f.endswith(".yml"):
                profiles.add(f[len("profiles/"): -4])
        profiles = sorted(list(profiles))

        default_profile = existing_cfg.get("default_profile")
        if profiles:
            print("\n\033[1m--- Workspace Defaults ---\033[0m")
            if len(profiles) == 1:
                default_profile = profiles[0]
                print(f"Default project profile: \033[96m{default_profile}\033[0m (Auto-selected)")
            else:
                start_idx = profiles.index(default_profile) if default_profile in profiles else 0
                prof_idx = interactive_select("Select default project profile for this workspace:", profiles, default_idx=start_idx)
                default_profile = profiles[prof_idx]

    except KeyboardInterrupt:
        print("\nAborted.")
        return 1

    yaml_config = {
        "workspace_dir": str(workspace_dir),
    }

    if reg_url:
        yaml_config["template_registry_url"] = reg_url
        yaml_config["template_registry_ref"] = reg_ref

    if default_profile:
        yaml_config["default_profile"] = default_profile

    for p_list, raw_var, ans in collected_answers:
        if not p_list:
            yaml_config[raw_var.lower()] = ans

    all_paths = {tuple(p_list) for p_list, _, _ in collected_answers if p_list}
    leaf_paths = []
    for p1 in all_paths:
        is_leaf = True
        for p2 in all_paths:
            if len(p2) > len(p1) and p2[:len(p1)] == p1:
                is_leaf = False
                break
        if is_leaf:
            leaf_paths.append(p1)

    scoped_bash_files = {}
    for leaf in leaf_paths:
        leaf_ns = "_".join(leaf).lower()
        leaf_dict = {}
        for i in range(len(leaf) + 1):
            current_ancestor = leaf[:i]
            for p_list, raw_var, ans in collected_answers:
                if tuple(p_list) == current_ancestor and raw_var.lower() not in ("stack", "stack_type"):
                    val_str = ",".join(ans) if isinstance(ans, list) else str(ans)
                    leaf_dict[raw_var] = val_str

        if leaf_dict:
            yaml_config[leaf_ns] = leaf_dict
            bash_lines = [f'export {k.upper()}="{v}"' for k, v in leaf_dict.items()]
            scoped_bash_files[f".scaffoldrc_{leaf_ns}"] = "\n".join(bash_lines) + "\n"

    workspace_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = workspace_dir / ".scaffoldrc.yaml"

    if yaml_path.exists():
        try:
            ans = input(f"\n⚠️  {yaml_path} already exists. Overwrite? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"): return 0
        except KeyboardInterrupt: return 1

    try:
        yaml_path.write_text(yaml.dump(yaml_config, sort_keys=False), encoding="utf-8")
        for filename, content in scoped_bash_files.items():
            (workspace_dir / filename).write_text(content, encoding="utf-8")

        print(f"\n✅ Successfully created workspace at \033[96m{workspace_dir}\033[0m")
        print(f"   Configuration saved to: {yaml_path}")
        if scoped_bash_files:
            print(f"   Generated {len(scoped_bash_files)} scoped bash environments.")
        return 0
    except Exception as e:
        print(f"\n❌ Failed to write config: {e}", file=sys.stderr)
        return 1

def append_stack_to_workspace(stack: str, stack_type: str, workspace_dir: Path, reader, existing_cfg: dict) -> dict:
    import yaml
    from jinja2 import Environment
    jenv = Environment()

    print(f"\n⚙️  Configuring Workspace for \033[96m{stack}/{stack_type}\033[0m...")

    data = reader.tmpl_src.get_stacked_defaults(f"stacks/{stack}/{stack_type}/_")
    init_prompts = data.get("init_prompts", [])

    if not init_prompts:
        return existing_cfg

    answers = {}
    leaf_ns = f"{stack}_{stack_type}".lower()
    leaf_dict = existing_cfg.get(leaf_ns, {})

    for p in init_prompts:
        raw_var = p.get("var")
        prompt_str = p.get("prompt", f"Set {raw_var}:")
        def_val = leaf_dict.get(raw_var.lower()) or p.get("default", "")
        options = p.get("options", [])
        is_multi = p.get("multiselect", False)

        dir_str = ""
        if "choices_from_dir" in p:
            dir_tmpl = p["choices_from_dir"]
            dir_str = jenv.from_string(dir_tmpl).render(**answers).strip()

            options_from_dir = set()
            for rel, _, _, _ in reader.tmpl_src.iter_files():
                if rel.startswith(f"{dir_str}/"):
                    parts = rel[len(dir_str)+1:].split("/")
                    if parts and parts[0] and not parts[0].startswith("."):
                        options_from_dir.add(parts[0])

            exclude = p.get("exclude", [])
            found = sorted([f for f in options_from_dir if f not in exclude])
            options.extend(found)

        if def_val == "./install": def_val = str(workspace_dir / "install")
        options = [o if o != "./install" else str(workspace_dir / "install") for o in options]

        if options:
            if len(options) == 1 and "Custom" not in options[0]:
                ans = [options[0]] if is_multi else options[0]
                print(f"{prompt_str} \033[96m{options[0]}\033[0m (Auto-selected)")
            else:
                start_idx = options.index(def_val) if def_val in options and not is_multi else 0
                ans_idx = interactive_select(prompt_str, options, default_idx=start_idx, multiselect=is_multi)
                if is_multi: ans = [options[i] for i in ans_idx]
                else:
                    ans = options[ans_idx]
                    if "Custom" in ans:
                        ans = input(f"  > Enter {raw_var} [\033[92m{def_val}\033[0m]: ").strip() or def_val
        else:
            if "choices_from_dir" in p: ans = [] if is_multi else def_val
            else:
                ans_str = input(f"{prompt_str} [\033[92m{def_val}\033[0m]: ").strip() or def_val
                ans = [x.strip() for x in ans_str.split(",")] if is_multi else ans_str

        answers[raw_var] = ans
        val_str = ",".join(ans) if isinstance(ans, list) else str(ans)
        leaf_dict[raw_var] = val_str

    existing_cfg[leaf_ns] = leaf_dict
    yaml_path = workspace_dir / ".scaffoldrc.yaml"
    yaml_path.write_text(yaml.dump(existing_cfg, sort_keys=False), encoding="utf-8")

    bash_lines = [f'export {k.upper()}="{v}"' for k, v in leaf_dict.items()]
    bash_content = "\n".join(bash_lines) + "\n"
    (workspace_dir / f".scaffoldrc_{leaf_ns}").write_text(bash_content, encoding="utf-8")

    print(f"✅ Workspace globally configured for {stack}/{stack_type}")
    return existing_cfg
