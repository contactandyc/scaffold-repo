# src/scaffold_repo/scaffoldrc.py
import sys
import subprocess
from pathlib import Path
import re
import yaml

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

def _interactive_select(prompt: str, options: list[str], default_idx: int = 0, multiselect: bool = False) -> list[int] | int:
    if multiselect:
        print(f"\n{prompt}")
        for i, opt in enumerate(options):
            print(f"  {i+1}) {opt}")
        while True:
            ans = input(f"Select one or more [1-{len(options)}] (comma-separated): ").strip()
            if not ans: return [default_idx]
            try:
                indices = [int(x.strip()) - 1 for x in ans.split(",")]
                if all(0 <= x < len(options) for x in indices):
                    return indices
            except ValueError: pass
            print("Invalid selection. Use comma-separated numbers (e.g., 1,3).")

    try:
        import sys, tty, termios
    except ImportError:
        print(prompt)
        for i, opt in enumerate(options):
            print(f"  {i+1}) {opt}")
        while True:
            ans = input(f"Select [1-{len(options)}] (default {default_idx+1}): ").strip()
            if not ans: return default_idx
            if ans.isdigit() and 1 <= int(ans) <= len(options): return int(ans) - 1
            print("Invalid selection.")

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    selected = default_idx
    sys.stdout.write("\033[?25l")
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
            if ch == '\x1b':
                ch2 = sys.stdin.read(2)
                if ch2 == '[A': selected = max(0, selected - 1)
                elif ch2 == '[B': selected = min(len(options) - 1, selected + 1)
            elif ch in ('\r', '\n'): break
            elif ch == '\x03': raise KeyboardInterrupt
            sys.stdout.write(f"\033[{len(options) + 1}A")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
    sys.stdout.write(f"\033[{len(options) + 1}A\r\033[K\033[J")
    sys.stdout.flush()
    return selected

def _sync_registry(url: str, ref: str) -> Path:
    slug = re.sub(r"[^0-9A-Za-z]+", "-", url).strip("-").lower()
    cache_dir = Path.home() / ".cache" / "scaffold-repo" / "registries" / slug

    if not cache_dir.exists():
        print(f"📥 Cloning remote registry from {url}...")
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", url, str(cache_dir)], check=True)
    else:
        print(f"🔄 Syncing remote registry...")
        subprocess.run(["git", "fetch", "--all"], cwd=cache_dir, check=True)

    if ref:
        subprocess.run(["git", "checkout", ref], cwd=cache_dir, check=True)
        subprocess.run(["git", "pull", "--rebase"], cwd=cache_dir, check=False)
    else:
        subprocess.run(["git", "checkout", "main"], cwd=cache_dir, stderr=subprocess.DEVNULL) or \
        subprocess.run(["git", "checkout", "master"], cwd=cache_dir, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "pull", "--rebase"], cwd=cache_dir, check=False)

    return cache_dir

def init_scaffoldrc() -> int:
    print("\n\033[1m=== Initializing scaffold-repo workspace ===\033[0m\n")

    target = Path.cwd().resolve()
    existing_cfg = find_scaffoldrc(target)

    if existing_cfg.get("workspace_dir"): def_ws = existing_cfg["workspace_dir"]
    elif (target / "scaffold.yaml").is_file(): def_ws = str(target.parent)
    else: def_ws = str(target)

    ws_input = input(f"Workspace Directory [\033[92m{def_ws}\033[0m]: ").strip()
    workspace_dir = Path(ws_input).expanduser().resolve() if ws_input else Path(def_ws).expanduser().resolve()

    print("")
    src_opts = ["Built-in (Default Templates)", "Remote Git Repository (Company Registry)"]
    src_idx = _interactive_select("Template Registry Source:", src_opts)

    reg_url = existing_cfg.get("template_registry_url", "")
    reg_ref = existing_cfg.get("template_registry_ref", "")
    tmpl_dir = Path(__file__).resolve().parents[2] / "templates"

    if src_idx == 1:
        reg_url = input(f"  > Git URL [\033[92m{reg_url}\033[0m]: ").strip() or reg_url
        reg_ref = input(f"  > Branch/Tag/Ref [\033[92m{reg_ref or 'main'}\033[0m]: ").strip() or reg_ref
        try:
            cache_dir = _sync_registry(reg_url, reg_ref)
            tmpl_dir = cache_dir / "templates" if (cache_dir / "templates").is_dir() else cache_dir
        except subprocess.CalledProcessError:
            print("\n❌ Failed to fetch remote registry. Aborting.")
            return 1

    print("")
    ans_overlay = input("Do you want to configure a local templates overlay? (For custom profiles/licenses) [y/N]: ").strip().lower()
    if ans_overlay in ("y", "yes"):
        overlay_dir = input("  > Overlay Directory Path (relative to workspace) [\033[92m./templates\033[0m]: ").strip() or "./templates"
        existing_cfg["template_overlay_dir"] = overlay_dir

    print(f"✅ Auto-detected base templates at: \033[96m{tmpl_dir}\033[0m")

    prompts_queue = []
    root_defaults = tmpl_dir / ".scaffold-defaults.yaml"
    if root_defaults.exists():
        try:
            loaded = (yaml.safe_load(root_defaults.read_text(encoding="utf-8")) or {}).get("init_prompts", [])
            for lp in loaded: lp["__path"] = []
            prompts_queue.extend(loaded)
        except Exception: pass

    answers = {}
    collected_answers = []
    seen_prompt_vars = set()

    try:
        if prompts_queue:
            print("\n\033[1m--- Dynamic Registry Configuration ---\033[0m")
            from jinja2 import Environment
            jenv = Environment()

        while prompts_queue:
            p = prompts_queue.pop(0)
            p_list = p.get("__path", [])
            raw_var = p.get("var")

            # Use raw_var directly to check for deduplication (no prefixes!)
            if raw_var in seen_prompt_vars:
                continue
            seen_prompt_vars.add(raw_var)

            ctx = {k.lower(): v for k, v in answers.items()}
            if len(p_list) > 0: ctx["stack"] = p_list[0]
            if len(p_list) > 1: ctx["stack_type"] = p_list[1]

            is_multi = p.get("multiselect", False)
            prompt_str = p.get("prompt", f"Set {raw_var}:")

            # Dig into the existing YAML config to find defaults based on current path
            ns_key = "_".join(p_list).lower() if p_list else ""
            def_source = existing_cfg.get(ns_key, {}) if ns_key else existing_cfg
            def_val = def_source.get(raw_var.lower()) or p.get("default", "")
            options = p.get("options", [])

            dir_str = ""
            if "choices_from_dir" in p:
                dir_tmpl = p["choices_from_dir"]
                dir_str = jenv.from_string(dir_tmpl).render(**ctx).strip()
                target_dir = tmpl_dir / dir_str
                if target_dir.is_dir():
                    exclude = p.get("exclude", [])
                    found = sorted([d.name for d in target_dir.iterdir() if d.is_dir() and not d.name.startswith(".")])
                    options.extend([f for f in found if f not in exclude])

            if def_val == "./install": def_val = str(workspace_dir / "install")
            options = [o if o != "./install" else str(workspace_dir / "install") for o in options]

            if options:
                if len(options) == 1 and "Custom" not in options[0]:
                    ans = [options[0]] if is_multi else options[0]
                    print(f"{prompt_str} \033[96m{options[0]}\033[0m (Auto-selected)")
                else:
                    start_idx = options.index(def_val) if def_val in options and not is_multi else 0
                    ans_idx = _interactive_select(prompt_str, options, default_idx=start_idx, multiselect=is_multi)
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
                    sub_defaults = tmpl_dir / dir_str / a / ".scaffold-defaults.yaml"
                    if sub_defaults.exists():
                        try:
                            loaded_prompts = (yaml.safe_load(sub_defaults.read_text(encoding="utf-8")) or {}).get("init_prompts", [])
                            next_path = p_list + [a]
                            for lp in loaded_prompts:
                                lp["__path"] = next_path
                            new_prompts.extend(loaded_prompts)
                        except Exception: pass
                prompts_queue = new_prompts + prompts_queue

    except KeyboardInterrupt:
        print("\nAborted.")
        return 1

    # --- Post-loop: Build the YAML Dict and Scoped Bash Files ---
    yaml_config = {
        "workspace_dir": str(workspace_dir),
    }
    if reg_url:
        yaml_config["template_registry_url"] = reg_url
        yaml_config["template_registry_ref"] = reg_ref
    else:
        yaml_config["scaffold_dir"] = str(Path(__file__).resolve().parents[2])

    # 1. Store global variables in root of YAML
    for p_list, raw_var, ans in collected_answers:
        if not p_list:
            yaml_config[raw_var.lower()] = ans

    # 2. Build the scoped Bash files
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
            # Store in YAML config under the namespace block (e.g. c_cmake: { prefix: ... })
            yaml_config[leaf_ns] = leaf_dict

            # Prepare Bash export strings
            bash_lines = [f'export {k.upper()}="{v}"' for k, v in leaf_dict.items()]
            scoped_bash_files[f".scaffoldrc_{leaf_ns}"] = "\n".join(bash_lines) + "\n"

    # Write files
    workspace_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = workspace_dir / ".scaffoldrc.yaml"

    if yaml_path.exists():
        try:
            ans = input(f"\n⚠️  {yaml_path} already exists. Overwrite? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"): return 0
        except KeyboardInterrupt: return 1

    try:
        # Write Master YAML
        yaml_path.write_text(yaml.dump(yaml_config, sort_keys=False), encoding="utf-8")

        # Write Scoped Bash Files
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


def append_stack_to_workspace(stack: str, stack_type: str, workspace_dir: Path, tmpl_dir: Path, existing_cfg: dict) -> dict:
    import yaml
    import re
    from jinja2 import Environment
    jenv = Environment()

    print(f"\n⚙️  Configuring Workspace for \033[96m{stack}/{stack_type}\033[0m...")

    defaults_file = tmpl_dir / "stacks" / stack / stack_type / ".scaffold-defaults.yaml"
    if not defaults_file.exists():
        return existing_cfg

    try:
        data = yaml.safe_load(defaults_file.read_text(encoding="utf-8")) or {}
        init_prompts = data.get("init_prompts", [])
    except Exception:
        return existing_cfg

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
            target_dir = tmpl_dir / dir_str
            if target_dir.is_dir():
                exclude = p.get("exclude", [])
                found = sorted([d.name for d in target_dir.iterdir() if d.is_dir() and not d.name.startswith(".")])
                options.extend([f for f in found if f not in exclude])

        if def_val == "./install": def_val = str(workspace_dir / "install")
        options = [o if o != "./install" else str(workspace_dir / "install") for o in options]

        if options:
            if len(options) == 1 and "Custom" not in options[0]:
                ans = [options[0]] if is_multi else options[0]
                print(f"{prompt_str} \033[96m{options[0]}\033[0m (Auto-selected)")
            else:
                start_idx = options.index(def_val) if def_val in options and not is_multi else 0
                ans_idx = _interactive_select(prompt_str, options, default_idx=start_idx, multiselect=is_multi)
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

    # 1. Update YAML config in memory
    existing_cfg[leaf_ns] = leaf_dict

    # 2. Write updated YAML back to disk
    yaml_path = workspace_dir / ".scaffoldrc.yaml"
    yaml_path.write_text(yaml.dump(existing_cfg, sort_keys=False), encoding="utf-8")

    # 3. Regenerate the scoped bash file for this stack
    bash_lines = [f'export {k.upper()}="{v}"' for k, v in leaf_dict.items()]
    bash_content = "\n".join(bash_lines) + "\n"
    (workspace_dir / f".scaffoldrc_{leaf_ns}").write_text(bash_content, encoding="utf-8")

    print(f"✅ Workspace globally configured for {stack}/{stack_type}")
    return existing_cfg