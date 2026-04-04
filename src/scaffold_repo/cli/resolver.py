# src/scaffold_repo/cli/resolver.py
from __future__ import annotations

import posixpath
import subprocess
import sys
import yaml
from pathlib import Path
from typing import Any

from .workspace import find_scaffoldrc
from ..core.config import ConfigReader
from ..utils.text import slug, snake
from ..utils.collections import deep_merge

def _get_active_git_project(current_dir: Path) -> str | None:
    try:
        res = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=current_dir, capture_output=True, text=True, check=True)
        return Path(res.stdout.strip()).name
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

def resolve_projects(reader: ConfigReader, projects: list[str]) -> list[tuple[str, str, str, dict]]:
    """Resolves a list of project tokens (or aliases) into their concrete config representations."""
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

def load_workspace_and_targets(cwd: Path, project_tokens: list[str]) -> tuple[Path, ConfigReader, list[tuple[str, str, str, dict]]]:
    """Finds the workspace, resolves aliases, and returns the target configurations."""
    rc = find_scaffoldrc(cwd)
    ws_str = rc.get("workspace_dir") or "../repos"
    workspace_dir = Path(ws_str).expanduser()
    if not workspace_dir.is_absolute():
        workspace_dir = (cwd / workspace_dir).resolve()

    reader = ConfigReader(cwd, project_name=None, base_templates_dir=None)
    reader.load()
    reader.effective_config["workspace_dir"] = str(workspace_dir)

    original_tokens = list(project_tokens)

    if not project_tokens:
        active_proj = _get_active_git_project(cwd)
        if active_proj:
            print(f"🎯 Auto-detected context: \033[96m{active_proj}\033[0m")
            project_tokens = [active_proj]

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

    targets = resolve_projects(reader, expanded_tokens) if expanded_tokens else []

    # ── THE IN-PLACE FALLBACK FIX ──
    # If we are running with NO explicit projects (in-place), and the registry
    # failed to resolve the auto-detected folder name, we inject a root target.
    # Setting raw_token to `None` signals to the orchestrator to operate on `root`.
    if not targets and not original_tokens:
        active_proj = _get_active_git_project(cwd)
        name = reader.cfg.get("project_name") or active_proj or "Workspace Root"
        proj_slug = reader.cfg.get("project_slug") or slug(active_proj or "root")
        targets = [(name, proj_slug, None, {"url": None})]

    return workspace_dir, reader, targets
