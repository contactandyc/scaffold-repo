# src/scaffold_repo/build_libs.py
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
import yaml

from .config_reader import ConfigReader, _slug, _snake, _deep_merge, _coerce_list, _dedupe

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
        if shallow is None or shallow is True: cmd += ["--depth", "1"]
        if branch: cmd += ["--branch", branch, "--single-branch"]
        cmd += [url, str(dest)]
        try: _run(cmd)
        except subprocess.CalledProcessError as e:
            if "Repository not found" in str(e) or "not found" in str(e):
                print(f"⚠️  skip {dest.name}: repo {url} not found")
                return
            raise
        return

    try: _run(["git", "-C", str(dest), "fetch", "--all", "--tags", "--prune"])
    except subprocess.CalledProcessError as e:
        print(f"⚠️  skip {dest.name}: fetch failed ({e})")
        return

    if branch:
        fetch_cmd = ["git", "-C", str(dest), "fetch", "origin", branch]
        if shallow is None or shallow is True: fetch_cmd += ["--depth", "1"]
        try: _run(fetch_cmd)
        except subprocess.CalledProcessError as e:
            print(f"⚠️  skip {dest.name}: branch/tag {branch} not found")
            return
        try: _run(["git", "-C", str(dest), "checkout", "-B", branch, f"origin/{branch}"])
        except subprocess.CalledProcessError:
            try: _run(["git", "-C", str(dest), "checkout", "-B", branch, "FETCH_HEAD"])
            except subprocess.CalledProcessError as e:
                print(f"⚠️  skip {dest.name}: cannot checkout {branch} ({e})")
                return
    else:
        try: _run(["git", "-C", str(dest), "pull", "--ff-only"])
        except subprocess.CalledProcessError as e:
            print(f"⚠️  skip {dest.name}: pull failed ({e})")

def _sanitize_steps(steps: list[str]) -> list[str]:
    out: list[str] = []
    for raw in steps or []:
        s = raw.strip()
        if not s: continue
        if re.search(r"^\s*git\s+clone\b", s) or re.search(r"^\s*rm\s+-rf\b", s) or re.fullmatch(r"\s*cd\s+/\s*", s): continue
        out.append(s)
    return out

def _run_steps_chain(steps: list[str], *, cwd: Path) -> None:
    if not steps: return
    prologue = r"""
        set -Eeuo pipefail
        nproc() { (command -v nproc >/dev/null && command nproc) || (sysctl -n hw.ncpu 2>/dev/null) || (getconf _NPROCESSORS_ONLN 2>/dev/null) || echo 4; }
    """
    chain = " && \\\n  ".join(steps)
    script = prologue + "\n" + chain + "\n"
    _run(["bash", "-lc", script], cwd=cwd)

def _normalize_dependency(raw_item) -> dict:
    """Normalizes a dependency from either a shorthand string (flavor+url@rev) or a dict."""
    norm = {"flavor": "cmake", "url": None, "revision": None, "is_alias": False, "build_args": [], "env": {}}

    if isinstance(raw_item, str):
        s = raw_item.strip()
        if "+" in s and "://" not in s.split("+", 1)[0]:
            flavor, s = s.split("+", 1)
            norm["flavor"] = flavor.lower()
        if "@" in s:
            s, rev = s.rsplit("@", 1)
            norm["revision"] = rev
        norm["url"] = s
        if not s.startswith(("http://", "https://", "git@")):
            norm["is_alias"] = True

    elif isinstance(raw_item, dict):
        data = raw_item
        if len(raw_item) == 1:
            key = next(iter(raw_item))
            if isinstance(raw_item[key], dict):
                data = raw_item[key]
                if key.startswith(("http://", "https://", "git@")):
                    norm["url"] = key

        norm["flavor"] = data.get("flavor", "cmake")
        norm["url"] = data.get("url") or data.get("source") or norm["url"]
        norm["revision"] = data.get("revision") or data.get("tag") or data.get("branch")
        norm["build_args"] = _coerce_list(data.get("build_args", []))
        norm["env"] = data.get("env", {})
        if norm["url"] and not norm["url"].startswith(("http://", "https://", "git@")):
            norm["is_alias"] = True
    else:
        raise ValueError(f"Invalid dependency format: {type(raw_item)}")
    return norm


def resolve_dependency_graph(
        target_repo_path: Path,
        global_registry: dict,
        workspace_dir: Path,
        graph: dict | None = None,
        visited: set | None = None
) -> dict:
    """Recursively parses scaffold.yaml files, clones missing repos, and builds a dependency map."""
    if graph is None: graph = {}
    if visited is None: visited = set()

    target_slug = target_repo_path.name
    if target_slug in visited:
        return graph
    visited.add(target_slug)

    manifest_path = target_repo_path / "scaffold.yaml"
    local_data = {}
    if manifest_path.exists():
        try:
            local_data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except Exception:
            pass

    # Extract dependencies from local truth
    raw_deps = _coerce_list(local_data.get("depends_on", []))
    for app in local_data.get("apps", {}).values():
        raw_deps.extend(_coerce_list(app.get("depends_on", [])))

    resolved_deps = []

    for raw in raw_deps:
        dep = _normalize_dependency(raw)

        # 1. Resolve Name and URL
        if dep["is_alias"]:
            idx = global_registry
            alias_slug = _slug(dep["url"])
            if alias_slug in idx:
                dep_url = idx[alias_slug]["item"].get("url")
                dep_name = idx[alias_slug]["name"]
                dep_rev = dep["revision"] or idx[alias_slug]["item"].get("branch")
            else:
                continue # System dependency or not found
        else:
            dep_url = dep["url"]
            # Extract folder name from URL (e.g. 'restinio' from '.../restinio.git')
            dep_name = dep_url.split("/")[-1].replace(".git", "")
            dep_rev = dep["revision"]

        if not dep_url: continue

        dep_path = workspace_dir / dep_name
        resolved_deps.append(dep_name)

        # 2. Clone if missing
        if not dep_path.exists():
            print(f"📦 Fetching dependency: {dep_name} -> {dep_url}")
            _ensure_clone(dep_url, dep_path, branch=dep_rev, shallow=True)

        # 3. Recurse into the dependency
        resolve_dependency_graph(dep_path, global_registry, workspace_dir, graph, visited)

    graph[target_slug] = {
        "deps": _dedupe(resolved_deps),
        "path": target_repo_path,
        "build_args": [] # Could store overrides here
    }
    return graph

def _toposort_graph(graph: dict) -> list[str]:
    """Topologically sort the resolved graph."""
    indeg = {s: 0 for s in graph}
    adj = {s: [] for s in graph}
    for s, info in graph.items():
        for d in info["deps"]:
            if d in graph:
                indeg[s] += 1
                adj[d].append(s)

    queue = [s for s in graph if indeg[s] == 0]
    ordered = []
    while queue:
        n = queue.pop(0)
        ordered.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    return ordered

def build_all_libs(
        repo: Path,
        workspace_dir: Path, # Explicitly receive the absolute path
        *,
        project_tokens: list[str],
        templates_dir: str | None,
        do_clone: bool = True,
        do_build: bool = True,
        do_install: bool = True
) -> None:
    repo = repo.resolve()
    reader = ConfigReader(repo, project_name=None, templates_dir=templates_dir)
    reader.load()

    reader.effective_config["workspace_dir"] = str(workspace_dir)

    global_registry = reader._build_library_index(reader.effective_config)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    print("\n🔍 Resolving dependency graph from local manifests...")
    graph = resolve_dependency_graph(repo, global_registry, workspace_dir)

    ordered_slugs = _toposort_graph(graph)
    if repo.name in ordered_slugs:
        ordered_slugs.remove(repo.name)

    if not ordered_slugs:
        print("— No external dependencies required.")
        return

    for slug in ordered_slugs:
        dep_path = graph[slug]["path"]
        print(f"\n=== Processing Dependency: {slug} ===")

        manifest_path = dep_path / "scaffold.yaml"
        local_data = {}
        if manifest_path.exists():
            try:
                local_data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            except Exception: pass

        steps = [str(s) for s in (local_data.get("build_steps") or [])]
        steps = _sanitize_steps(steps)

        if not steps and do_build:
            # FLAVOR FALLBACK
            cmds = [
                f'mkdir -p build',
                f'cd build',
                f'cmake .. -DCMAKE_BUILD_TYPE=Release',
                f'cmake --build . -j"$(nproc)"',
            ]
            if do_install:
                cmds.append(f'sudo cmake --install .')

            print("• using generic CMake fallback")
            _run_steps_chain(cmds, cwd=dep_path)
        elif steps and do_build:
            print("• using local build_steps")
            _run_steps_chain(steps, cwd=dep_path)

        print(f"✅ done: {slug}")