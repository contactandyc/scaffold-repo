# src/scaffold_repo/build_libs.py
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from .config_reader import ConfigReader, _slug, _snake, _deep_merge


def _run(cmd: list[str] | str, *, cwd: Path | None = None, shell: bool = False) -> None:
    if shell:
        print(f"$ (in {cwd or Path.cwd()}) {cmd}")
        subprocess.run(cmd, cwd=cwd, shell=True, check=True, executable="/bin/bash")
    else:
        print("$", " ".join(shlex.quote(c) for c in (cmd if isinstance(cmd, list) else [cmd])))
        subprocess.run(cmd if isinstance(cmd, list) else [cmd], cwd=cwd, check=True)

def _ensure_clone(url: str, dest: Path, *, branch: str | None, shallow: bool | None) -> None:
    if not dest.exists():
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


def _sanitize_steps(steps: list[str]) -> list[str]:
    out: list[str] = []
    for raw in steps or []:
        s = raw.strip()
        if not s:
            continue
        if re.search(r"^\s*git\s+clone\b", s) or re.search(r"^\s*rm\s+-rf\b", s) or re.fullmatch(r"\s*cd\s+/\s*", s):
            continue
        out.append(s)
    return out


def _run_steps_chain(steps: list[str], *, cwd: Path) -> None:
    if not steps:
        return
    prologue = r"""
        set -Eeuo pipefail
        nproc() { (command -v nproc >/dev/null && command nproc) || (sysctl -n hw.ncpu 2>/dev/null) || (getconf _NPROCESSORS_ONLN 2>/dev/null) || echo 4; }
    """
    chain = " && \\\n  ".join(steps)
    script = prologue + "\n" + chain + "\n"
    _run(["bash", "-lc", script], cwd=cwd)


def _compute_git_order(reader: ConfigReader, project_tokens: list[str]) -> tuple[list[str], dict[str, dict]]:
    idx = reader._build_library_index(reader.effective_config)

    target_slugs = []
    by_name_lower = {v["name"].lower(): k for k, v in idx.items()}
    by_snake = {v["snake"]: k for k, v in idx.items()}

    # If no tokens provided, we assume we want to build dependencies for the local repo
    if not project_tokens:
        proj_slug = _slug(reader.effective_config.get("project_name", reader.effective_config.get("project_slug", "")))
        if proj_slug in idx:
            target_slugs.append(proj_slug)
    else:
        for p in project_tokens:
            if p.lower() == "all":
                target_slugs.extend(idx.keys())
                continue

            ns_cands = [s for s, v in idx.items() if str(v.get("raw_key", "")).startswith(f"{p}/")]
            if ns_cands:
                target_slugs.extend(ns_cands)
                continue

            slug = _slug(p)
            key = slug if slug in idx else by_snake.get(_snake(p), by_name_lower.get(p.lower()))
            if key:
                target_slugs.append(key)

    if not target_slugs:
        return [], idx

    transitive_deps = reader._collect_transitive(idx, target_slugs, exclude_roots=False)

    # Filter down to only libraries that actually need compiling from source
    git_slugs = [s for s in transitive_deps if s in idx and str(idx[s]["item"].get("kind")) == "git"]
    ordered = reader._toposort_subset(idx, git_slugs)

    return ordered, idx


def build_all_libs(
        repo: Path,
        *,
        project_tokens: list[str],
        templates_dir: str | None,
) -> None:
    repo = repo.resolve()
    reader = ConfigReader(repo, project_name=None, templates_dir=templates_dir)
    reader.load()

    # --- REMOVED THE REDUNDANT REGISTRY LOOPS FROM HERE ---

    ordered, idx = _compute_git_order(reader, project_tokens)

    workspace_dir = reader.effective_config.get("workspace_dir", "../repos")
    repos_dir = (repo / workspace_dir).resolve()
    repos_dir.mkdir(parents=True, exist_ok=True)

    if not ordered:
        print("— No external git dependencies required for the selected project(s).")
        return

    for slug in ordered:
        lib = idx[slug]["item"]
        name = lib.get("name") or slug
        url = lib.get("url")
        branch = lib.get("branch")
        shallow = lib.get("shallow")

        if not url:
            print(f"— skip {name}: no 'url' for git library")
            continue

        lib_dir_hint = str(lib.get("dir") or "").strip()
        clone_dirname = (lib_dir_hint.split("/", 1)[0] if lib_dir_hint else str(name))
        clone_path = repos_dir / clone_dirname

        print(f"\n=== Building Dependency: {name} ===")
        _ensure_clone(url, clone_path, branch=branch, shallow=shallow)

        steps = [str(s) for s in (lib.get("build_steps") or [])]
        steps = _sanitize_steps(steps)

        if not steps:
            build_src = lib_dir_hint if lib_dir_hint else clone_dirname
            work_dir = repos_dir
            cmds = [
                f'mkdir -p build/{name}',
                f'cd build/{name}',
                f'cmake ../../{build_src}',
                f'cmake --build . -j"$(nproc)"',
                f'sudo cmake --install .'
            ]
            print("• using generic CMake flow")
            _run_steps_chain(cmds, cwd=work_dir)
        else:
            print("• using library build_steps")
            _run_steps_chain(steps, cwd=repos_dir)

        print(f"✅ done: {name}")
