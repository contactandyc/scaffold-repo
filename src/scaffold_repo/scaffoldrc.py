# src/scaffold_repo/scaffoldrc.py
import sys
from pathlib import Path

def parse_bash_rc(path: Path) -> dict:
    """Safely parses a Bash/env file without executing it."""
    cfg = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if line.startswith("export "): line = line[7:].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip().lower()] = v.strip().strip('"\'')
    except Exception: pass
    return cfg

def find_scaffoldrc(current_dir: Path) -> dict:
    """Walks upward to find a .scaffoldrc, falling back to ~/.scaffoldrc."""
    target = current_dir.resolve()
    for parent in [target, *target.parents]:
        rc_candidate = parent / ".scaffoldrc"
        if rc_candidate.is_file():
            cfg = parse_bash_rc(rc_candidate)
            cfg.setdefault("workspace_dir", str(parent))
            return cfg

    global_rc = Path.home() / ".scaffoldrc"
    if global_rc.is_file():
        return parse_bash_rc(global_rc)
    return {}

def _interactive_select(prompt: str, options: list[str], default_idx: int = 0) -> int:
    """Interactive arrow-key selection menu with default support."""
    try:
        import sys, tty, termios
    except ImportError:
        print(prompt)
        for i, opt in enumerate(options):
            print(f"  {i+1}) {opt}")
        while True:
            ans = input(f"Select [1-{len(options)}] (default {default_idx+1}): ").strip()
            if not ans: return default_idx
            if ans.isdigit() and 1 <= int(ans) <= len(options):
                return int(ans) - 1
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
            elif ch in ('\r', '\n'):
                break
            elif ch == '\x03':
                raise KeyboardInterrupt

            sys.stdout.write(f"\033[{len(options) + 1}A")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()

    sys.stdout.write(f"\033[{len(options) + 1}A")
    sys.stdout.write("\r\033[K")
    sys.stdout.write("\033[J")
    sys.stdout.flush()
    return selected

def init_scaffoldrc() -> int:
    """Interactively initializes or updates a .scaffoldrc in the target workspace."""
    print("\n\033[1m=== Initializing scaffold-repo workspace ===\033[0m\n")

    scaffold_src = Path(__file__).resolve().parents[2]

    if not (scaffold_src / "templates").is_dir():
        print(f"❌ Error: Cannot find templates at {scaffold_src / 'templates'}")
        print("   Is your installation corrupted?")
        return 1

    print(f"✅ Auto-detected scaffold templates at: \033[96m{scaffold_src}\033[0m")

    # 1. Look for existing .scaffoldrc to prepopulate defaults
    existing_cfg = {}
    existing_rc_path = None
    target = Path.cwd().resolve()

    for parent in [target, *target.parents]:
        candidate = parent / ".scaffoldrc"
        if candidate.is_file():
            existing_cfg = parse_bash_rc(candidate)
            existing_rc_path = candidate
            print(f"🔍 Found existing config at: \033[90m{existing_rc_path}\033[0m\n")
            break
    else:
        global_rc = Path.home() / ".scaffoldrc"
        if global_rc.is_file():
            existing_cfg = parse_bash_rc(global_rc)
            existing_rc_path = global_rc
            print(f"🔍 Found global config at: \033[90m{existing_rc_path}\033[0m\n")

    # 2. Extract existing values or set defaults
    def_ws = existing_cfg.get("workspace_dir") or str(existing_rc_path.parent if existing_rc_path else Path.home() / "repos")
    def_prefix = existing_cfg.get("prefix") or "/usr/local"
    def_btype = existing_cfg.get("build_type") or "Release"
    def_variant = existing_cfg.get("build_variant") or "static"

    # 3. Interactive Prompts
    try:
        # -- Workspace Directory --
        ws_input = input(f"Workspace Directory [\033[92m{def_ws}\033[0m]: ").strip()
        workspace_dir = Path(ws_input).expanduser().resolve() if ws_input else Path(def_ws).expanduser().resolve()

        # -- Install Prefix --
        opt_ws = str(workspace_dir / "install")
        opt_sys = "/usr/local"
        opt_custom = "Custom (type it yourself)"

        prefix_opts = [
            f"Workspace Install ({opt_ws})",
            f"System Install ({opt_sys})",
            opt_custom
        ]

        start_p_idx = 0 if def_prefix == opt_ws else (1 if def_prefix == opt_sys else 2)
        p_idx = _interactive_select("Install Prefix:", prefix_opts, default_idx=start_p_idx)

        if p_idx == 0:
            prefix = opt_ws
        elif p_idx == 1:
            prefix = opt_sys
        else:
            prefix_input = input(f"  > Enter Custom Install Prefix [\033[92m{def_prefix}\033[0m]: ").strip()
            prefix = prefix_input if prefix_input else def_prefix

        # -- Build Type --
        btypes = ["Release", "Debug", "RelWithDebInfo", "MinSizeRel"]
        start_b_idx = btypes.index(def_btype) if def_btype in btypes else 0
        b_idx = _interactive_select("Build Type:", btypes, default_idx=start_b_idx)
        btype = btypes[b_idx]

        # -- Build Variant --
        variants = ["debug", "memory", "static", "shared"]
        start_v_idx = variants.index(def_variant) if def_variant in variants else 0
        v_idx = _interactive_select("Default Build Variant:", variants, default_idx=start_v_idx)
        variant = variants[v_idx]

    except KeyboardInterrupt:
        print("\nAborted.")
        return 1

    workspace_dir.mkdir(parents=True, exist_ok=True)
    rc_path = workspace_dir / ".scaffoldrc"

    content = (
        f'export SCAFFOLD_DIR="{scaffold_src}"\n'
        f'export WORKSPACE_DIR="{workspace_dir}"\n'
        f'export PREFIX="{prefix}"\n'
        f'export BUILD_TYPE="{btype}"\n'
        f'export BUILD_VARIANT="{variant}"\n'
    )

    if rc_path.exists():
        try:
            ans = input(f"\n⚠️  {rc_path} already exists. Overwrite? [y/N]: ").strip().lower()
        except KeyboardInterrupt:
            print("\nAborted.")
            return 1
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 0

    try:
        rc_path.write_text(content, encoding="utf-8")
        print(f"\n✅ Successfully created workspace at \033[96m{workspace_dir}\033[0m")
        print(f"   Configuration saved to: {rc_path}")
        return 0
    except Exception as e:
        print(f"\n❌ Failed to write config: {e}", file=sys.stderr)
        return 1
