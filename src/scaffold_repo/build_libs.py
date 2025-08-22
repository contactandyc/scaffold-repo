# src/scaffold_repo/build_libs.py
from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config_reader import ConfigReader  # uses your existing normalization & deps


def _run(cmd: List[str] | str, *, cwd: Path | None = None, shell: bool = False) -> None:
    if shell:
        print(f"$ (in {cwd or Path.cwd()}) {cmd}")
        subprocess.run(cmd, cwd=cwd, shell=True, check=True, executable="/bin/bash")
    else:
        print("$", " ".join(shlex.quote(c) for c in (cmd if isinstance(cmd, list) else [cmd])))
        subprocess.run(cmd if isinstance(cmd, list) else [cmd], cwd=cwd, check=True)

def _ensure_clone(url: str, dest: Path, *, branch: Optional[str], shallow: Optional[bool]) -> None:
    if not dest.exists():
        # Fresh clone
        cmd = ["git", "clone"]
        if shallow is None or shallow is True:
            cmd += ["--depth", "1"]
        if branch:
            cmd += ["--branch", branch, "--single-branch"]
        cmd += [url, str(dest)]
        try:
            _run(cmd)
        except subprocess.CalledProcessError as e:
            if "Repository not found" in str(e) or "not found" in str(e):
                print(f"⚠️  skip {dest.name}: repo {url} not found")
                return
            raise
        return

    # Repo already exists
    try:
        _run(["git", "-C", str(dest), "fetch", "--all", "--tags", "--prune"])
    except subprocess.CalledProcessError as e:
        print(f"⚠️  skip {dest.name}: fetch failed ({e})")
        return

    if branch:
        fetch_cmd = ["git", "-C", str(dest), "fetch", "origin", branch]
        if shallow is None or shallow is True:
            fetch_cmd += ["--depth", "1"]
        try:
            _run(fetch_cmd)
        except subprocess.CalledProcessError as e:
            print(f"⚠️  skip {dest.name}: branch/tag {branch} not found")
            return

        # Try branch first, else fallback to FETCH_HEAD
        try:
            _run(["git", "-C", str(dest), "checkout", "-B", branch, f"origin/{branch}"])
        except subprocess.CalledProcessError:
            try:
                _run(["git", "-C", str(dest), "checkout", "-B", branch, "FETCH_HEAD"])
            except subprocess.CalledProcessError as e:
                print(f"⚠️  skip {dest.name}: cannot checkout {branch} ({e})")
                return
    else:
        try:
            _run(["git", "-C", str(dest), "pull", "--ff-only"])
        except subprocess.CalledProcessError as e:
            print(f"⚠️  skip {dest.name}: pull failed ({e})")


def _sanitize_steps(steps: List[str], phase: str) -> List[str]:
    """
    Use library-provided build_steps but strip unsafe/irrelevant bits so they
    run inside our ./repos workspace and can be split by phase.
    """
    out: List[str] = []
    for raw in steps or []:
        s = raw.strip()
        if not s:
            continue
        # Always skip clone/removal/absolute cd; we manage clones & keep repos
        if re.search(r"^\s*git\s+clone\b", s):
            continue
        if re.search(r"^\s*rm\s+-rf\b", s):
            continue
        if re.fullmatch(r"\s*cd\s+/\s*", s):
            continue

        # Build-only: skip install lines; flip build.sh install → build
        if phase == "build":
            if re.search(r"(?:^|&&)\s*(?:sudo\s+)?make\s+install\b", s):
                continue
            if re.search(r"\bcmake\s+--install\b", s):
                continue
            s = re.sub(r"(\./build\.sh)\s+install\b", r"\1 build", s)

        out.append(s)
    return out


def _run_steps_chain(steps: List[str], *, cwd: Path) -> None:
    if not steps:
        return
    # Provide nproc fallback for macOS/BSD while keeping existing scripts happy.
    prologue = r"""
        set -Eeuo pipefail
        nproc() { (command -v nproc >/dev/null && command nproc) || (sysctl -n hw.ncpu 2>/dev/null) || (getconf _NPROCESSORS_ONLN 2>/dev/null) || echo 4; }
    """
    chain = " && \\\n  ".join(steps)
    script = prologue + "\n" + chain + "\n"
    _run(["bash", "-lc", script], cwd=cwd)


def _compute_git_order(reader: ConfigReader) -> Tuple[List[str], Dict[str, dict]]:
    """
    Returns (ordered_slugs, idx) for libraries with kind=git,
    using reader's dependency resolution/toposort.
    """
    idx = reader._build_library_index(reader.effective_config)  # internal but stable for our package
    subset = [s for s, v in idx.items() if str(v["item"].get("kind")) == "git"]
    ordered = reader._toposort_subset(idx, subset)
    return ordered, idx


def build_all_libs(
    repo: Path,
    *,
    project: Optional[str],
    templates_dir: Optional[Path],
    do_clone: bool,
    do_build: bool,
    do_install: bool,
    only: Optional[List[str]] = None,
) -> None:
    repo = repo.resolve()
    reader = ConfigReader(repo, project_name=project, templates_dir=(templates_dir.as_posix() if templates_dir else None))
    reader.load()

    ordered, idx = _compute_git_order(reader)

    # Filter by --only names if provided (accept name or slug)
    only_set = {x.strip().lower() for x in (only or [])}
    if only_set:
        def _ok(slug: str) -> bool:
            name = str(idx[slug]["name"]).lower()
            return name in only_set or slug in only_set
        ordered = [s for s in ordered if _ok(s)]

    repos_dir = repo / "repos"
    repos_dir.mkdir(parents=True, exist_ok=True)

    # Phase semantics:
    # - --install implies clone + build + install (even if --clone/--build omitted)
    # - --build implies clone + build
    # - --clone alone only clones
    phase_install = do_install
    phase_build = do_install or do_build
    phase_clone = do_install or do_build or do_clone

    for slug in ordered:
        lib = idx[slug]["item"]
        name = lib.get("name") or slug
        url = lib.get("url")
        branch = lib.get("branch")
        shallow = lib.get("shallow")
        if not url:
            print(f"— skip {name}: no 'url' for git library")
            continue

        # Choose clone directory:
        # - If 'dir' is provided (e.g., 'restinio/dev'), clone to first segment 'restinio'
        # - Else use the library name
        lib_dir_hint = str(lib.get("dir") or "").strip()
        clone_dirname = (lib_dir_hint.split("/", 1)[0] if lib_dir_hint else str(name))
        clone_path = repos_dir / clone_dirname

        print(f"\n=== {name} ===")
        if phase_clone:
            _ensure_clone(url, clone_path, branch=branch, shallow=shallow)

        if phase_build or phase_install:
            steps = [str(s) for s in (lib.get("build_steps") or [])]
            steps = _sanitize_steps(steps, phase=("install" if phase_install else "build"))

            if not steps:
                # Generic CMake fallback (dir hint can point inside the clone)
                build_src = lib_dir_hint if lib_dir_hint else clone_dirname
                work_dir = repos_dir
                cmds = [
                    f'mkdir -p build/{name}',
                    f'cd build/{name}',
                    f'cmake ../../{build_src}',
                    f'cmake --build . -j"$(nproc)"',
                ]
                if phase_install:
                    cmds.append("sudo cmake --install .")
                print("• using generic CMake flow")
                _run_steps_chain(cmds, cwd=work_dir)
            else:
                # Run the library-provided script, sanitized, from ./repos
                print("• using library build_steps")
                _run_steps_chain(steps, cwd=repos_dir)

        print(f"✅ done: {name}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scaffold-repo-libs",
        description="Clone/build/install all git libraries from scaffold config in dependency order.",
    )
    ap.add_argument("repo", nargs="?", type=Path, default=Path("."), help="Repository root containing templates/config")
    ap.add_argument("--project", type=str, default=None, help="Project/library overlay name (same as scaffold-repo)")
    ap.add_argument("--templates_dir", type=Path, default=None, help="Override templates_dir (same as scaffold-repo)")

    phases = ap.add_mutually_exclusive_group()
    phases.add_argument("--clone", action="store_true", help="Clone only")
    phases.add_argument("--build", action="store_true", help="Clone + build (no install)")
    phases.add_argument("--install", action="store_true", help="Clone + build + install (default)")

    ap.add_argument(
        "--only",
        help="Comma-separated list of library names/slugs to process (others are skipped)",
        default=None,
    )

    args = ap.parse_args(argv)

    do_clone = bool(args.clone)
    do_build = bool(args.build)
    do_install = bool(args.install or (not args.clone and not args.build))  # default: install

    only_list = [s for s in (args.only.split(",") if args.only else []) if s.strip()]
    try:
        build_all_libs(
            repo=args.repo,
            project=args.project,
            templates_dir=args.templates_dir,
            do_clone=do_clone,
            do_build=do_build,
            do_install=do_install,
            only=only_list,
        )
        return 0
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Command failed (exit {e.returncode}). See output above.", file=sys.stderr)
        return e.returncode
    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
