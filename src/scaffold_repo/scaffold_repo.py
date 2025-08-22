# src/scaffold_repo/scaffold_repo.py
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .repo_sync import verify_repo
from .build_libs import build_all_libs
from .config_reader import ConfigReader, _slug, _snake


def _parse_projects_arg(s: Optional[str]) -> List[str]:
    """Parse comma-separated project names; preserve order, drop dups."""
    if not s or not s.strip():
        return []
    out: List[str] = []
    seen: set[str] = set()
    for tok in (t.strip() for t in s.split(",") if t.strip()):
        key = tok.lower()
        if key not in seen:
            seen.add(key)
            out.append(tok)
    return out


def _resolve_projects(reader: ConfigReader, projects: List[str]) -> List[Tuple[str, str]]:
    """
    Resolve project tokens to (display_name, slug).
    - "all" expands to all libraries with template=git_build (or kind=git),
      ordered by dependencies.
    - Otherwise, tokens match by slug, snake, or case-insensitive name.
    """
    idx = reader._build_library_index(reader.effective_config)
    by_name_lower = {v["name"].lower(): k for k, v in idx.items()}
    by_snake = {v["snake"]: k for k, v in idx.items()}

    def is_git_build(item: dict) -> bool:
        return str(item.get("template")) == "git_build"

    out: List[Tuple[str, str]] = []
    for p in projects:
        if p.lower() == "all":
            cands = [s for s, v in idx.items() if is_git_build(v["item"])]
            ordered = reader._toposort_subset(idx, cands)
            out.extend([(idx[s]["name"], s) for s in ordered])
            continue

        # Try slug, then snake, then case-insensitive name
        slug = _slug(p)
        key = slug if slug in idx else by_snake.get(_snake(p), by_name_lower.get(p.lower()))
        if key:
            out.append((idx[key]["name"], key))

    # Deduplicate while preserving order
    seen: set[str] = set()
    uniq: List[Tuple[str, str]] = []
    for name, slug in out:
        if slug not in seen:
            seen.add(slug)
            uniq.append((name, slug))
    return uniq


def _print_text_result(res: Dict[str, Any]) -> None:
    s = res.get("summary", {})
    print(f"\n\033[1m=== {res['repo']} ===\033[0m")
    print(
        "files_checked: {fc}  headers_added: {ha}  headers_updated: {hu}  unchanged: {un}".format(
            fc=s.get("files_checked", 0),
            ha=s.get("headers_added", 0),
            hu=s.get("headers_updated", 0),
            un=s.get("unchanged", 0),
        )
    )
    if s.get("profiles_used"):
        print(f"profiles_used: {', '.join(s['profiles_used'])}")
    for it in res.get("issues", []):
        kind = it.get("type", "issue")
        if "file" in it:
            print(f"- {kind}: {it['file']}")
        else:
            print(f"- {kind}: {it.get('message', '')}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scaffold-repo",
        description=(
            "Scaffold/refresh repositories from templates and enforce SPDX/NOTICE/licenses.\n"
            "Also supports cloning/building/installing external git libraries."
        ),
    )

    # NOTE: For multi-project scaffolding (list or 'all'), this is treated as the monorepo
    # root under which 'repos/<slug>/' will be used as the destination for each project.
    # For single-project scaffolding, this can be the explicit destination directory.
    ap.add_argument(
        "repo",
        nargs="?",
        type=Path,
        default=Path("."),
        help=(
            "Path to the repo to update (single project) OR the monorepo root for multi-project runs. "
            "When --project is given and no explicit destination is intended, output goes to <repo>/repos/<project-slug>/."
        ),
    )

    # Template/config options
    ap.add_argument(
        "--project",
        type=str,
        default=None,
        help=(
            "Project/library name. Accepts a comma-separated list. "
            "Use 'all' to scaffold every git_build library in dependency order."
        ),
    )
    ap.add_argument(
        "--templates_dir",
        type=Path,
        default=None,
        help="Path to templates_dir (overrides internal templates).",
    )

    # Behavior for applying templates + license verification
    ap.add_argument("--assume-yes", "-y", action="store_true", help="Apply file updates without prompting.")
    ap.add_argument("--show-diffs", action="store_true", help="Show diffs for any updated files.")
    ap.add_argument("--no-prompt", action="store_true", help="Do not prompt when fixing SPDX (non-interactive).")
    ap.add_argument("--notice-file", default="NOTICE", help="NOTICE file name (default: NOTICE).")
    ap.add_argument("--decisions", type=Path, default=None, help="Path to decisions cache (e.g., .license_decisions.json).")
    ap.add_argument("--format", choices=["text", "json"], default="text", help="Output format for results.")

    # Libraries: clone/build/install (merged from scaffold-build-libs)
    grp = ap.add_argument_group("libraries (clone/build/install)")
    phases = grp.add_mutually_exclusive_group()
    phases.add_argument("--libs-clone", action="store_true", help="Clone only the configured git libraries.")
    phases.add_argument("--libs-build", action="store_true", help="Clone + build (no install).")
    phases.add_argument("--libs-install", action="store_true", help="Clone + build + install (default if any --libs-* given).")
    grp.add_argument(
        "--libs-only",
        default=None,
        help="Comma-separated list of library names/slugs to process for --libs-* phases.",
    )

    args = ap.parse_args(argv)

    root = args.repo.resolve()
    tmpl_dir = args.templates_dir.resolve() if args.templates_dir else None

    # Determine if we are scaffolding multiple projects
    project_tokens = _parse_projects_arg(args.project)
    multi = bool(project_tokens) and not (len(project_tokens) == 1 and project_tokens[0].lower() != "all")

    # If --project provided, scaffold each requested project; otherwise run once against 'repo'
    results: List[Dict[str, Any]] = []
    exit_code = 0

    try:
        if project_tokens:
            # Use a reader (at 'root') *only* to resolve project names and dependency order
            reader = ConfigReader(root, project_name=None, templates_dir=(tmpl_dir.as_posix() if tmpl_dir else None))
            reader.load()
            targets = _resolve_projects(reader, project_tokens)
            if not targets:
                print("No matching projects found for --project.", file=sys.stderr)
                return 2

            for name, slug in targets:
                # Destination logic:
                # - If exactly one project AND user passed a non-default repo path → use that path.
                # - Otherwise → write under <root>/repos/<slug>.
                if len(targets) == 1 and args.repo != Path("."):
                    dest = root
                else:
                    dest = root / "repos" / slug

                print(f"\n\033[95m=== Scaffolding {name} ({slug}) into {dest} ===\033[0m")

                dest = dest.resolve()

                code, res = verify_repo(
                    dest,
                    fix_licenses=True,
                    no_prompt=args.no_prompt,
                    notice_file=args.notice_file,
                    decisions_path=args.decisions,
                    project_name=name,  # accepts name/slug/snake; name is friendlier in logs
                    templates_dir=tmpl_dir,
                    assume_yes=args.assume_yes,
                    show_diffs=args.show_diffs,
                )
                exit_code = max(exit_code, code)
                results.append(res)

        else:
            # Single run (no --project): operate directly on 'root'
            code, res = verify_repo(
                root,
                fix_licenses=True,
                no_prompt=args.no_prompt,
                notice_file=args.notice_file,
                decisions_path=args.decisions,
                project_name=None,
                templates_dir=tmpl_dir,
                assume_yes=args.assume_yes,
                show_diffs=args.show_diffs,
            )
            exit_code = max(exit_code, code)
            results.append(res)

        # Optionally run the libraries phase (clone/build/install) in the same CLI
        if args.libs_clone or args.libs_build or args.libs_install:
            do_install = bool(args.libs_install or (not args.libs_clone and not args.libs_build))
            do_build = bool(args.libs_build or do_install)
            do_clone = bool(args.libs_clone or do_build or do_install)
            only_list = [s for s in (args.libs_only.split(",") if args.libs_only else []) if s.strip()]

            # The libs workflow always uses <root>/repos
            build_all_libs(
                repo=root,
                project=None,               # build order is derived from full config
                templates_dir=tmpl_dir,
                do_clone=do_clone,
                do_build=do_build,
                do_install=do_install,
                only=only_list,
            )

    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        return 1

    # Output formatting
    if args.format == "json":
        print(json.dumps({"results": results}, indent=2, ensure_ascii=False))
    else:
        for res in results:
            _print_text_result(res)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
