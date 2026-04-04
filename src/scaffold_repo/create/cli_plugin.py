# src/scaffold_repo/create/cli_plugin.py
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, ChoiceLoader

from ..core.config import ConfigReader
from ..cli.ui import interactive_select
from ..cli.workspace import append_stack_to_workspace
from ..utils.text import snake, camel

def add_create_arguments(parser: argparse.ArgumentParser) -> None:
    """Appends repository creation arguments."""
    grp_cr = parser.add_argument_group("Creation Options")
    grp_cr.add_argument("--create", metavar="SLUG", help="Create a new project in the workspace")

def run_create(project_slug: str, workspace_dir: Path, reader: ConfigReader, existing_cfg: dict) -> int:
    """Interactive wizard to bootstrap a new repository and its scaffold.yaml manifest."""
    loaders = [FileSystemLoader(str(reader.tmpl_src._pkg_root))] if reader.tmpl_src and reader.tmpl_src._pkg_root else []
    jenv = Environment(
        loader=ChoiceLoader(loaders) if loaders else None,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True
    )

    print(f"\n\033[1m=== Creating New Project: {project_slug} ===\033[0m\n")

    # 1. Discover Stacks
    stacks = set()
    for rel, _, _, _ in reader.tmpl_src.iter_files():
        if rel.startswith("stacks/"):
            parts = rel.split("/")
            if len(parts) >= 2 and parts[1] and not parts[1].startswith("."):
                stacks.add(parts[1])
    stacks = sorted(list(stacks))

    if not stacks:
        print("❌ No stacks found in templates/stacks/.", file=sys.stderr)
        return 1

    stack_idx = interactive_select("Select primary stack:", stacks)
    selected_stack = stacks[stack_idx]

    # 2. Discover Stack Types
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

    # 3. Configure Workspace for the chosen stack
    ns_key = f"{selected_stack}_{selected_type}".lower()
    if ns_key not in existing_cfg:
        existing_cfg = append_stack_to_workspace(selected_stack, selected_type, workspace_dir, reader, existing_cfg)

    # 4. Process Creation Prompts from Defaults
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

    # 5. Process Profile Selection
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

    # 6. Initialize the Target Directory
    project_dir = workspace_dir / project_slug
    if project_dir.exists() and (project_dir / "scaffold.yaml").exists():
        print(f"⚠️  Project {project_slug} already exists.")
        return 1

    project_dir.mkdir(parents=True, exist_ok=True)
    scaffold_file = project_dir / "scaffold.yaml"
    subprocess.run(["git", "init"], cwd=project_dir, capture_output=True)

    # 7. Render Template
    ctx = {
        "project_slug": project_slug,
        "project_title": project_slug,
        "project_snake": snake(project_slug),
        "project_camel": camel(project_slug),
        "stack": selected_stack,
        "stack_type": selected_type,
        "current_date": date.today().isoformat(),
        "profile": selected_profile,
        "registry_url": existing_cfg.get("template_registry_url", ""),
        "registry_ref": existing_cfg.get("template_registry_ref", "main"),
        "prompt_answers": answers
    }

    # Search for the template in the specific stack first, then fallback to base
    template_text = reader.tmpl_src.read_resource_text(f"stacks/{selected_stack}/{selected_type}/scaffold.yaml.j2")
    if not template_text:
        template_text = reader.tmpl_src.read_resource_text("base/scaffold.yaml.j2")

    if template_text:
        try:
            rendered = jenv.from_string(template_text).render(**ctx)
            scaffold_file.write_text(rendered.rstrip() + "\n", encoding="utf-8")
        except Exception as e:
            print(f"❌ Failed to render scaffold.yaml template: {e}", file=sys.stderr)
            return 1
    else:
        # Bare-minimum fallback if the registry is completely missing the template
        print("⚠️  Warning: scaffold.yaml.j2 not found in templates. Using bare minimum.")
        fallback_yaml = (
            f"project_title: {project_slug}\n"
            f"version: \"0.1.0\"\n"
            f"stack: {selected_stack}/{selected_type}\n"
        )
        scaffold_file.write_text(fallback_yaml, encoding="utf-8")

    print(f"✅ Initialized {project_slug}/scaffold.yaml")

    return 0