# src/scaffold_repo/core/config.py
from __future__ import annotations

import posixpath
import sys
import os
import glob
from pathlib import Path
from typing import Any, Iterable

import yaml
from jinja2 import Environment, StrictUndefined

from ..utils.collections import coerce_list, deep_merge, dedupe
from ..utils.text import slug, snake, camel
from ..utils.git import sync_git_template_repo
from ..templating.source import TemplateSource
from ..templating.planner import TemplatePlanner
from ..cli.workspace import find_scaffoldrc

def _extract_dep_name(raw_dep: Any) -> str:
    """
    Parses a dependency definition (which could be a URL, a git SSH string, a dictionary,
    or a plain string) and extracts a clean, base name for the dependency.
    """
    # If the dependency is a dictionary, try to extract the URL or source string
    if isinstance(raw_dep, dict):
        if len(raw_dep) == 1:
            k = next(iter(raw_dep))
            # Handle cases where the key itself is the URL
            if k.startswith(("http://", "https://", "git@")): raw_dep = k
        if isinstance(raw_dep, dict):
            raw_dep = raw_dep.get("url") or raw_dep.get("source") or ""

    s = str(raw_dep).strip()

    # Strip out protocols and version tags to isolate the package name
    if "+" in s and "://" not in s.split("+", 1)[0]: s = s.split("+", 1)[1]
    if "@" in s: s = s.rsplit("@", 1)[0]

    # If it's a remote URL, extract the final path segment and drop the .git extension
    if s.startswith(("http://", "https://", "git@")): return s.split("/")[-1].replace(".git", "")
    return s

class ConfigReader:
    """
    Responsible for discovering, loading, merging, and normalizing configuration files
    (scaffold.yaml, registry files, included templates) for a project workspace.
    """
    def __init__(self, repo: Path, *, project_name: str | None = None, base_templates_dir: str | None = None, is_init: bool = False):
        self.repo = repo.resolve()
        self.cfg: dict = {}
        self.project_name: str | None = project_name
        self.is_init = is_init

        # Resolve the base directory for templates, handling relative/absolute paths
        base_dir = None
        if base_templates_dir:
            base_dir = Path(base_templates_dir)
            if not base_dir.is_absolute(): base_dir = (self.repo / base_dir)
            base_dir = base_dir.resolve()

        self.tmpl_src = TemplateSource(base_dir=base_dir, pkg_rel="templates")
        self.enabled_packages: set[str] = set()
        self.package_patterns: dict[str, list[str]] = {}

    @property
    def effective_config(self) -> dict:
        """Returns the fully parsed and merged configuration dictionary."""
        return self.cfg

    def get_planner(self) -> TemplatePlanner:
        """
        Instantiates a TemplatePlanner using the current loaded configuration
        and enabled packages to dictate which files get generated.
        """
        self.cfg["package_patterns"] = self.package_patterns
        self.cfg["enabled_packages"] = self.enabled_packages
        return TemplatePlanner(self.repo, self.tmpl_src, self.cfg, self.is_init)

    def load(self) -> None:
        """
        The main orchestration method. It finds the local configuration, handles remote
        template fetching, merges various YAML registries, resolves includes, and
        normalizes the data into a final usable state.
        """
        local_manifest = self.repo / "scaffold.yaml"
        local_data = {}

        # Locate global/workspace config (.scaffoldrc)
        rc_cfg = find_scaffoldrc(self.repo)
        ws_dir = Path(rc_cfg.get("workspace_dir") or self.repo.parent).resolve()

        url = None
        ref = "main"
        custom_path = None

        # Attempt to read the project's local scaffold.yaml
        if local_manifest.exists():
            try:
                local_data = yaml.safe_load(local_manifest.read_text(encoding="utf-8")) or {}
            except Exception as e:
                print(f"Warning: Failed to parse local {local_manifest}:\n{e}", file=sys.stderr)

            # Check if local config specifies a remote template repository to inherit from
            base_tmpl = local_data.get("base_templates")
            if isinstance(base_tmpl, dict) and "repo" in base_tmpl:
                url = base_tmpl["repo"]
                ref = base_tmpl.get("ref", "main")
                custom_path = base_tmpl.get("path")

        # Fallback to workspace/rc config if local doesn't specify a remote registry
        if not url and rc_cfg.get("template_registry_url"):
            url = rc_cfg.get("template_registry_url")
            ref = rc_cfg.get("template_registry_ref", "main")

        # Sync remote templates if a URL is provided
        if url:
            cached_dir = sync_git_template_repo(url, ref, ws_dir)
            if cached_dir:
                if custom_path:
                    new_base = cached_dir / custom_path
                    if not new_base.is_dir():
                        print(f"Warning: base_templates path '{custom_path}' not found in {url}@{ref}. Falling back to root.", file=sys.stderr)
                        new_base = cached_dir
                else:
                    new_base = cached_dir / "templates" if (cached_dir / "templates").is_dir() else cached_dir

                self.tmpl_src = TemplateSource(base_dir=new_base)

        # Load default configuration from the template source
        self.cfg = self.tmpl_src.load_defaults_yaml() or {}

        if rc_cfg.get("workspace_dir"):
            self.cfg["workspace_dir"] = rc_cfg["workspace_dir"]

        # Deep merge libraries, apps, and licenses from the template registry
        for f in self.tmpl_src.find_registry_yamls("libraries"):
            data = self.tmpl_src._load_logical_path(f)
            if data and "libraries" in data:
                self.cfg.setdefault("libraries", {})
                self.cfg["libraries"] = deep_merge(self.cfg["libraries"], data["libraries"])

        for f in self.tmpl_src.find_registry_yamls("apps"):
            data = self.tmpl_src._load_logical_path(f)
            if data and "apps" in data:
                self.cfg.setdefault("apps", {})
                self.cfg["apps"] = deep_merge(self.cfg["apps"], data["apps"])

        for f in self.tmpl_src.find_registry_yamls("licenses"):
            data = self.tmpl_src._load_logical_path(f)
            if data and "licenses" in data:
                self.cfg.setdefault("licenses", {})
                self.cfg["licenses"] = deep_merge(self.cfg["licenses"], data["licenses"])

        # Determine the primary project scope based on matched names
        self._select_project()

        # Handle 'includes' defined in the local scaffold.yaml
        if local_data:
            raw_includes = local_data.pop("includes", [])
            if not isinstance(raw_includes, list): raw_includes = [raw_includes]

            base = {}
            for inc in raw_includes:
                if isinstance(inc, str):
                    inc = {"source": inc}

                source = inc.get("source") or inc.get("repo")
                if not source: continue

                ref = inc.get("ref") or inc.get("branch") or inc.get("tag")
                file_path = inc.get("file")
                include_keys = coerce_list(inc.get("include", []))
                exclude_keys = coerce_list(inc.get("exclude", []))

                inc_data = {}
                # Fetch remote yaml includes or load local logical paths
                if source.startswith(("http://", "https://")):
                    from ..templating.source import _fetch_remote_yaml
                    inc_data = _fetch_remote_yaml(source, ref=ref, file_path=file_path)
                else:
                    inc_str = str(source)
                    if not inc_str.endswith((".yaml", ".yml")):
                        inc_str += ".yaml"
                    inc_data = self.tmpl_src._load_logical_path(inc_str)

                # Filter included data based on include/exclude keys
                if isinstance(inc_data, dict):
                    if include_keys:
                        inc_data = {k: v for k, v in inc_data.items() if k in include_keys}
                    if exclude_keys:
                        inc_data = {k: v for k, v in inc_data.items() if k not in exclude_keys}

                base = deep_merge(base, inc_data)

            # Merge includes, then apply local data on top
            self.cfg = deep_merge(self.cfg, base)
            self.cfg = deep_merge(self.cfg, local_data)

            # Apply "stack" defaults (e.g., C++, Python, generic base config)
            raw_stack = str(self.cfg.get("stack") or "").strip()
            if raw_stack:
                st = raw_stack.split("/")[0].lower()
                st_type = raw_stack.split("/")[1].lower() if "/" in raw_stack else "base"
                stack_defaults = self.tmpl_src.get_stacked_defaults(f"stacks/{st}/{st_type}/_")
                self.cfg = deep_merge(stack_defaults, self.cfg)

            # This catches `profile` whether it was defined in the leaf repo OR the stack defaults
            prof = self.cfg.get("profile")
            if prof:
                prof_file = f"{prof}.yaml" if not str(prof).endswith((".yaml", ".yml")) else str(prof)
                prof_data = self.tmpl_src._load_logical_path(f"profiles/{prof_file}")
                if prof_data:
                    # Merge the profile underneath the current config, so local overrides always win
                    self.cfg = deep_merge(prof_data, self.cfg)
            # -----------------------------------------------------

            # Establish standardized string formats for the project name (slug, snake_case, camelCase)
            nm = self.project_name or local_data.get("project_name") or local_data.get("project_title") or self.repo.name
            self.cfg["project_name"] = nm
            self.cfg["project_slug"] = slug(nm)
            self.cfg["project_snake"] = local_data.get("project_snake") or snake(nm) or nm
            self.cfg["project_camel"] = local_data.get("project_camel") or camel(nm)

            # Ensure the current project is registered in the libraries dict
            self.cfg.setdefault("libraries", {})
            lib_entry = dict(local_data)
            lib_entry["name"] = nm
            self.cfg["libraries"][self.cfg["project_slug"]] = lib_entry

        # Run final data normalization passes
        self._render_contributors()
        self._normalize_keys_autofill()
        self._expand_library_templates()
        self._augment_with_libraries_tests_apps()
        self._compute_package_switches()

    def _render_contributors(self) -> None:
        """
        Evaluates Jinja2 template strings within the 'contributors' dictionary
        so properties can dynamically reference other config values.
        """
        contribs = self.cfg.get("contributors") or {}
        if not isinstance(contribs, dict) or not contribs: return
        env = Environment(undefined=StrictUndefined, autoescape=False)
        def render_fields(fields: dict) -> dict:
            ctx = {**self.cfg, **fields}
            out = {}
            for k, v in fields.items():
                if isinstance(v, str):
                    try: out[k] = env.from_string(v).render(**ctx)
                    except Exception: out[k] = v
                else: out[k] = v
            return out
        self.cfg["contributors"] = {key: (render_fields(val) if isinstance(val, dict) else val) for key, val in contribs.items()}

    def _select_project(self) -> None:
        """
        Identifies if the provided project_name matches a predefined library or app
        in the registries, and if so, merges that specific component's config into the root.
        """
        proj = self.project_name or self.cfg.get("project_name")
        if not proj: return
        proj_str = str(proj).strip()
        proj_base = posixpath.basename(proj_str)

        # Handle direct path references
        if "/" in proj_str:
            for pth in [f"libraries/{proj_str}.yaml", f"apps/{proj_str}.yaml"]:
                extra_data = self.tmpl_src._load_logical_path(pth)
                if extra_data:
                    self.cfg = deep_merge(self.cfg, extra_data)
                    break

        want_slug, want_snake = slug(proj_base), snake(proj_base)
        picked = None

        # Search the registries for a match against the slug/snake representations
        for category in ["libraries", "apps"]:
            items = self.cfg.get(category) or {}
            iterable_items = items.items() if isinstance(items, dict) else enumerate(items)
            for key, item in iterable_items:
                if isinstance(item, dict):
                    nm = str(item.get("name") or key).strip()
                    if nm and (nm == proj_base or slug(nm) == want_slug or snake(nm) == want_snake):
                        picked = item
                        break
            if picked: break

        if not picked: return
        self.cfg = deep_merge(self.cfg, picked)
        if not self.cfg.get("project_name"): self.cfg["project_name"] = picked.get("name") or proj_base

    def _normalize_keys_autofill(self) -> None:
        """
        Fills in missing configuration values. Auto-discovers source/test files
        on the filesystem, or performs a 'virtual glob' of the Jinja templates
        if the files haven't been generated yet.
        """
        # 1. Normalize the technology stack notation
        raw_stack = str(self.cfg.get("stack") or "").strip()
        if "/" in raw_stack:
            st, st_type = raw_stack.split("/", 1)
            self.cfg["stack"] = st.lower()
            if not self.cfg.get("stack_type"):
                self.cfg["stack_type"] = st_type.lower()
        elif raw_stack:
            self.cfg["stack"] = raw_stack.lower()

        if not self.cfg.get("stack_type"):
            self.cfg["stack_type"] = "base"

        # --- THE VIRTUAL GLOBBER ---
        def _virtual_glob(target_dir: str, valid_exts: set[str]) -> list[str]:
            """Peers into the template headers to predict what files will be created."""
            import re
            import yaml
            from jinja2 import Environment
            found = []
            st, st_type = self.cfg.get("stack"), self.cfg.get("stack_type")
            valid_prefixes = (f"stacks/{st}/{st_type}/", f"stacks/{st}/base/") if st else ()

            for rel, data, is_j2, _ in self.tmpl_src.iter_files():
                if not is_j2 or not valid_prefixes or not rel.startswith(valid_prefixes):
                    continue

                text = data.decode("utf-8", errors="ignore")
                m = re.match(r"^\s*\{#-?\s*(.*?)\s*-?#\}\s*", text, re.S)
                dest = ""

                # Extract dest from Jinja header
                if m:
                    try:
                        meta = yaml.safe_load(m.group(1))
                        if isinstance(meta, dict):
                            meta = meta.get("scaffold-repo", meta.get("scaffold_repo", meta))
                            if isinstance(meta, dict):
                                dest = meta.get("dest", "")
                    except Exception: pass

                # Fallback: calculate dest from relative path if no header exists
                if not dest:
                    for pfx in valid_prefixes:
                        if rel.startswith(pfx):
                            dest = rel[len(pfx):-3] # strip prefix and .j2
                            break

                if dest.startswith(target_dir) and any(dest.endswith(e) for e in valid_exts):
                    try:
                        rendered = Environment().from_string(dest).render(**self.cfg)
                        found.append(rendered)
                    except Exception: pass
            return found
        # ---------------------------

        # 2. Auto-discover library sources
        dp = dict(self.cfg.get("deps") or {})
        ts = dict(self.cfg.get("tests") or {})
        ps = self.cfg.get("project_snake") or "project"

        lib_srcs = self.cfg.get("library_sources") or dp.get("sources")
        if not lib_srcs:
            exts = {".c", ".cc", ".cpp", ".cxx"}
            src_root = self.repo / "src"
            if src_root.exists():
                auto = [p.relative_to(self.repo).as_posix() for p in src_root.rglob("*") if p.is_file() and p.suffix.lower() in exts]
            else:
                # 🔮 Predict the future using templates!
                auto = _virtual_glob("src/", exts)

            if auto: lib_srcs = sorted(auto)

        norm_srcs = [str(s) for s in (lib_srcs if isinstance(lib_srcs, (list, tuple)) else [lib_srcs])] if lib_srcs else []
        self.cfg["library_sources"] = dp["sources"] = norm_srcs

        # 3. Auto-discover test targets
        test_tgts = self.cfg.get("test_targets") or ts.get("test_targets") or ts.get("targets")
        if not test_tgts:
            tests_src = self.repo / "tests" / "src"
            exts = {".c", ".cc", ".cpp", ".cxx"}
            if tests_src.exists():
                auto = [{"name": p.stem, "sources": [f"src/{p.name}"]} for p in tests_src.glob("test_*") if p.is_file() and p.suffix.lower() in exts]
            else:
                # 🔮 Predict the future using templates!
                auto = [{"name": Path(p).stem, "sources": [p.replace("tests/", "", 1)]} for p in _virtual_glob("tests/src/", exts)]

            test_tgts = sorted(auto, key=lambda t: t["name"]) if auto else []

        ts["targets"] = test_tgts or []
        self.cfg["deps"], self.cfg["tests"] = dp, ts

        # 4. Normalize dates and calculate year spans for copyrights
        # ... (The rest of the method remains exactly the same from here down!) ...
        import datetime
        current_year = str(self.cfg.get("date") or "")[:4]
        self.cfg["year"] = current_year
        raw_created = str(self.cfg.get("date_created") or "")
        default_start_year = raw_created[:4] if len(raw_created) >= 4 else current_year

        env = Environment(undefined=StrictUndefined, autoescape=False)
        def _render_val(val):
            if isinstance(val, str) and "{{" in val:
                try: return env.from_string(val).render(**self.cfg)
                except Exception as e:
                    print(f"\n⚠️ Warning: Failed to double-render '{val}': {e}", file=sys.stderr)
                    return val
            return val

        def _resolve_entity_ref(val):
            # Helper to allow configs to reference common identities (e.g., from 'contributors')
            if isinstance(val, dict): return dict(val)
            if isinstance(val, str):
                if not " " in val and "." in val and not "{{" in val:
                    parts = val.split(".")
                    curr = self.cfg
                    for p in parts:
                        if isinstance(curr, dict) and p in curr: curr = curr[p]
                        else: return {"entity": val}
                    if isinstance(curr, dict): return dict(curr)
                return {"entity": val}
            return {}

        # 5. Build standard copyright dictionaries
        raw_copyrights = self.cfg.get("copyrights", [])
        norm_copyrights = []
        for raw_cp in (raw_copyrights if isinstance(raw_copyrights, list) else [raw_copyrights]):
            norm_cp = _resolve_entity_ref(raw_cp)
            if not norm_cp: continue

            if "entity" not in norm_cp and "contact" in norm_cp:
                norm_cp["entity"] = norm_cp["contact"]

            norm_cp["entity"] = _render_val(norm_cp.get("entity", ""))
            norm_cp["full_entity"] = _render_val(norm_cp.get("full_entity", norm_cp.get("entity", "")))

            cp_start = str(norm_cp.get("start_year") or default_start_year)
            if cp_start < default_start_year:
                cp_start = default_start_year

            end_year = str(norm_cp.get("end_year") or current_year)
            norm_cp["year_span"] = f"{cp_start}–{end_year}" if cp_start and cp_start != end_year else end_year

            norm_copyrights.append(norm_cp)
        self.cfg["copyrights"] = norm_copyrights

        # 6. Resolve contact identities and roles
        raw_contacts = coerce_list(self.cfg.get("contacts", []))
        norm_contacts = []
        for raw_c in raw_contacts:
            norm_c = _resolve_entity_ref(raw_c)
            if not norm_c: continue

            if isinstance(norm_c.get("entity"), str) and norm_c["entity"].startswith("contributors."):
                resolved = _resolve_entity_ref(norm_c["entity"])
                if resolved:
                    role = norm_c.get("role")
                    norm_c = dict(resolved)
                    if role: norm_c["role"] = role

            if "entity" not in norm_c and "contact" in norm_c:
                norm_c["entity"] = norm_c["contact"]

            norm_c["role"] = _render_val(norm_c.get("role", "Maintainer"))
            norm_c["entity"] = _render_val(norm_c.get("entity", ""))
            norm_contacts.append(norm_c)
        self.cfg["contacts"] = norm_contacts

    def _expand_library_templates(self) -> None:
        """
        Applies shared library templates to individual library configurations.
        If a library sets `template: <name>`, it merges the definition from `library_templates`.
        """
        templates = self.cfg.get("library_templates") or {}
        env = Environment(undefined=StrictUndefined, autoescape=False)
        def to_dict(val):
            if isinstance(val, dict): return val
            if isinstance(val, list):
                acc = {}
                for frag in val:
                    if isinstance(frag, dict): acc = deep_merge(acc, frag)
                return acc
            return {}

        out = {}
        raw_libs = self.cfg.get("libraries") or {}
        iterable_libs = raw_libs.items() if isinstance(raw_libs, dict) else enumerate(raw_libs)

        for key, lib in iterable_libs:
            if not isinstance(lib, dict):
                out[key] = lib
                continue
            if isinstance(raw_libs, dict): lib["name"] = lib.get("name") or posixpath.basename(str(key))

            tmpl_name = lib.get("template")
            if not tmpl_name:
                out[key] = lib
                continue

            tmpl_data = {}
            # Resolving template source: remote, local dict, or named reference
            if isinstance(tmpl_name, dict):
                tmpl_data = tmpl_name
            elif str(tmpl_name).startswith(("http://", "https://")):
                from ..templating.source import _fetch_remote_yaml
                tmpl_data = _fetch_remote_yaml(str(tmpl_name))
            elif tmpl_name in templates:
                tmpl_data = to_dict(templates[tmpl_name])
            else:
                out[key] = lib
                continue

            # Merge the template defaults with the specific library overrides
            merged = deep_merge(tmpl_data, lib)
            nm = str(merged.get("name") or posixpath.basename(str(key))).strip()
            derived = {
                "slug": slug(nm), "snake": snake(nm), "camel": camel(nm),
                "project_snake": snake(nm) or nm, "project_slug": slug(nm), "project_camel": camel(nm),
            }
            if "project_name" not in merged: derived["project_name"] = derived["project_snake"]

            ctx = {**self.cfg, **derived, **merged}
            # Recursively render templated variables within the library config
            def render_any(obj):
                if isinstance(obj, str):
                    try: return env.from_string(obj).render(**ctx)
                    except Exception: return obj
                if isinstance(obj, list): return [render_any(x) for x in obj]
                if isinstance(obj, dict): return {k: render_any(v) for k, v in obj.items()}
                return obj

            out[key] = render_any(merged)
        self.cfg["libraries"] = list(out.values()) if isinstance(raw_libs, list) else out

    def _build_library_index(self, cfg: dict) -> dict[str, dict]:
        """
        Creates a dependency map containing metadata (pkg-configs, cmake links, find packages)
        for all defined libraries. Dynamically infers Git dependencies and fetches their
        internal configs from the workspace folder if available.
        """
        raw_libs = cfg.get("libraries") or {}
        idx: dict[str, dict] = {}
        iterable = raw_libs.items() if isinstance(raw_libs, dict) else enumerate(raw_libs)

        # 1. Register explicitly configured libraries into the index
        for key, item in iterable:
            if not isinstance(item, dict): continue

            nm = item.get("name") or (posixpath.basename(str(key)) if isinstance(key, str) else str(key))
            track_slug = slug(str(key)) if isinstance(key, str) else slug(str(nm))
            target_snake = snake(str(nm))

            fp_raw = item.get("find_package") if "find_package" in item else None
            finds = [f"{target_snake} CONFIG REQUIRED"] if "find_package" not in item else ([] if fp_raw is None else [str(x) for x in coerce_list(fp_raw) if str(x).strip()])
            pkg_configs = [str(x) for x in coerce_list(item.get("pkg_config")) if str(x).strip()]
            lk_raw = item.get("link")
            links = [f"{target_snake}::{target_snake}"] if lk_raw is None else [str(x) for x in coerce_list(lk_raw) if str(x).strip()]

            idx[track_slug] = {
                "item": item, "name": nm, "slug": track_slug, "snake": target_snake,
                "raw_key": str(key),
                "finds": finds, "pkg_configs": pkg_configs, "links": links,
                "depends_raw": list(coerce_list(item.get("depends_on"))),
                "depends": []
            }
            item.setdefault("build_steps", [])
            item.setdefault("kind", "local")

        by_snake = {v["snake"]: k for k, v in idx.items()}

        ws_str = self.cfg.get("workspace_dir", "../repos")
        workspace_dir = Path(ws_str).expanduser()
        if not workspace_dir.is_absolute():
            workspace_dir = (self.repo / workspace_dir).resolve()

        # 2. Gather all top-level explicit dependencies listed in libraries, apps, and tests
        all_explicit_deps = []
        for v in idx.values(): all_explicit_deps.extend(v["depends_raw"])

        tests_cfg = self.cfg.get("tests", {})
        if isinstance(tests_cfg, dict):
            all_explicit_deps.extend(coerce_list(tests_cfg.get("depends_on", [])))
            for t in (tests_cfg.get("targets") or []):
                if isinstance(t, dict):
                    all_explicit_deps.extend(coerce_list(t.get("depends_on", [])))

        apps_cfg = self.cfg.get("apps", {})
        if isinstance(apps_cfg, dict):
            app_ctx = apps_cfg.get("context", {})
            if isinstance(app_ctx, dict):
                all_explicit_deps.extend(coerce_list(app_ctx.get("depends_on", [])))

            for app_name, app_cfg in apps_cfg.items():
                if isinstance(app_cfg, dict):
                    all_explicit_deps.extend(coerce_list(app_cfg.get("depends_on", [])))
                    for b in (app_cfg.get("binaries") or []):
                        if isinstance(b, dict):
                            all_explicit_deps.extend(coerce_list(b.get("depends_on", [])))

        # 3. Helper to synthesize implicit/external dependencies into the index
        def _synthesize(d_raw):
            dep_name = _extract_dep_name(d_raw)
            if not dep_name: return None
            s = slug(dep_name)

            if s not in idx and snake(dep_name) not in by_snake:
                target_snake = snake(dep_name)
                url_str = d_raw.get("url") if isinstance(d_raw, dict) else (d_raw if isinstance(d_raw, str) else "")
                clean_url = url_str.split("+", 1)[-1] if "+" in str(url_str) else str(url_str)
                clean_url = clean_url.split("@", 1)[0]

                # Create build steps for git dependencies
                build_steps = []
                if clean_url.startswith(("http", "https://", "git@")):
                    build_steps = [
                        f"git clone --depth 1 \"{clean_url}\" \"{dep_name}\"",
                        f"cd {dep_name}",
                        f"./build.sh clean",
                        f"./build.sh install",
                        f"cd ..",
                        f"rm -rf {dep_name}"
                    ]

                idx[s] = {
                    "item": {"name": dep_name, "build_steps": build_steps, "kind": "git", "pkg": None},
                    "name": dep_name, "slug": s, "snake": target_snake,
                    "raw_key": dep_name, "finds": [f"{target_snake} CONFIG REQUIRED"],
                    "pkg_configs": [], "links": [f"{target_snake}::{target_snake}"],
                    "depends": [], "depends_raw": []
                }
                by_snake[target_snake] = s

                # Try to peek into the dependency's own scaffold.yaml to find nested dependencies
                dep_manifest = workspace_dir / dep_name / "scaffold.yaml"
                if dep_manifest.exists():
                    try:
                        dep_data = yaml.safe_load(dep_manifest.read_text(encoding="utf-8")) or {}
                        idx[s]["depends_raw"] = list(coerce_list(dep_data.get("depends_on", [])))
                    except Exception: pass
            return s if s in idx else by_snake.get(snake(dep_name))

        # 4. Resolve the explicit deps against the index
        for d in dedupe(all_explicit_deps): _synthesize(d)

        # 5. Iteratively process and link nested dependency arrays (breadth-first traversal)
        to_process = list(idx.keys())
        processed = set()
        while to_process:
            current_slug = to_process.pop(0)
            if current_slug in processed: continue
            processed.add(current_slug)

            v = idx[current_slug]
            deps = []
            for d_raw in v.get("depends_raw", []):
                r = _synthesize(d_raw)
                if r:
                    deps.append(r)
                    to_process.append(r)

            if not v.get("depends"):
                v["depends"] = dedupe(deps)

        return idx

    def _augment_with_libraries_tests_apps(self) -> None:
        """
        Uses the resolved dependency graph index to populate `deps`, `tests`, and `apps`
        with the full chain of required links, packages, and apt-dependencies.
        """
        idx = self._build_library_index(self.cfg)
        if not idx: return

        # Identify the root project within the index
        proj_slug = slug(self.cfg.get("project_name", "") or self.cfg.get("project_slug", ""))
        if proj_slug not in idx:
            ps = snake(self.cfg.get("project_name", ""))
            proj_slug = next((s for s, v in idx.items() if v["snake"] == ps), proj_slug)

        # 1. Augment root 'deps' block with immediate project dependencies
        if proj_slug in idx:
            direct = idx[proj_slug]["depends"]
            dp = dict(self.cfg.get("deps") or {})

            if direct:
                dp.setdefault("find_packages", dedupe([fp for d in direct for fp in (idx[d].get("finds") or []) if fp]))
                if not dp.get("pkg_config_deps"):
                    dp["pkg_config_deps"] = [{"module": m, "target": t} for m, t in {mod: idx[d]["snake"] for d in direct for mod in (idx[d].get("pkg_configs") or [])}.items()]
                dp.setdefault("link_libraries", dedupe([lk for d in direct for lk in idx[d]["links"]]))
                dp.setdefault("deps_for_config", dedupe([fp.strip().split()[0] for d in direct for fp in (idx[d].get("finds") or []) if fp.strip()]))

            # 2. Gather roots for transitive dependency evaluation
            all_roots = [proj_slug]

            # Add test targets as dependency roots
            tests_cfg = self.cfg.get("tests") or {}
            test_deps = []
            if isinstance(tests_cfg, dict):
                test_deps.extend(coerce_list(tests_cfg.get("depends_on", [])))
                for t in (tests_cfg.get("targets") or []):
                    if isinstance(t, dict):
                        test_deps.extend(coerce_list(t.get("depends_on", [])))
            all_roots.extend(self._resolve_dep_names_to_lib_slugs(test_deps, idx))

            # Add apps as dependency roots
            apps_cfg = self.cfg.get("apps") or {}
            if isinstance(apps_cfg, dict):
                app_ctx = apps_cfg.get("context", {})
                if isinstance(app_ctx, dict):
                    all_roots.extend(self._resolve_dep_names_to_lib_slugs(coerce_list(app_ctx.get("depends_on", [])), idx))

                for app_name, app_cfg in apps_cfg.items():
                    if app_name == "context":
                        continue
                    if isinstance(app_cfg, dict):
                        app_deps = coerce_list(app_cfg.get("depends_on", []))
                        for b in (app_cfg.get("binaries") or []):
                            if isinstance(b, dict):
                                app_deps.extend(coerce_list(b.get("depends_on", [])))
                        all_roots.extend(self._resolve_dep_names_to_lib_slugs(app_deps, idx))

            all_roots = dedupe(all_roots)

            # 3. Collect ALL transitive dependencies for topologically sorting libraries
            all_transitive = self._collect_transitive(idx, all_roots, exclude_roots=False)
            if proj_slug in all_transitive:
                all_transitive.remove(proj_slug)

            dp["libraries"] = [idx[s]["item"] for s in self._toposort_subset(idx, set(all_transitive))]

            # Extract any APT packages required by system-level dependencies
            apt_pkgs = []
            for s in set(all_transitive):
                item = idx[s]["item"]
                if str(item.get("kind")) == "system" and item.get("pkg"):
                    pkg = item["pkg"]
                    apt_pkgs.extend([str(x) for x in pkg if str(x).strip()] if isinstance(pkg, (list, tuple)) else [str(pkg)])
            dp["apt_packages"] = dedupe([p for p in apt_pkgs if p and str(p).lower() not in ("none", "null")])

            self.cfg["deps"] = dp

        # Cascade resolution to tests and apps
        self.cfg = self._augment_tests_cfg(self.cfg, idx, proj_slug)
        self.cfg = self._normalize_apps_cfg(self.cfg, idx, proj_slug)

        # 4. Resolve explicit developer/apt packages attached to the project
        dev_pkgs = []
        for pkg, constraint in (self.cfg.get("dev_packages") or {}).items() if isinstance(self.cfg.get("dev_packages"), dict) else {p: True for p in coerce_list(self.cfg.get("dev_packages"))}.items():
            if constraint is False or constraint is None: continue
            elif constraint is True: dev_pkgs.append(str(pkg))
            else: dev_pkgs.append(f"{pkg}{str(constraint).strip()}" if str(constraint).strip() and str(constraint).strip()[0] in "=<>~" else f"{pkg}={str(constraint).strip()}")
        if dev_pkgs:
            dp = dict(self.cfg.get("deps") or {})
            dp["apt_dev_packages"] = dedupe([p for p in dev_pkgs if p.strip()])
            self.cfg["deps"] = dp

        # Ensure all core 'deps' lists are initialized
        dp = dict(self.cfg.get("deps") or {})
        for k in ["sources", "libraries", "deps_for_config", "apt_packages", "apt_dev_packages", "find_packages", "pkg_config_deps", "link_libraries", "depends_on"]: dp.setdefault(k, [])
        self.cfg["deps"] = dp

    def _collect_transitive(self, idx: dict[str, dict], roots: Iterable[str], *, exclude_roots=False) -> list[str]:
        """
        Traverses the dependency tree index starting from the `roots` to find all nested dependencies.
        Returns a flat list of dependency slugs.
        """
        roots = [r for r in roots if r in idx]
        seen, stack = set(), list(roots)
        while stack:
            s = stack.pop()
            if s in seen: continue
            seen.add(s)
            stack.extend(d for d in idx[s]["depends"] if d in idx)
        if exclude_roots: seen.difference_update(roots)
        return list(seen)

    def _toposort_subset(self, idx: dict[str, dict], subset: Iterable[str]) -> list[str]:
        """
        Performs a topological sort on a subset of the dependency graph using Kahn's algorithm.
        Ensures that dependencies are listed in the correct linking order (dependents before dependencies).
        """
        S = set(subset)
        adj, indeg = {s: [] for s in S}, {s: 0 for s in S}
        for s in S:
            for d in idx[s]["depends"]:
                if d in S:
                    indeg[s] += 1
                    adj[d].append(s)
        queue = [s for s in S if indeg[s] == 0]
        ordered = []
        while queue:
            queue.sort(key=lambda x: idx[x]["name"].lower())
            n = queue.pop(0)
            ordered.append(n)
            for m in adj[n]:
                indeg[m] -= 1
                if indeg[m] == 0: queue.append(m)
        if len(ordered) != len(S): ordered.extend(sorted([s for s in S if s not in ordered], key=lambda x: idx[x]["name"].lower()))
        return ordered

    def _gather_apt_packages(self, cfg: dict, idx: dict[str, dict], proj_slug: str) -> list[str]:
        """
        Collects all transitive 'system' dependencies configured via APT for a specific project slug.
        """
        if proj_slug not in idx: return []
        pkgs = []
        for s in set(self._collect_transitive(idx, [proj_slug], exclude_roots=True)):
            item = idx[s]["item"]
            if str(item.get("kind")) != "system" or not item.get("pkg"): continue
            pkg = item["pkg"]
            pkgs.extend([str(x) for x in pkg if str(x).strip()] if isinstance(pkg, (list, tuple)) else [str(pkg)])
        return dedupe([p for p in pkgs if p and str(p).lower() not in ("none", "null")])

    def _normalize_build_targets(self, raw_items: Any, abs_dest_dir: Path, default_src_dir: str = "") -> list[dict]:
        """
        Converts messy user inputs for build targets (which could be strings, lists of sources,
        or dictionaries) into a standardized list of dictionaries with normalized source paths
        and expanded glob patterns.
        """
        if not raw_items: return []
        b_dict = {}
        # Parse inputs into a standard dictionary
        if isinstance(raw_items, list):
            for item in raw_items:
                if isinstance(item, str) and item.strip(): b_dict[item.strip()] = {"sources": [f"{default_src_dir}/{item.strip()}.c" if default_src_dir else f"{item.strip()}.c"]}
                elif isinstance(item, dict) and item:
                    if "name" in item: b_dict[str(item.pop("name")).strip()] = item
                    else:
                        k = next(iter(item.keys()))
                        v = item[k]
                        if isinstance(v, list): b_dict[str(k).strip()] = {"sources": v}
                        elif isinstance(v, str): b_dict[str(k).strip()] = {"sources": [v]}
                        elif isinstance(v, dict): b_dict[str(k).strip()] = v
        elif isinstance(raw_items, dict):
            for k, v in raw_items.items():
                if isinstance(v, list): b_dict[str(k).strip()] = {"sources": v}
                elif isinstance(v, str): b_dict[str(k).strip()] = {"sources": [v]}
                elif isinstance(v, dict): b_dict[str(k).strip()] = v
                elif v is None: b_dict[str(k).strip()] = {"sources": [f"{default_src_dir}/{k}.c" if default_src_dir else f"{k}.c"]}
        else: b_dict[str(raw_items).strip()] = {"sources": [f"{default_src_dir}/{str(raw_items).strip()}.c" if default_src_dir else f"{str(raw_items).strip()}.c"]}

        norm = []
        # Expand wildcard patterns (* or ?) into absolute paths within the dest directory
        for name, conf in b_dict.items():
            if not name: continue
            raw_sources = coerce_list(conf.get("sources") or [f"{default_src_dir}/{name}.c" if default_src_dir else f"{name}.c"])
            expanded_sources, include_dirs = [], set()
            for src in raw_sources:
                src_str = str(src).strip()
                if "*" in src_str or "?" in src_str:
                    if abs_dest_dir.exists():
                        search_path = abs_dest_dir / src_str
                        matches = glob.glob(str(search_path), recursive=True)
                        if matches:
                            for m_str in matches:
                                if os.path.isfile(m_str):
                                    rel_p = os.path.relpath(m_str, str(abs_dest_dir)).replace("\\", "/")
                                    expanded_sources.append(rel_p)
                                    include_dirs.add(posixpath.dirname(rel_p) or ".")
                        else:
                            expanded_sources.append(src_str)
                            include_dirs.add(posixpath.dirname(src_str) or ".")
                    else:
                        expanded_sources.append(src_str)
                        include_dirs.add(posixpath.dirname(src_str) or ".")
                else:
                    expanded_sources.append(src_str)
                    include_dirs.add(posixpath.dirname(src_str) or ".")

            ent = {"name": name, "sources": dedupe(expanded_sources), "include_dirs": sorted(list(include_dirs))}
            # Persist other linker/compiler flags
            for f in ("link_libraries", "depends_on", "find_packages"):
                if conf.get(f) is not None: ent[f] = [str(x) for x in coerce_list(conf[f])]
            for f in ("c_standard", "cxx_standard"):
                if f in conf: ent[f] = conf[f]
            norm.append(ent)
        return sorted(norm, key=lambda x: x["name"])

    def _resolve_dep_names_to_lib_slugs(self, dep_names: list[str], idx: dict) -> list[str]:
        """
        Translates a raw list of dependency strings into their respective library index slugs.
        """
        by_snake = {v["snake"]: k for k, v in idx.items()}
        out = []
        for nm in dep_names or []:
            if not nm: continue
            clean_nm = _extract_dep_name(nm)
            s = slug(clean_nm)
            if s in idx: out.append(s)
            elif snake(clean_nm) in by_snake: out.append(by_snake[snake(clean_nm)])
        return dedupe(out)

    def _derive_suite_deps_from_libs(self, lib_slugs: list[str], idx: dict) -> tuple[list[str], list[str]]:
        """
        Helper that extracts find_packages and link_libraries for a given list of library slugs.
        """
        return dedupe([fp for s in lib_slugs for fp in idx[s]["finds"]]), dedupe([lk for s in lib_slugs for lk in idx[s]["links"]])

    def _augment_tests_cfg(self, cfg: dict, idx: dict, proj_slug: str) -> dict:
        """
        Normalizes test target definitions and injects all missing find/link libraries required
        by the dependency graph into the test suite configuration.
        """
        ts = dict(cfg.get("tests") or {})
        norm_targets = self._normalize_build_targets(ts.get("targets") or ts.get("test_targets"), self.repo / "tests", "src")
        if norm_targets: ts["targets"] = ts["test_targets"] = norm_targets
        union_deps = [str(x) for x in coerce_list(ts.get("depends_on"))]
        for t in (norm_targets or []): union_deps.extend(str(nm) for nm in t.get("depends_on") or [])
        lib_slugs = self._resolve_dep_names_to_lib_slugs(union_deps, idx)

        in_links, ext_finds, ext_links = [], [], []
        for s in lib_slugs:
            if s not in idx: continue
            if s == proj_slug: in_links.extend(idx[s].get("links", []))
            else:
                ext_finds.extend(idx[s].get("finds", []))
                ext_links.extend(idx[s].get("links", []))
        ts.setdefault("find_packages", dedupe(ext_finds))
        ts.setdefault("link_libraries", dedupe(in_links + ext_links))
        cfg["tests"] = ts
        return cfg

    def _compute_app_dest_dir(self, base_dest: str, ctx_dest: str | None, ctx_name: str) -> str:
        """
        Calculates the relative directory structure where an app should be written based
        on base overrides and local names.
        """
        base = (base_dest or "apps").strip("/")
        return str(ctx_dest).strip().strip("/") if ctx_dest and str(ctx_dest).strip().startswith("/") else f"{base}/{(str(ctx_dest).strip('/') if ctx_dest else ctx_name)}"

    def _normalize_apps_cfg(self, cfg: dict, idx: dict, proj_slug: str) -> dict:
        """
        Applies a shared 'context' to individual apps, normalizes their binary build targets,
        resolves dependencies locally per app, and sets up CMake linkers/find packages.
        """
        apps = dict(cfg.get("apps") or {})
        if not apps: return cfg
        base_ctx = dict(apps.get("context") or {})
        base_dest = str(base_ctx.get("dest") or "apps")
        base_ctx_wo_dest = {k: v for k, v in base_ctx.items() if k != "dest"}

        out = {}
        for name, v in apps.items():
            if name == "context": continue
            # Merge base app context with specific app definition
            ctx = deep_merge(base_ctx_wo_dest, dict(v or {}))
            ctx["_apps_dest_dir"] = self._compute_app_dest_dir(base_dest, ctx.get("dest"), str(name))
            ctx["binaries"] = self._normalize_build_targets(ctx.get("binaries"), self.repo / ctx["_apps_dest_dir"], "src")

            # Combine the app's base dependencies with each specific binary's dependencies
            union_deps = [str(x) for x in coerce_list(base_ctx_wo_dest.get("depends_on")) + coerce_list(ctx.get("depends_on"))]
            for b in ctx["binaries"]: union_deps.extend(str(nm) for nm in b.get("depends_on") or [])
            if not union_deps and proj_slug: union_deps.append(proj_slug)

            lib_slugs = self._resolve_dep_names_to_lib_slugs(dedupe(union_deps), idx)
            if lib_slugs:
                finds, links = self._derive_suite_deps_from_libs(lib_slugs, idx)
                ctx.setdefault("find_packages", finds)
                ctx.setdefault("link_libraries", links)
            out[str(name)] = ctx

        apps.update(out)
        cfg["apps"] = apps
        return cfg

    def _compute_package_switches(self) -> None:
        """
        Extracts toggles and patterns for 'packages' (which are groups of templates or components).
        Resolves conditional rendering for package filters.
        """
        env = Environment(undefined=StrictUndefined)
        def render_pat(p):
            if "{{" in p:
                try: return env.from_string(p).render(**self.cfg)
                except Exception: return p
            return p

        self.package_patterns = {name: [render_pat(str(x)) for x in coerce_list(pats)] for name, pats in (self.cfg.get("template_packages") or {}).items()}
        raw_pkgs = self.cfg.get("packages") or {}
        enabled, flavors = set(), {}

        # Determine which packages are switched on, and if they have a specific 'flavor'
        if isinstance(raw_pkgs, dict):
            for pkg, val in raw_pkgs.items():
                if val is False or val is None: continue
                enabled.add(pkg)
                if isinstance(val, str) and val.lower() != "true": flavors[pkg] = val
        else: enabled = set(str(x) for x in coerce_list(raw_pkgs))

        self.enabled_packages = enabled
        self.cfg["package_flavors"] = flavors