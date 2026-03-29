# src/scaffold_repo/core/config.py
from __future__ import annotations

import posixpath
import sys
import os
import glob
import datetime
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
    if isinstance(raw_dep, dict):
        if len(raw_dep) == 1:
            k = next(iter(raw_dep))
            if k.startswith(("http://", "https://", "git@")): raw_dep = k
        if isinstance(raw_dep, dict):
            raw_dep = raw_dep.get("url") or raw_dep.get("source") or ""

    s = str(raw_dep).strip()

    if "+" in s and "://" not in s.split("+", 1)[0]: s = s.split("+", 1)[1]
    if "@" in s: s = s.rsplit("@", 1)[0]

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
        return self.cfg

    def get_planner(self) -> TemplatePlanner:
        self.cfg["package_patterns"] = self.package_patterns
        self.cfg["enabled_packages"] = self.enabled_packages
        return TemplatePlanner(self.repo, self.tmpl_src, self.cfg, self.is_init)

    def load(self) -> None:
        local_manifest = self.repo / "scaffold.yaml"
        local_data = {}

        rc_cfg = find_scaffoldrc(self.repo)
        ws_dir = Path(rc_cfg.get("workspace_dir") or self.repo.parent).resolve()

        url = None
        ref = "main"
        custom_path = None

        if local_manifest.exists():
            try:
                local_data = yaml.safe_load(local_manifest.read_text(encoding="utf-8")) or {}
            except Exception as e:
                print(f"Warning: Failed to parse local {local_manifest}:\n{e}", file=sys.stderr)

            base_tmpl = local_data.get("base_templates")
            if isinstance(base_tmpl, dict) and "repo" in base_tmpl:
                url = base_tmpl["repo"]
                ref = base_tmpl.get("ref", "main")
                custom_path = base_tmpl.get("path")

        if not url and rc_cfg.get("template_registry_url"):
            url = rc_cfg.get("template_registry_url")
            ref = rc_cfg.get("template_registry_ref", "main")

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

        self.cfg = self.tmpl_src.load_defaults_yaml() or {}

        if rc_cfg.get("workspace_dir"):
            self.cfg["workspace_dir"] = rc_cfg["workspace_dir"]

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

        self._select_project()

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
                if source.startswith(("http://", "https://")):
                    from ..templating.source import _fetch_remote_yaml
                    inc_data = _fetch_remote_yaml(source, ref=ref, file_path=file_path)
                else:
                    inc_str = str(source)
                    if not inc_str.endswith((".yaml", ".yml")):
                        inc_str += ".yaml"
                    inc_data = self.tmpl_src._load_logical_path(inc_str)

                if isinstance(inc_data, dict):
                    if include_keys:
                        inc_data = {k: v for k, v in inc_data.items() if k in include_keys}
                    if exclude_keys:
                        inc_data = {k: v for k, v in inc_data.items() if k not in exclude_keys}

                base = deep_merge(base, inc_data)

            self.cfg = deep_merge(self.cfg, base)
            self.cfg = deep_merge(self.cfg, local_data)

            raw_stack = str(self.cfg.get("stack") or "").strip()
            if raw_stack:
                st = raw_stack.split("/")[0].lower()
                st_type = raw_stack.split("/")[1].lower() if "/" in raw_stack else "base"
                stack_defaults = self.tmpl_src.get_stacked_defaults(f"stacks/{st}/{st_type}/_")
                self.cfg = deep_merge(stack_defaults, self.cfg)

            prof = self.cfg.get("profile")
            if prof:
                prof_file = f"{prof}.yaml" if not str(prof).endswith((".yaml", ".yml")) else str(prof)
                prof_data = self.tmpl_src._load_logical_path(f"profiles/{prof_file}")
                if prof_data:
                    self.cfg = deep_merge(prof_data, self.cfg)

            nm = self.project_name or local_data.get("project_name") or local_data.get("project_title") or self.repo.name
            self.cfg["project_name"] = nm
            self.cfg["project_slug"] = slug(nm)
            self.cfg["project_snake"] = local_data.get("project_snake") or snake(nm) or nm
            self.cfg["project_camel"] = local_data.get("project_camel") or camel(nm)

            self.cfg.setdefault("libraries", {})
            lib_entry = dict(local_data)
            lib_entry["name"] = nm
            self.cfg["libraries"][self.cfg["project_slug"]] = lib_entry

        self._render_contributors()
        self._normalize_keys_autofill()
        self._expand_library_templates()
        self._augment_with_libraries_tests_apps()
        self._compute_package_switches()

    def _render_contributors(self) -> None:
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
        proj = self.project_name or self.cfg.get("project_name")
        if not proj: return
        proj_str = str(proj).strip()
        proj_base = posixpath.basename(proj_str)

        if "/" in proj_str:
            for pth in [f"libraries/{proj_str}.yaml", f"apps/{proj_str}.yaml"]:
                extra_data = self.tmpl_src._load_logical_path(pth)
                if extra_data:
                    self.cfg = deep_merge(self.cfg, extra_data)
                    break

        want_slug, want_snake = slug(proj_base), snake(proj_base)
        picked = None

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

    def _predict_template_files(self, resource_dir: str | None = None) -> list[str]:
        import re
        from jinja2 import Environment

        found = []
        st, st_type = self.cfg.get("stack"), self.cfg.get("stack_type")
        valid_prefixes = [f"stacks/{st}/{st_type}/", f"stacks/{st}/base/"] if st else []

        if resource_dir:
            valid_prefixes = [f"{p}{resource_dir}/" for p in valid_prefixes]

        env = Environment()
        for rel, data, is_j2, _ in self.tmpl_src.iter_files():
            if not is_j2 or not valid_prefixes or not any(rel.startswith(pfx) for pfx in valid_prefixes):
                continue

            text = data.decode("utf-8", errors="ignore")
            m = re.match(r"^\s*\{#-?\s*(.*?)\s*-?#\}\s*", text, re.S)
            dest = ""

            if m:
                try:
                    meta = yaml.safe_load(m.group(1))
                    if isinstance(meta, dict):
                        meta = meta.get("scaffold-repo", meta.get("scaffold_repo", meta))
                        if isinstance(meta, dict):
                            dest = meta.get("dest", "")
                except Exception: pass

            if not dest:
                for pfx in valid_prefixes:
                    if rel.startswith(pfx):
                        dest = rel[len(pfx):-3]
                        break

            if dest:
                try:
                    rendered = env.from_string(dest).render(**self.cfg)
                    found.append(rendered)
                except Exception: pass
        return found

    def _auto_discover_targets(self, rule: dict, dest_dir: Path) -> dict:
        import fnmatch

        source_globs = coerce_list(rule.get("source_globs") or ["*"])
        strategy = rule.get("discovery_strategy", "aggregate")
        targets_dir = rule.get("targets_dir", "")

        discovered_files = []
        search_base = self.repo / dest_dir / targets_dir if targets_dir else self.repo / dest_dir

        if search_base.exists():
            for pattern in source_globs:
                for p in search_base.rglob(pattern.replace("**/", "")) if "**" in pattern else search_base.glob(pattern):
                    if p.is_file():
                        discovered_files.append(p.relative_to(self.repo / dest_dir).as_posix())
        else:
            predicted_files = self._predict_template_files(rule.get("resource"))
            dest_prefix = dest_dir.as_posix()
            for predicted_dest in predicted_files:
                if dest_prefix == "." or predicted_dest.startswith(dest_prefix + "/"):
                    for pattern in source_globs:
                        if fnmatch.fnmatch(Path(predicted_dest).name, pattern):
                            rel_p = predicted_dest if dest_prefix == "." else predicted_dest[len(dest_prefix)+1:]
                            discovered_files.append(rel_p)
                            break

        discovered_files = sorted(list(set(discovered_files)))
        if not discovered_files:
            return {}

        if strategy == "1-to-1":
            return {Path(f).stem: {"sources": [f]} for f in discovered_files}
        else:
            proj_snake = self.cfg.get("project_snake", "project")
            return {proj_snake: {"sources": discovered_files}}

    def _normalize_subprojects(self, idx: dict, proj_slug: str) -> None:
        rules = self.cfg.get("subproject_rules", {})
        reserved = {"depends_on", "context"}

        # ── ZERO-CONFIG MAGIC ──
        for implicit_key in ["main", "tests"]:
            if implicit_key in rules and implicit_key not in self.cfg:
                self.cfg[implicit_key] = {}

        for block_name, rule in rules.items():
            block_data = self.cfg.get(block_name)
            if not isinstance(block_data, dict): continue

            suite_ctx = dict(block_data.get("context") or {})
            suite_ctx["depends_on"] = coerce_list(block_data.get("depends_on", []))

            out = {}

            # ── THE FIX: Flat vs Grouped Detection ──
            # If the block has standard target keys at the root, it's flat!
            flat_keys = {"targets", "find_packages", "link_libraries", "include_dirs", "sources"}
            is_flat = any(k in block_data for k in flat_keys) or not any(k for k in block_data.keys() if k not in reserved)

            items_to_process = {"": block_data} if is_flat else block_data

            for item_name, item_cfg in items_to_process.items():
                if item_name in reserved: continue

                # Safe fallback if they used shorthand at the group level too
                if not isinstance(item_cfg, dict):
                    item_cfg = {"targets": item_cfg}

                ctx = deep_merge(suite_ctx, dict(item_cfg or {}))

                if block_name == "main":
                    ctx["_dest_dir"] = "."
                else:
                    ctx["_dest_dir"] = f"{block_name}/{item_name}" if item_name else f"{block_name}"

                is_auto_discovered = "targets" not in ctx
                discovered = {}

                # ── SHORTHAND ROUTING VARIABLES ──
                targets_dir = rule.get("targets_dir", "")
                default_ext = rule.get("default_ext", "c")

                if is_auto_discovered:
                    discovered = self._auto_discover_targets(rule, self.repo / ctx["_dest_dir"])
                    ctx["targets"] = self._normalize_build_targets(discovered, self.repo / ctx["_dest_dir"], targets_dir, default_ext)
                else:
                    ctx["targets"] = self._normalize_build_targets(ctx["targets"], self.repo / ctx["_dest_dir"], targets_dir, default_ext)

                # Ensure discovered targets don't overwrite the cleaner include_dirs from _auto_discover_targets
                if is_auto_discovered and discovered:
                    for t in ctx["targets"]:
                        if t["name"] in discovered:
                            t["include_dirs"] = discovered[t["name"]].get("include_dirs", [])

                # FIX 1: Grab global dependencies from the root of scaffold.yaml
                global_deps = coerce_list(self.cfg.get("depends_on", []))

                # Combine global + suite + specific item dependencies
                union_deps = [str(x) for x in global_deps + coerce_list(suite_ctx.get("depends_on")) + coerce_list(ctx.get("depends_on"))]
                for tgt in ctx["targets"]:
                    union_deps.extend(str(nm) for nm in tgt.get("depends_on", []))

                # FIX 2: Ensure subprojects (tests, apps, examples) ALWAYS link to the main library!
                if proj_slug and block_name != "main" and proj_slug not in union_deps:
                    union_deps.append(proj_slug)

                lib_slugs = self._resolve_dep_names_to_lib_slugs(dedupe(union_deps), idx)
                finds, links = [], []

                for s in lib_slugs:
                    if s not in idx: continue
                    # Do not find_package ourselves if we are linking in-tree!
                    if s == proj_slug:
                        links.extend(idx[s].get("links", []))
                    else:
                        finds.extend(idx[s].get("finds", []))
                        links.extend(idx[s].get("links", []))

                ctx.setdefault("find_packages", dedupe(finds))
                ctx.setdefault("link_libraries", dedupe(links))

                ctx["targets_dir"] = targets_dir

                name_key = item_name if item_name else "default"
                ctx["name"] = name_key

                if ctx["targets"]:
                    out[name_key] = ctx

            self.cfg[block_name] = out

            if block_name != "main" and out:
                for ctx in out.values():
                    self.cfg.setdefault("active_subprojects", []).append(ctx["_dest_dir"])

    def _normalize_keys_autofill(self) -> None:
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

        current_year = str(self.cfg.get("date") or datetime.date.today().year)[:4]
        self.cfg["year"] = current_year
        raw_created = str(self.cfg.get("date_created") or "")
        default_start_year = raw_created[:4] if len(raw_created) >= 4 else current_year

        env = Environment(undefined=StrictUndefined, autoescape=False)
        def _render_val(val):
            if isinstance(val, str) and "{{" in val:
                try: return env.from_string(val).render(**self.cfg)
                except Exception: return val
            return val

        def _resolve_entity_ref(val):
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

        raw_copyrights = self.cfg.get("copyrights", [])
        norm_copyrights = []
        for raw_cp in (raw_copyrights if isinstance(raw_copyrights, list) else [raw_copyrights]):
            norm_cp = _resolve_entity_ref(raw_cp)
            if not norm_cp: continue
            if "entity" not in norm_cp and "contact" in norm_cp: norm_cp["entity"] = norm_cp["contact"]
            norm_cp["entity"] = _render_val(norm_cp.get("entity", ""))
            norm_cp["full_entity"] = _render_val(norm_cp.get("full_entity", norm_cp.get("entity", "")))
            cp_start = str(norm_cp.get("start_year") or default_start_year)
            if cp_start < default_start_year: cp_start = default_start_year
            end_year = str(norm_cp.get("end_year") or current_year)
            norm_cp["year_span"] = f"{cp_start}–{end_year}" if cp_start and cp_start != end_year else end_year
            norm_copyrights.append(norm_cp)
        self.cfg["copyrights"] = norm_copyrights

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
            if "entity" not in norm_c and "contact" in norm_c: norm_c["entity"] = norm_c["contact"]
            norm_c["role"] = _render_val(norm_c.get("role", "Maintainer"))
            norm_c["entity"] = _render_val(norm_c.get("entity", ""))
            norm_contacts.append(norm_c)
        self.cfg["contacts"] = norm_contacts

    def _augment_with_libraries_tests_apps(self) -> None:
        idx = self._build_library_index(self.cfg)
        if not idx: return

        proj_slug = slug(self.cfg.get("project_name", "") or self.cfg.get("project_slug", ""))
        if proj_slug not in idx:
            ps = snake(self.cfg.get("project_name", ""))
            proj_slug = next((s for s, v in idx.items() if v["snake"] == ps), proj_slug)

        dp = dict(self.cfg.get("deps") or {})
        if proj_slug in idx:
            direct = idx[proj_slug]["depends"]
            if direct:
                dp.setdefault("find_packages", dedupe([fp for d in direct for fp in (idx[d].get("finds") or []) if fp]))
                if not dp.get("pkg_config_deps"):
                    dp["pkg_config_deps"] = [{"module": m, "target": t} for m, t in {mod: idx[d]["snake"] for d in direct for mod in (idx[d].get("pkg_configs") or [])}.items()]
                dp.setdefault("link_libraries", dedupe([lk for d in direct for lk in idx[d]["links"]]))
                dp.setdefault("deps_for_config", dedupe([fp.strip().split()[0] for d in direct for fp in (idx[d].get("finds") or []) if fp.strip()]))

        all_roots = [proj_slug]

        rules = self.cfg.get("subproject_rules", {})
        for block_name in rules.keys():
            block_data = self.cfg.get(block_name)
            if not isinstance(block_data, dict): continue

            suite_deps = coerce_list(block_data.get("depends_on", []))
            all_roots.extend(self._resolve_dep_names_to_lib_slugs(suite_deps, idx))

            for item_name, item_cfg in block_data.items():
                if item_name in ("context", "depends_on"): continue
                if isinstance(item_cfg, dict):
                    item_deps = coerce_list(item_cfg.get("depends_on", []))
                    all_roots.extend(self._resolve_dep_names_to_lib_slugs(item_deps, idx))
                    for tgt in coerce_list(item_cfg.get("targets", [])):
                        if isinstance(tgt, dict):
                            all_roots.extend(self._resolve_dep_names_to_lib_slugs(coerce_list(tgt.get("depends_on", [])), idx))

        all_roots = dedupe(all_roots)
        all_transitive = self._collect_transitive(idx, all_roots, exclude_roots=False)
        if proj_slug in all_transitive: all_transitive.remove(proj_slug)

        dp["libraries"] = [idx[s]["item"] for s in self._toposort_subset(idx, set(all_transitive))]

        apt_pkgs = []
        for s in set(all_transitive):
            item = idx[s]["item"]
            if str(item.get("kind")) == "system" and item.get("pkg"):
                pkg = item["pkg"]
                apt_pkgs.extend([str(x) for x in pkg if str(x).strip()] if isinstance(pkg, (list, tuple)) else [str(pkg)])
        dp["apt_packages"] = dedupe([p for p in apt_pkgs if p and str(p).lower() not in ("none", "null")])

        self.cfg["deps"] = dp

        self._normalize_subprojects(idx, proj_slug)

        dev_pkgs = []
        for pkg, constraint in (self.cfg.get("dev_packages") or {}).items() if isinstance(self.cfg.get("dev_packages"), dict) else {p: True for p in coerce_list(self.cfg.get("dev_packages"))}.items():
            if constraint is False or constraint is None: continue
            elif constraint is True: dev_pkgs.append(str(pkg))
            else: dev_pkgs.append(f"{pkg}{str(constraint).strip()}" if str(constraint).strip() and str(constraint).strip()[0] in "=<>~" else f"{pkg}={str(constraint).strip()}")
        if dev_pkgs:
            dp = dict(self.cfg.get("deps") or {})
            dp["apt_dev_packages"] = dedupe([p for p in dev_pkgs if p.strip()])
            self.cfg["deps"] = dp

        dp = dict(self.cfg.get("deps") or {})
        for k in ["sources", "libraries", "deps_for_config", "apt_packages", "apt_dev_packages", "find_packages", "pkg_config_deps", "link_libraries", "depends_on"]: dp.setdefault(k, [])
        self.cfg["deps"] = dp

    def _expand_library_templates(self) -> None:
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

            merged = deep_merge(tmpl_data, lib)
            nm = str(merged.get("name") or posixpath.basename(str(key))).strip()
            derived = {
                "slug": slug(nm), "snake": snake(nm), "camel": camel(nm),
                "project_snake": snake(nm) or nm, "project_slug": slug(nm), "project_camel": camel(nm),
            }
            if "project_name" not in merged: derived["project_name"] = derived["project_snake"]

            ctx = {**self.cfg, **derived, **merged}
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
        raw_libs = cfg.get("libraries") or {}
        idx: dict[str, dict] = {}
        iterable = raw_libs.items() if isinstance(raw_libs, dict) else enumerate(raw_libs)

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

        all_explicit_deps = []
        for v in idx.values(): all_explicit_deps.extend(v["depends_raw"])

        rules = self.cfg.get("subproject_rules", {})
        for block_name in rules.keys():
            block_data = self.cfg.get(block_name)
            if not isinstance(block_data, dict): continue
            all_explicit_deps.extend(coerce_list(block_data.get("depends_on", [])))
            for item_name, item_cfg in block_data.items():
                if item_name in ("context", "depends_on"): continue
                if isinstance(item_cfg, dict):
                    all_explicit_deps.extend(coerce_list(item_cfg.get("depends_on", [])))
                    for tgt in coerce_list(item_cfg.get("targets", [])):
                        if isinstance(tgt, dict):
                            all_explicit_deps.extend(coerce_list(tgt.get("depends_on", [])))

        def _synthesize(d_raw):
            dep_name = _extract_dep_name(d_raw)
            if not dep_name: return None
            s = slug(dep_name)

            if s not in idx and snake(dep_name) not in by_snake:
                target_snake = snake(dep_name)
                url_str = d_raw.get("url") if isinstance(d_raw, dict) else (d_raw if isinstance(d_raw, str) else "")
                clean_url = url_str.split("+", 1)[-1] if "+" in str(url_str) else str(url_str)
                clean_url = clean_url.split("@", 1)[0]

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

                dep_manifest = workspace_dir / dep_name / "scaffold.yaml"
                if dep_manifest.exists():
                    try:
                        dep_data = yaml.safe_load(dep_manifest.read_text(encoding="utf-8")) or {}
                        idx[s]["depends_raw"] = list(coerce_list(dep_data.get("depends_on", [])))
                    except Exception: pass
            return s if s in idx else by_snake.get(snake(dep_name))

        for d in dedupe(all_explicit_deps): _synthesize(d)

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

    def _collect_transitive(self, idx: dict[str, dict], roots: Iterable[str], *, exclude_roots=False) -> list[str]:
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
        if proj_slug not in idx: return []
        pkgs = []
        for s in set(self._collect_transitive(idx, [proj_slug], exclude_roots=True)):
            item = idx[s]["item"]
            if str(item.get("kind")) != "system" or not item.get("pkg"): continue
            pkg = item["pkg"]
            pkgs.extend([str(x) for x in pkg if str(x).strip()] if isinstance(pkg, (list, tuple)) else [str(pkg)])
        return dedupe([p for p in pkgs if p and str(p).lower() not in ("none", "null")])

    def _normalize_build_targets(self, raw_items: Any, abs_dest_dir: Path, targets_dir: str = "", default_ext: str = "c") -> list[dict]:
        if not raw_items: return []
        b_dict = {}

        # The Shorthand Magic Helper
        def _make_default_src(name: str) -> str:
            return f"{targets_dir}/{name}.{default_ext}" if targets_dir else f"{name}.{default_ext}"

        if isinstance(raw_items, list):
            for item in raw_items:
                if isinstance(item, str) and item.strip():
                    name = item.strip()
                    b_dict[name] = {"sources": [_make_default_src(name)]}
                elif isinstance(item, dict) and item:
                    if "name" in item:
                        b_dict[str(item.pop("name")).strip()] = item
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
                elif v is None: b_dict[str(k).strip()] = {"sources": [_make_default_src(str(k).strip())]}
        else:
            name = str(raw_items).strip()
            b_dict[name] = {"sources": [_make_default_src(name)]}

        norm = []
        for name, conf in b_dict.items():
            if not name: continue
            raw_sources = coerce_list(conf.get("sources") or [_make_default_src(name)])
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
            for f in ("link_libraries", "depends_on", "find_packages"):
                if conf.get(f) is not None: ent[f] = [str(x) for x in coerce_list(conf[f])]
            for f in ("c_standard", "cxx_standard"):
                if f in conf: ent[f] = conf[f]
            norm.append(ent)
        return sorted(norm, key=lambda x: x["name"])
    def _resolve_dep_names_to_lib_slugs(self, dep_names: list[str], idx: dict) -> list[str]:
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
        return dedupe([fp for s in lib_slugs for fp in idx[s]["finds"]]), dedupe([lk for s in lib_slugs for lk in idx[s]["links"]])

    def _compute_package_switches(self) -> None:
        env = Environment(undefined=StrictUndefined)
        def render_pat(p):
            if "{{" in p:
                try: return env.from_string(p).render(**self.cfg)
                except Exception: return p
            return p

        self.package_patterns = {name: [render_pat(str(x)) for x in coerce_list(pats)] for name, pats in (self.cfg.get("template_packages") or {}).items()}
        raw_pkgs = self.cfg.get("packages") or {}
        enabled, flavors = set(), {}

        if isinstance(raw_pkgs, dict):
            for pkg, val in raw_pkgs.items():
                if val is False or val is None: continue
                enabled.add(pkg)
                if isinstance(val, str) and val.lower() != "true": flavors[pkg] = val
        else: enabled = set(str(x) for x in coerce_list(raw_pkgs))

        self.enabled_packages = enabled
        self.cfg["package_flavors"] = flavors
