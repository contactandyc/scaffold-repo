# src/scaffold_repo/repo_sync.py
from __future__ import annotations

import sys
import stat
from pathlib import Path
from typing import Any

from .core.config import ConfigReader
from .compliance.licenses import validate_licenses
from .utils.text import sha256

def verify_repo(
        repo: Path,
        *,
        fix_licenses: bool = False,
        no_prompt: bool = False,
        include_exts: set[str] | None = None,
        project_name: str | None = None,
        base_templates_dir: str | None = None,
        assume_yes: bool = False,
        show_diffs: bool = False,
        is_init: bool = False
) -> tuple[int, dict[str, Any]]:
    rc_apply = apply_repo(
        repo,
        project_name=project_name,
        base_templates_dir=base_templates_dir,
        assume_yes=assume_yes,
        show_diffs=show_diffs,
        is_init=is_init
    )

    reader = ConfigReader(repo, project_name=project_name, base_templates_dir=base_templates_dir, is_init=is_init)
    reader.load()

    # Offload the heavily compliance check to the new module
    res = validate_licenses(
        repo,
        cfg=reader.effective_config,
        resource_reader=reader.tmpl_src.read_resource_text,
        include_exts=include_exts,
        apply_fixes=fix_licenses,
        no_prompt=no_prompt,
    )
    exit_code = rc_apply if rc_apply != 0 else (1 if res.get("issues") else 0)
    return exit_code, res

def apply_repo(
        repo: Path,
        *,
        project_name: str | None = None,
        base_templates_dir: str | None = None,
        assume_yes: bool = False,
        show_diffs: bool = False,
        is_init: bool = False
) -> int:
    repo = Path(repo).resolve()

    reader = ConfigReader(repo, project_name=project_name, base_templates_dir=base_templates_dir, is_init=is_init)
    try:
        reader.load()
    except Exception as e:
        print(f"\n❌ Config/template load failed:\n{e}", file=sys.stderr)
        return 3

    planner = reader.get_planner()
    jinja_plan = planner.plan_jinja(show_diffs=show_diffs)
    copy_plan = planner.plan_copy(show_diffs=show_diffs)

    state: dict[str, Any] = {}
    early = [i for i in (jinja_plan + copy_plan) if i.path == ".gitignore" and i.status in ("create", "update")]
    if early:
        print("\nApplying .gitignore early …")
        _apply_items(repo, early, state)
        jinja_plan = [i for i in jinja_plan if i not in early]
        copy_plan  = [i for i in copy_plan  if i not in early]

    _print_summary("Jinja templates (*.j2 → rendered)", jinja_plan)
    _print_summary("Non-Jinja files (verbatim copy)", copy_plan)

    jinja_to_apply = [i for i in jinja_plan if i.status in ("create", "update") and (i.status == "create" or i.updatable)]
    j_updates = [i for i in jinja_to_apply if i.status == "update"]

    if j_updates:
        print("\nJinja updates (batched):")
        for it in j_updates:
            print(f"  • {it.path}")
        if show_diffs:
            for it in j_updates:
                if it.diff:
                    print(f"\n--- diff: {it.path} ---")
                    print(it.diff.rstrip())
        if not assume_yes:
            ans = input("Apply Jinja updates? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                jinja_to_apply = [i for i in jinja_to_apply if i.status == "create"]

    copy_to_apply: list[Any] = []
    for it in copy_plan:
        if it.status == "create":
            copy_to_apply.append(it)
        elif it.status == "update":
            print(f"\nNon-Jinja update: {it.path}")
            if it.diff and show_diffs:
                print(it.diff.rstrip())
            if assume_yes:
                copy_to_apply.append(it)
            else:
                if it.diff and not show_diffs:
                    print(it.diff.rstrip())
                ans = input("Apply this update? [y/N] ").strip().lower()
                if ans in ("y", "yes"):
                    copy_to_apply.append(it)

    _apply_items(repo, jinja_to_apply, state)
    _apply_items(repo, copy_to_apply, state)

    changed = bool(jinja_to_apply or copy_to_apply)

    if changed:
        validate_licenses(
            repo,
            cfg=reader.effective_config,
            resource_reader=reader.tmpl_src.read_resource_text,
            apply_fixes=True,
            no_prompt=True,
        )

    return 0

def _apply_items(repo: Path, items, state: dict) -> None:
    for it in items:
        p = repo / it.path
        if it.status in ("create", "update"):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(it.new_bytes)
            if it.executable:
                mode = p.stat().st_mode
                p.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        files = state.setdefault("files", {})
        files[it.path] = {
            "kind": it.kind,
            "content_sha256": sha256(p.read_bytes()) if p.exists() else sha256(it.new_bytes),
            "template_sha256": it.template_sha256,
            **({"context": it.context_key} if it.kind == "jinja" else {}),
        }

def _print_summary(label: str, plan) -> None:
    by = {"create": [], "update": [], "unchanged": []}
    for i in plan:
        by[i.status].append(i.path)
    total = len(plan)
    print(f"\n{label} (total {total})")
    for k in ("create", "update", "unchanged"):
        if by[k]:
            print(f"  {k:8} {len(by[k]):2}  " + ", ".join(by[k])[:120])
