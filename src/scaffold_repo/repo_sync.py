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
        is_init: bool = False,
        dry_run: bool = False,
        quiet: bool = False
) -> tuple[int, dict[str, Any], bool]:
    rc_apply, apply_changed = apply_repo(
        repo,
        project_name=project_name,
        base_templates_dir=base_templates_dir,
        assume_yes=assume_yes,
        show_diffs=show_diffs,
        is_init=is_init,
        dry_run=dry_run,
        quiet=quiet
    )

    reader = ConfigReader(repo, project_name=project_name, base_templates_dir=base_templates_dir, is_init=is_init)
    reader.load()

    # Offload the heavily compliance check to the new module
    res = validate_licenses(
        repo,
        cfg=reader.effective_config,
        resource_reader=reader.tmpl_src.read_resource_text,
        include_exts=include_exts,
        apply_fixes=(fix_licenses and not dry_run),
        no_prompt=(no_prompt or dry_run),
    )

    lic_issues = len(res.get("issues", [])) > 0
    has_drift = apply_changed or lic_issues
    exit_code = rc_apply if rc_apply != 0 else (1 if lic_issues and not dry_run else 0)

    return exit_code, res, has_drift

def apply_repo(
        repo: Path,
        *,
        project_name: str | None = None,
        base_templates_dir: str | None = None,
        assume_yes: bool = False,
        show_diffs: bool = False,
        is_init: bool = False,
        dry_run: bool = False,
        quiet: bool = False
) -> tuple[int, bool]:
    repo = Path(repo).resolve()

    reader = ConfigReader(repo, project_name=project_name, base_templates_dir=base_templates_dir, is_init=is_init)
    try:
        reader.load()
    except Exception as e:
        if not quiet: print(f"\n❌ Config/template load failed:\n{e}", file=sys.stderr)
        return 3, False

    planner = reader.get_planner()
    jinja_plan = planner.plan_jinja(show_diffs=show_diffs)
    copy_plan = planner.plan_copy(show_diffs=show_diffs)

    state: dict[str, Any] = {}
    early = [i for i in (jinja_plan + copy_plan) if i.path == ".gitignore" and i.status in ("create", "update")]
    if early and not dry_run:
        if not quiet: print("\nApplying .gitignore early …")
        _apply_items(repo, early, state)
        jinja_plan = [i for i in jinja_plan if i not in early]
        copy_plan  = [i for i in copy_plan  if i not in early]

    if not quiet:
        _print_summary("Jinja templates (*.j2 → rendered)", jinja_plan)
        _print_summary("Non-Jinja files (verbatim copy)", copy_plan)

    jinja_to_apply = [i for i in jinja_plan if i.status in ("create", "update") and (i.status == "create" or i.updatable)]
    j_updates = [i for i in jinja_to_apply if i.status == "update"]

    if j_updates and not quiet:
        print("\nJinja updates (batched):")
        for it in j_updates:
            print(f"  • {it.path}")
        if show_diffs:
            for it in j_updates:
                if it.diff:
                    print(f"\n--- diff: {it.path} ---")
                    print(it.diff.rstrip())

    if not assume_yes and not dry_run and j_updates:
        ans = input("Apply Jinja updates? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            jinja_to_apply = [i for i in jinja_to_apply if i.status == "create"]

    copy_to_apply: list[Any] = []
    for it in copy_plan:
        if it.status == "create":
            copy_to_apply.append(it)
        elif it.status == "update":
            if not quiet: print(f"\nNon-Jinja update: {it.path}")
            if it.diff and show_diffs and not quiet:
                print(it.diff.rstrip())
            if assume_yes or dry_run:
                copy_to_apply.append(it)
            else:
                if it.diff and not show_diffs and not quiet:
                    print(it.diff.rstrip())
                ans = input("Apply this update? [y/N] ").strip().lower()
                if ans in ("y", "yes"):
                    copy_to_apply.append(it)

    changed = bool(jinja_to_apply or copy_to_apply)

    if not dry_run:
        _apply_items(repo, jinja_to_apply, state)
        _apply_items(repo, copy_to_apply, state)

    return 0, changed

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
    by = {"create": [], "update": [], "ignored": [], "unchanged": []}
    for i in plan:
        if i.status == "update" and not getattr(i, 'updatable', True):
            by["ignored"].append(i.path)
        else:
            by[i.status].append(i.path)

    total = len(plan)
    print(f"\n{label} (total {total})")
    for k in ("create", "update", "ignored", "unchanged"):
        if by[k]:
            print(f"  {k:8} {len(by[k]):2}  " + ", ".join(by[k])[:120])
