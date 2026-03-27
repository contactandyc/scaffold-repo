# src/scaffold_repo/create/cli_plugin.py
from __future__ import annotations

import argparse
import subprocess
from datetime import date
from pathlib import Path

from ..core.config import ConfigReader
from ..cli.ui import interactive_select
from ..cli.workspace import append_stack_to_workspace

def add_create_arguments(parser: argparse.ArgumentParser) -> None:
    """Appends repository creation arguments."""
    grp_cr = parser.add_argument_group("Creation Options")
    grp_cr.add_argument("--create", metavar="SLUG", help="Create a new project in the workspace")

def run_create(project_slug: str, workspace_dir: Path, reader: ConfigReader, existing_cfg: dict) -> int:
    """Interactive wizard to bootstrap a new repository and its scaffold.yaml manifest."""
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
        "depends_on: []",
        "",
        "# ── 5. Sources (Auto-Discovered) ──",
        "# library_sources:",
        "#   - src/main.c",
        "",
        "# ── 6. Tests (Auto-Discovered) ──",
        "# tests:",
        "#   targets:",
        "#     - name: test_custom",
        "#       sources: ",
        "#         - tests/src/test_custom.c",
        "",
        "# ── 7. Apps & Examples (Optional) ──",
        "# apps:",
        "#   01_basic_example:",
        "#     binaries:",
        "#       basic_app:",
        "#         - src/main.c",
        "",
        "# ── 8. Feature Flags & Overrides ──",
        "# packages:",
        "#   changie: true",
        ""
    ])

    scaffold_file.write_text("\n".join(yaml_lines), encoding="utf-8")
    print(f"✅ Initialized {project_slug}/scaffold.yaml")

    return 0
