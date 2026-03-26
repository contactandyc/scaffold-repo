# src/scaffold_repo/build_libs.py
from __future__ import annotations

import re
import subprocess
from pathlib import Path
import yaml

from .utils.text import slug
from .utils.collections import coerce_list, dedupe
from .core.config import ConfigReader
from .utils.shell import run_steps_chain
from .utils.git import ensure_clone

def _sanitize_steps(steps: list[str]) -> list[str]:
    out: list[str] = []
    for raw in steps or []:
        s = str(raw).strip()
        if not s: continue
        if "git clone" in s or "rm -rf" in s: continue
        out.append(s)
    return out

def _normalize_dependency(raw_item) -> dict:
    norm = {"stack": "generic", "stack_type": "", "url": None, "revision": None, "is_alias": False, "build_args": [], "env": {}, "shallow": False}
    if isinstance(raw_item, str):
        s = raw_item.strip()
        if "+" in s and "://" not in s.split("+", 1)[0]:
            stack_str, s = s.split("+", 1)
            if "/" in stack_str:
                norm["stack"], norm["stack_type"] = stack_str.split("/", 1)
                norm["stack"] = norm["stack"].lower()
                norm["stack_type"] = norm["stack_type"].lower()
            else:
                norm["stack"] = stack_str.lower()
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

        stack_val = data.get("stack", "generic")
        if "/" in stack_val:
            norm["stack"], norm["stack_type"] = stack_val.split("/", 1)
            norm["stack"] = norm["stack"].lower()
            norm["stack_type"] = norm["stack_type"].lower()
        else:
            norm["stack"] = stack_val.lower()
            norm["stack_type"] = str(data.get("stack_type", "")).lower()

        norm["url"] = data.get("url") or data.get("source") or norm["url"]
        norm["revision"] = data.get("revision") or data.get("tag") or data.get("branch")
        norm["shallow"] = data.get("shallow", False)
        norm["build_args"] = coerce_list(data.get("build_args", []))
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

    raw_deps = coerce_list(local_data.get("depends_on", []))
    for app in local_data.get("apps", {}).values():
        raw_deps.extend(coerce_list(app.get("depends_on", [])))

    resolved_deps = []

    for raw in raw_deps:
        dep = _normalize_dependency(raw)

        if dep["is_alias"]:
            alias_slug = slug(dep["url"])
            if alias_slug in global_registry:
                dep_url = global_registry[alias_slug]["item"].get("url")
                dep_name = global_registry[alias_slug]["name"]
                dep_rev = dep["revision"] or global_registry[alias_slug]["item"].get("branch")
                dep_shallow = global_registry[alias_slug]["item"].get("shallow", False)
            else:
                continue
        else:
            dep_url = dep["url"]
            dep_name = dep_url.split("/")[-1].replace(".git", "")
            dep_rev = dep["revision"]
            dep_shallow = dep.get("shallow", False)

        if not dep_url: continue

        dep_path = workspace_dir / dep_name
        resolved_deps.append(dep_name)

        if not dep_path.exists():
            print(f"📦 Fetching dependency: {dep_name} -> {dep_url}")
            ensure_clone(dep_url, dep_path, branch=dep_rev, shallow=dep_shallow)

        resolve_dependency_graph(dep_path, global_registry, workspace_dir, graph, visited)

    graph[target_slug] = {
        "deps": dedupe(resolved_deps),
        "path": target_repo_path,
        "build_args": []
    }
    return graph

def _toposort_graph(graph: dict) -> list[str]:
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

def execute_build(project_slug: str, target_dir: Path, reg_item: dict, workspace_dir: Path, do_build: bool = True, do_install: bool = True, do_clean: bool = False) -> None:
    stack = reg_item.get("stack", "generic")
    stack_type = reg_item.get("stack_type", "")

    if (target_dir / "build.sh").exists():
        print(f"  • using local build.sh (clean={do_clean}, build={do_build}, install={do_install})")
        cmds = []
        if do_clean:
            cmds.append("./build.sh clean")
        if do_install:
            cmds.append("./build.sh install")
        elif do_build:
            cmds.append("./build.sh build")

        if cmds:
            run_steps_chain(cmds, cwd=target_dir, stack=stack, stack_type=stack_type)

    elif (target_dir / "CMakeLists.txt").exists():
        print(f"  • using smart CMake fallback (clean={do_clean}, build={do_build}, install={do_install})")
        cmds = []
        if do_clean:
            cmds.append("rm -rf build")

        if do_build or do_install:
            extra_cmake_args = []
            raw_steps = coerce_list(reg_item.get("build_steps", []))
            for step in raw_steps:
                if "cmake " in step:
                    args = re.findall(r"-D[^\s]+", step)
                    for a in args:
                        if not a.startswith("-DCMAKE_INSTALL_PREFIX") and not a.startswith("-DCMAKE_BUILD_TYPE"):
                            if a not in extra_cmake_args:
                                extra_cmake_args.append(a)

            args_str = " ".join(extra_cmake_args)
            cmds.extend([
                f'mkdir -p build',
                f'cd build',
                f'cmake .. -DCMAKE_BUILD_TYPE=${{BUILD_TYPE:-Release}} -DCMAKE_INSTALL_PREFIX=${{PREFIX:-/usr/local}} {args_str}',
                f'cmake --build . -j"$(nproc)"'
            ])
            if do_install:
                cmds.append(f'${{SUDO}}cmake --install .')

        if cmds:
            run_steps_chain(cmds, cwd=target_dir, stack=stack, stack_type=stack_type)

    elif reg_item.get("build_steps"):
        print(f"  • using explicit build_steps from registry (clean={do_clean}, build={do_build})")
        raw_steps = coerce_list(reg_item["build_steps"])
        sanitized = _sanitize_steps(raw_steps)

        if not do_build and not do_install:
            sanitized = [s for s in sanitized if "clean" in s or "rm -rf" in s]
        elif not do_clean:
            sanitized = [s for s in sanitized if s.strip() not in ("./build.sh clean", "make clean", "ninja clean") and "rm -rf" not in s]

        if sanitized:
            steps = [s.replace("/usr/local", "${PREFIX:-/usr/local}").replace("sudo ", "${SUDO}") for s in sanitized]
            run_steps_chain(steps, cwd=target_dir, stack=stack, stack_type=stack_type)

    else:
        print(f"  ⚠️  skip {project_slug}: no build.sh, CMakeLists.txt, or build_steps found.")


def build_all_libs(
        repo: Path,
        workspace_dir: Path,
        *,
        project_tokens: list[str],
        base_templates_dir: str | None = None,
        do_clone: bool = True,
        do_build: bool = True,
        do_install: bool = True,
        do_clean: bool = False
) -> None:
    repo = repo.resolve()
    reader = ConfigReader(repo, project_name=None, base_templates_dir=base_templates_dir)
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

    for project_slug in ordered_slugs:
        dep_path = graph[project_slug]["path"]
        print(f"\n=== Processing Dependency: {project_slug} ===")

        if not do_build and not do_clean:
            print(f"✅ fetched: {project_slug}")
            continue

        reg_item = None
        if slug in global_registry:
            reg_item = global_registry[project_slug]["item"]
        else:
            for k, v in global_registry.items():
                if v.get("name") == project_slug or v.get("slug") == project_slug or k.endswith(f"-{project_slug}") or k == project_slug:
                    reg_item = v.get("item")
                    break

        if not reg_item:
            reg_item = {}

        execute_build(project_slug, dep_path, reg_item, workspace_dir, do_build=do_build, do_install=do_install, do_clean=do_clean)

        print(f"✅ done: {project_slug}")
