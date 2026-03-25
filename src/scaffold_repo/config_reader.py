# src/scaffold_repo/config_reader.py
from __future__ import annotations

import fnmatch
import glob
import hashlib
import os
import posixpath
import re
import sys
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
import subprocess

import yaml
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined
from .scaffoldrc import find_scaffoldrc

from .utils.text import _slug, _snake, _camel, _sha256
from .utils.collections import _dedupe, _coerce_list, _deep_merge
from .utils.git import sync_git_template_repo

# ──────────────────────────────────────────────────────────────────────────────
# Small, self‑contained utilities (pure, no I/O side effects)
# ──────────────────────────────────────────────────────────────────────────────
def _ensure_trailing_newline(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    if not s.endswith("\n"): return s + "\n"
    return s

def _diff(old: bytes, new: bytes, path: str) -> str:
    import difflib
    old_lines = old.decode("utf-8", errors="replace").splitlines(keepends=True)
    new_lines = new.decode("utf-8", errors="replace").splitlines(keepends=True)
    return "".join(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}"))

def _fetch_remote_yaml(url: str, ref: str | None = None, file_path: str | None = None) -> dict:
    if not url.startswith(("http://", "https://")):
        return {}

    ref = ref or "main"
    target_file = file_path or "scaffold.yaml"

    if url.endswith(".git") and "github.com" in url:
        parts = url.replace(".git", "").split("github.com/")[-1].split("/")
        if len(parts) >= 2:
            url = f"https://raw.githubusercontent.com/{parts[0]}/{parts[1]}/{ref}/{target_file}"

    cache_key = hashlib.md5(f"{url}@{ref}/{target_file}".encode('utf-8')).hexdigest()
    cache_file = Path.home() / ".cache" / "scaffold-repo" / "urls" / f"{cache_key}.yaml"

    if cache_file.exists():
        try:
            return yaml.safe_load(cache_file.read_text(encoding="utf-8")) or {}
        except Exception:
            pass

    print(f"  [Network] Fetching remote config: {url}")
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as response:
            raw_text = response.read().decode('utf-8')

        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(raw_text, encoding="utf-8")
        return yaml.safe_load(raw_text) or {}
    except Exception as e:
        print(f"Warning: Failed to fetch remote config {url}: {e}", file=sys.stderr)
        return {}

_OSS_HEADER_EXTS = {
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".java", ".js", ".ts",
    ".tsx", ".mjs", ".cjs", ".go", ".rs", ".swift", ".kt", ".cs",
    ".cmake", ".mk", ".make",
}

def _header_managed_default(dest: str) -> bool:
    p = Path(dest)
    return p.name == "CMakeLists.txt" or (p.suffix.lower() in _OSS_HEADER_EXTS)

_LINE = "line"
_BLOCK = "block"
def _comment_style_for(path: Path) -> dict[str, str]:
    name = path.name.lower()
    ext = path.suffix.lower()
    if name == "cmakelists.txt" or ext == ".cmake" or name.startswith("makefile") or ext in {".mk", ".make"}:
        return {"mode": _LINE, "prefix": "#"}
    if ext in {".py", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"}:
        return {"mode": _LINE, "prefix": "#"}
    if ext in {".sql"}:
        return {"mode": _LINE, "prefix": "--"}
    if ext in _OSS_HEADER_EXTS and ext not in {".cmake", ".mk", ".make"}:
        return {"mode": _LINE, "prefix": "//"}
    if ext in {".html", ".xml", ".xsd", ".svg"}:
        return {"mode": _BLOCK, "open": ""}
    if ext in {".css", ".scss"}:
        return {"mode": _BLOCK, "open": "/*", "close": "*/"}
    return {"mode": _LINE, "prefix": "#"}

def _strip_spdx_for_compare(path: Path, text: str) -> str:
    style = _comment_style_for(path)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    i = 1 if (lines and lines[0].startswith("#!")) else 0
    n = len(lines)

    def _is_blank(s: str) -> bool: return not s.strip()

    if style["mode"] == _LINE:
        pref = style["prefix"]
        start = i
        j = start
        while j < n and (_is_blank(lines[j]) or lines[j].lstrip().startswith(pref)): j += 1
        top = lines[start:j]
        if not top: return text
        spdx_idxs = [k for k, ln in enumerate(top) if "SPDX-" in ln]
        if not spdx_idxs: return text
        last = spdx_idxs[-1]
        k = last + 1
        while k < len(top) and (not _is_blank(top[k])) and top[k].lstrip().startswith(pref): k += 1
        if k < len(top) and _is_blank(top[k]): k += 1
        end = start + k
        return "\n".join(lines[:start] + lines[end:])
    else:
        open_, close_ = style["open"], style["close"]
        start = i
        if start < n and lines[start].strip().startswith(open_):
            j = start + 1
            while j < n and not lines[j].strip().endswith(close_): j += 1
            j = min(j + 1, n)
            block = lines[start:j]
            if any("SPDX-" in ln for ln in block):
                end = j
                if end < n and not lines[end].strip(): end += 1
                return "\n".join(lines[:start] + lines[end:])
    return text

def _normalize_for_cmp(text: str, path: Path, header_managed: bool) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if header_managed:
        text = _strip_spdx_for_compare(path, text)
    return re.sub(r"\n+\Z", "\n", (text.rstrip("\n") + "\n"))

# ──────────────────────────────────────────────────────────────────────────────
# Template Source & YAML Includes Loader
# ──────────────────────────────────────────────────────────────────────────────

class TemplateSource:
    def __init__(self, base_dir: Path | None = None, pkg_rel: str | None = "templates"):
        self._pkg_root = base_dir if (base_dir and base_dir.is_dir()) else None

        if self._pkg_root is None and pkg_rel:
            import importlib.resources as resources
            try:
                self._pkg_root = resources.files("scaffold_repo").joinpath(pkg_rel)
                _ = list(self._pkg_root.iterdir())
            except Exception:
                self._pkg_root = None

    def find_registry_yamls(self, prefix: str) -> list[str]:
        out = []
        clean_prefix = prefix.strip("/")

        def scan_node(node, current_prefix):
            try:
                for child in node.iterdir():
                    if child.is_file() and child.name.endswith((".yaml", ".yml")):
                        out.append(f"{current_prefix}/{child.name}")
                    elif child.is_dir() and not child.name.startswith("."):
                        scan_node(child, f"{current_prefix}/{child.name}")
            except Exception: pass

        if self._pkg_root:
            tgt = self._pkg_root
            for part in clean_prefix.split("/"):
                if part: tgt = tgt.joinpath(part)
            if tgt.is_dir(): scan_node(tgt, clean_prefix)

        return _dedupe(out)

    def get_stacked_defaults(self, rel_path: str) -> dict:
        """Cascades .scaffold-defaults.yaml from the root down to the target directory."""
        if not hasattr(self, "_defaults_cache"):
            self._defaults_cache = {}

        dir_path = posixpath.dirname(rel_path)
        if dir_path in self._defaults_cache:
            return dict(self._defaults_cache[dir_path])

        parts = dir_path.split("/") if dir_path else []
        stacked = {}
        current = ""

        paths_to_check = [".scaffold-defaults.yaml"]
        for p in parts:
            if not p or p == ".": continue
            current = f"{current}/{p}" if current else p
            paths_to_check.append(f"{current}/.scaffold-defaults.yaml")

        for pth in paths_to_check:
            text = self.read_resource_text(pth)
            if text:
                try:
                    data = yaml.safe_load(text) or {}
                    if isinstance(data, dict):
                        stacked = _deep_merge(stacked, data)
                except Exception:
                    pass

        self._defaults_cache[dir_path] = stacked
        return dict(stacked)

    def _load_logical_path(self, rel_path: str, seen: set[str] | None = None) -> dict:
        if seen is None: seen = set()
        rel_path = posixpath.normpath(rel_path)
        if rel_path in seen: return {}

        seen_next = seen.copy()
        seen_next.add(rel_path)

        def parse_and_resolve(file_obj) -> dict:
            try:
                data = yaml.safe_load(file_obj.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:
                print(f"Warning: YAML parsing failed in {rel_path}\n{e}", file=sys.stderr)
                return {}
            except Exception:
                return {}

            root_data = data.pop("root", {}) if isinstance(data, dict) else {}
            if root_data: data = _deep_merge(root_data, data)

            raw_includes = data.pop("includes", [])
            if not isinstance(raw_includes, list): raw_includes = [raw_includes]

            base = self.get_stacked_defaults(rel_path)
            for inc in raw_includes:
                if isinstance(inc, str):
                    inc = {"source": inc}

                source = inc.get("source") or inc.get("repo")
                if not source: continue

                ref = inc.get("ref") or inc.get("branch") or inc.get("tag")
                file_path = inc.get("file")
                include_keys = _coerce_list(inc.get("include", []))
                exclude_keys = _coerce_list(inc.get("exclude", []))

                inc_data = {}
                if source.startswith(("http://", "https://")):
                    inc_data = _fetch_remote_yaml(source, ref=ref, file_path=file_path)
                else:
                    inc_str = str(source)
                    if not inc_str.endswith((".yaml", ".yml")):
                        inc_str += ".yaml"
                    inc_data = self._load_logical_path(inc_str, seen_next)

                if isinstance(inc_data, dict):
                    if include_keys:
                        inc_data = {k: v for k, v in inc_data.items() if k in include_keys}
                    if exclude_keys:
                        inc_data = {k: v for k, v in inc_data.items() if k not in exclude_keys}

                base = _deep_merge(base, inc_data)

            rel_no_ext = posixpath.splitext(rel_path)[0]
            parts = rel_no_ext.split("/", 1)
            if len(parts) == 2 and parts[0] in ("libraries", "apps", "licenses", "library-templates", "app-templates"):
                folder = parts[0].replace("-", "_")
                key = parts[1]
                if folder not in data: data = {folder: {key: data}}

            for folder_key in ["libraries", "apps"]:
                raw_dict = data.get(folder_key)
                if isinstance(raw_dict, dict):
                    new_dict = {}
                    for k, v in raw_dict.items():
                        if isinstance(v, dict) and "name" not in v:
                            v["name"] = posixpath.basename(str(k))
                        new_dict[str(k)] = v
                    data[folder_key] = new_dict

            return _deep_merge(base, data)

        pkg_data = {}
        if self._pkg_root:
            try:
                cand = self._pkg_root
                for part in rel_path.split("/"):
                    if part and part != ".": cand = cand.joinpath(part)
                if cand.is_file(): pkg_data = parse_and_resolve(cand)
            except Exception: pass

        if not pkg_data:
            actual_basename = posixpath.basename(rel_path)
            if actual_basename != ".scaffold-defaults.yaml" and rel_path != ".scaffold-defaults.yaml":
                print(f"Warning: Included file '{rel_path}' not found in templates.", file=sys.stderr)

        return pkg_data

    def iter_files(self):
        SKIP_DIRS = {"libraries", "apps", "profiles", "licenses", "library-templates", "app-templates"}
        files_map = {}

        if self._pkg_root:
            def walk(node, prefix=""):
                for child in node.iterdir():
                    if child.is_dir() and not prefix and child.name in SKIP_DIRS: continue
                    name = child.name
                    rel = f"{prefix}{name}" if prefix else name
                    if child.is_file(): files_map[rel] = (child.read_bytes(), rel.endswith(".j2"), "pkg", child)
                    elif child.is_dir(): walk(child, rel + "/")
            walk(self._pkg_root)

        for rel, (data, is_j2, origin, _path) in files_map.items():
            yield rel, data, is_j2, origin

    def load_defaults_yaml(self) -> dict:
        return self._load_logical_path(".scaffold-defaults.yaml")

    def read_resource_text(self, rel_path: str) -> str | None:
        for rel, data, _is_j2, _origin in self.iter_files():
            if rel == rel_path: return data.decode("utf-8", errors="replace")
        return None

_FM_RE = re.compile(r"^\s*\{#-?\s*(.*?)\s*-?#\}\s*", re.S)

def _extract_annotation(text: str):
    m = _FM_RE.match(text)
    if not m: return None, text
    try: data = yaml.safe_load(m.group(1))
    except yaml.YAMLError: return None, text

    meta = {}
    if isinstance(data, dict):
        val = data.get("scaffold-repo", data.get("scaffold_repo", data))
        if isinstance(val, str): meta = {"context": val}
        elif isinstance(val, dict): meta = dict(val)
    elif isinstance(data, str):
        meta = {"context": data}
    return (meta or None), text[m.end():]

@dataclass
class PlanItem:
    kind: str
    path: str
    status: str
    updatable: bool
    diff: str
    new_bytes: bytes
    template_sha256: str
    context_key: str | None = None
    header_managed: bool = True
    executable: bool = False

def _extract_dep_name(raw_dep: Any) -> str:
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
                self.cfg["libraries"] = _deep_merge(self.cfg["libraries"], data["libraries"])

        for f in self.tmpl_src.find_registry_yamls("apps"):
            data = self.tmpl_src._load_logical_path(f)
            if data and "apps" in data:
                self.cfg.setdefault("apps", {})
                self.cfg["apps"] = _deep_merge(self.cfg["apps"], data["apps"])

        for f in self.tmpl_src.find_registry_yamls("licenses"):
            data = self.tmpl_src._load_logical_path(f)
            if data and "licenses" in data:
                self.cfg.setdefault("licenses", {})
                self.cfg["licenses"] = _deep_merge(self.cfg["licenses"], data["licenses"])

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
                include_keys = _coerce_list(inc.get("include", []))
                exclude_keys = _coerce_list(inc.get("exclude", []))

                inc_data = {}
                if source.startswith(("http://", "https://")):
                    inc_data = _fetch_remote_yaml(source, ref=ref, file_path=file_path)
                else:
                    inc_str = str(source)
                    if not inc_str.endswith((".yaml", ".yml")):
                        inc_str += ".yaml"
                    inc_data = self.tmpl_src._load_logical_path(inc_str)  # <-- TYPO FIXED HERE

                if isinstance(inc_data, dict):
                    if include_keys:
                        inc_data = {k: v for k, v in inc_data.items() if k in include_keys}
                    if exclude_keys:
                        inc_data = {k: v for k, v in inc_data.items() if k not in exclude_keys}

                base = _deep_merge(base, inc_data)

            self.cfg = _deep_merge(self.cfg, base)
            self.cfg = _deep_merge(self.cfg, local_data)

            raw_stack = str(self.cfg.get("stack") or "").strip()
            if raw_stack:
                st = raw_stack.split("/")[0].lower()
                st_type = raw_stack.split("/")[1].lower() if "/" in raw_stack else "base"
                stack_defaults = self.tmpl_src.get_stacked_defaults(f"stacks/{st}/{st_type}/_")
                self.cfg = _deep_merge(stack_defaults, self.cfg)  # Local Config wins over defaults

            nm = self.project_name or local_data.get("project_name") or local_data.get("project_title") or self.repo.name
            self.cfg["project_name"] = nm
            self.cfg["project_slug"] = _slug(nm)
            self.cfg["project_snake"] = local_data.get("project_snake") or _snake(nm) or nm
            self.cfg["project_camel"] = local_data.get("project_camel") or _camel(nm)

            self.cfg.setdefault("libraries", {})
            lib_entry = dict(local_data)
            lib_entry["name"] = nm
            self.cfg["libraries"][self.cfg["project_slug"]] = lib_entry

        self._render_contributors()
        self._normalize_keys_autofill()
        self._expand_library_templates()
        self._augment_with_libraries_tests_apps()
        self._compute_package_switches()

    @property
    def effective_config(self) -> dict: return self.cfg

    def read_resource_text(self, rel_path: str) -> str | None:
        return self.tmpl_src.read_resource_text(rel_path) if self.tmpl_src else None

    def _strip_package_prefix(self, rel: str) -> str:
        for pkg_name, pats in self.package_patterns.items():
            if pkg_name == "resources": continue
            for pat in pats:
                if pat.endswith("/**"):
                    prefix = pat[:-2]
                    if rel.startswith(prefix): return rel[len(prefix):]
        return rel

    def plan_jinja(self, *, show_diffs: bool = False) -> list[PlanItem]:
        env = self._jinja_env_for_inline()
        items = self._discover_jinja_items()
        plan: list[PlanItem] = []
        for it in items:
            ctx = self._build_ctx_inherited(it["context"])
            new_text = self._render_with_help(env, it, ctx)

            try:
                rendered_dest = env.from_string(it["dest"]).render(**ctx)
            except Exception:
                rendered_dest = it["dest"]

            target = self.repo / rendered_dest
            old_text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
            hm_meta = it.get("header_managed")
            header_managed = _header_managed_default(rendered_dest) if hm_meta is None else bool(hm_meta)
            cmp_new = _normalize_for_cmp(new_text, target, header_managed)
            cmp_old = _normalize_for_cmp(old_text, target, header_managed)
            status = "create" if not target.exists() else ("update" if cmp_old != cmp_new else "unchanged")
            diff_text = _diff(old_text.encode("utf-8"), new_text.encode("utf-8"), rendered_dest) if show_diffs and status in ("create", "update") else ""
            is_exec = it.get("executable", False)
            plan.append(PlanItem("jinja", rendered_dest, status, it.get("updatable", True), diff_text, new_text.encode("utf-8"), _sha256(it["inline_template"].encode("utf-8")), it["context"], header_managed, is_exec))

        plan.extend(self._plan_apps_resources(show_diffs=show_diffs))
        return plan

    def plan_copy(self, *, show_diffs: bool = False) -> list[PlanItem]:
        plan: list[PlanItem] = []
        for it in self._discover_copy_items():
            new_norm = _ensure_trailing_newline(it["bytes"].decode("utf-8", errors="replace"))
            new_bytes = new_norm.encode("utf-8")
            target = self.repo / it["dest"]
            old_norm = _ensure_trailing_newline(target.read_text(encoding="utf-8", errors="replace") if target.exists() else "")
            status = "create" if not target.exists() else ("update" if old_norm != new_norm else "unchanged")
            diff_text = _diff(old_norm.encode("utf-8"), new_norm.encode("utf-8"), it["dest"]) if show_diffs and status in ("create", "update") else ""
            plan.append(PlanItem("copy", it["dest"], status, True, diff_text, new_bytes, _sha256(new_bytes), None, False, it.get("executable", False)))
        return plan

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
                    self.cfg = _deep_merge(self.cfg, extra_data)
                    break

        want_slug, want_snake = _slug(proj_base), _snake(proj_base)
        picked = None

        for category in ["libraries", "apps"]:
            items = self.cfg.get(category) or {}
            iterable_items = items.items() if isinstance(items, dict) else enumerate(items)
            for key, item in iterable_items:
                if isinstance(item, dict):
                    nm = str(item.get("name") or key).strip()
                    if nm and (nm == proj_base or _slug(nm) == want_slug or _snake(nm) == want_snake):
                        picked = item
                        break
            if picked: break

        if not picked: return
        self.cfg = _deep_merge(self.cfg, picked)
        if not self.cfg.get("project_name"): self.cfg["project_name"] = picked.get("name") or proj_base

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

        dp = dict(self.cfg.get("deps") or {})
        ts = dict(self.cfg.get("tests") or {})

        ps = self.cfg.get("project_snake") or "project"

        lib_srcs = self.cfg.get("library_sources") or dp.get("sources")
        if not lib_srcs:
            exts = {".c", ".cc", ".cpp", ".cxx"}
            src_root = self.repo / "src"
            if self.is_init and not src_root.exists():
                auto = [f"src/{ps}.c"]
            else:
                auto = [p.relative_to(self.repo).as_posix() for p in src_root.rglob("*") if p.is_file() and p.suffix.lower() in exts] if src_root.is_dir() else []
            if auto: lib_srcs = sorted(auto)

        norm_srcs = [str(s) for s in (lib_srcs if isinstance(lib_srcs, (list, tuple)) else [lib_srcs])] if lib_srcs else []
        self.cfg["library_sources"] = dp["sources"] = norm_srcs

        test_tgts = self.cfg.get("test_targets") or ts.get("test_targets") or ts.get("targets")
        if not test_tgts:
            tests_src = self.repo / "tests" / "src"
            exts = {".c", ".cc", ".cpp", ".cxx"}
            if self.is_init and not tests_src.exists():
                auto = [{"name": f"test_{ps}", "sources": [f"src/test_{ps}.c"]}]
            else:
                auto = [{"name": p.stem, "sources": [f"src/{p.name}"]} for p in tests_src.glob("test_*") if p.is_file() and p.suffix.lower() in exts] if tests_src.is_dir() else []
            test_tgts = sorted(auto, key=lambda t: t["name"]) if auto else []

        ts["targets"] = test_tgts or []
        self.cfg["deps"], self.cfg["tests"] = dp, ts

        import datetime
        current_year = str(self.cfg.get("date") or "")[:4]
        if not current_year or len(current_year) < 4:
            current_year = str(datetime.datetime.now().year)

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
            if isinstance(val, dict):
                return dict(val)
            if isinstance(val, str):
                if not " " in val and "." in val and not "{{" in val:
                    parts = val.split(".")
                    curr = self.cfg
                    for p in parts:
                        if isinstance(curr, dict) and p in curr:
                            curr = curr[p]
                        else:
                            return {"entity": val}
                    if isinstance(curr, dict):
                        return dict(curr)
                return {"entity": val}
            return {}

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

        raw_contacts = _coerce_list(self.cfg.get("contacts", []))
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

    def _build_ctx_inherited(self, key: str | None) -> dict:
        ctx = _deep_merge(self._base_from_cfg(self.cfg), {} if not key or key == "." else (self.cfg.get(key) or {}))
        ctx.setdefault("project_name", self.cfg.get("project_name") or "project")
        ctx.setdefault("project_slug", _slug(ctx.get("project_name", "project")))
        ctx.setdefault("project_snake", _snake(ctx["project_slug"]))
        ctx.setdefault("project_camel", _camel(ctx.get("project_name", "project")))
        ctx.setdefault("project_title", ctx.get("project_title", ctx.get("project_name")))
        ctx.setdefault("version", str(self.cfg.get("version") or "0.1.0"))

        stack = self.cfg.get("stack", "generic")
        stack_type = self.cfg.get("stack_type", "")
        ctx.setdefault("stack", stack)
        ctx.setdefault("stack_type", stack_type)

        scoped_key = f"{stack}_{stack_type}".strip("_").lower()
        if scoped_key in self.cfg:
            ctx = _deep_merge(ctx, self.cfg[scoped_key])

        ctx.setdefault("deps", self.cfg.get("deps") or {})
        ctx.setdefault("tests", self.cfg.get("tests") or {})
        ctx.setdefault("test_targets", (self.cfg.get("tests") or {}).get("test_targets") or (self.cfg.get("tests") or {}).get("targets") or [])

        ctx.setdefault("kind", "compiled")
        ctx.setdefault("is_cli_app", "Library")
        return ctx

    def _plan_apps_resources(self, *, show_diffs: bool) -> list[PlanItem]:
        apps = self.cfg.get("apps") or {}
        contexts = [k for k in apps.keys() if k != "context"]
        if not contexts: return []
        all_app_resources = [(rel, data, is_j2, origin) for rel, data, is_j2, origin in self.tmpl_src.iter_files() if rel.startswith("app-resources/") and not rel.endswith("/")]
        if not all_app_resources: return []

        env, plan, base = self._jinja_env_for_inline(), [], self._build_ctx_inherited("deps")
        for ctx_name in contexts:
            ctx = dict(apps.get(ctx_name) or {})
            dest_dir = ctx.get("_apps_dest_dir") or self._compute_app_dest_dir(str((apps.get("context") or {}).get("dest") or "apps"), ctx.get("dest"), ctx_name)
            rctx = _deep_merge(base, ctx)

            raw_stack = str(ctx.get("stack") or self.cfg.get("stack", "")).strip()
            raw_type = str(ctx.get("stack_type") or self.cfg.get("stack_type", "")).strip()

            if "/" in raw_stack:
                app_stack, derived_type = raw_stack.split("/", 1)
                app_stack = app_stack.lower()
                app_stack_type = raw_type.lower() or derived_type.lower()
            else:
                app_stack = raw_stack.lower()
                app_stack_type = raw_type.lower()

            if not app_stack_type:
                app_stack_type = raw_type.lower() or "base"

            app_defaults = self.tmpl_src.get_stacked_defaults(f"stacks/{app_stack}/{app_stack_type}/_")
            rctx = _deep_merge(app_defaults, rctx)

            active_prefix = f"app-resources/{app_stack}/{app_stack_type}/" if app_stack and app_stack_type else None
            global_prefix = "app-resources/global/"

            rctx.setdefault("project_name", self.cfg.get("project_name") or "project")
            rctx.setdefault("project_slug", _slug(rctx["project_name"]))
            rctx.setdefault("project_snake", _snake(rctx["project_slug"]))

            app_scoped_key = f"{app_stack}_{app_stack_type}".strip("_").lower()
            if app_scoped_key in self.cfg:
                rctx = _deep_merge(rctx, self.cfg[app_scoped_key])

            rctx.setdefault("app_project_name", f"{base.get('project_snake','project')}_{ctx_name}")
            rctx.setdefault("app_stack", app_stack)
            rctx.setdefault("app_stack_type", app_stack_type)

            for rel, data, is_j2, origin in all_app_resources:
                root_prefix = active_prefix if active_prefix and rel.startswith(active_prefix) else (global_prefix if rel.startswith(global_prefix) else None)
                if not root_prefix: continue
                sub_rel = rel[len(root_prefix):]
                dest_rel = f"{dest_dir}/{sub_rel[:-3] if (is_j2 and sub_rel.endswith('.j2')) else sub_rel}"
                target = self.repo / dest_rel

                if is_j2:
                    raw_tpl = data.decode("utf-8", errors="replace")
                    try: new_bytes = env.from_string(raw_tpl).render(**rctx).encode("utf-8")
                    except Exception as e: raise RuntimeError(f"Jinja render error in apps resource '{rel}' → '{dest_rel}': {e}") from e
                    tmpl_sha = _sha256(raw_tpl.encode("utf-8"))
                else: new_bytes, tmpl_sha = data, _sha256(data)

                old_text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
                hm = _header_managed_default(dest_rel)
                cmp_new, cmp_old = _normalize_for_cmp(new_bytes.decode("utf-8", errors="replace"), target, hm), _normalize_for_cmp(old_text, target, hm)
                status = "create" if not target.exists() else ("update" if cmp_old != cmp_new else "unchanged")
                diff_text = _diff(old_text.encode("utf-8"), cmp_new.encode("utf-8"), dest_rel) if show_diffs and status in ("create", "update") else ""

                is_exec = False
                if hasattr(origin, "exists") and origin.exists():
                    import os
                    is_exec = os.access(origin, os.X_OK)

                plan.append(PlanItem("jinja" if is_j2 else "copy", dest_rel, status, True, diff_text, new_bytes, tmpl_sha, f"apps.{ctx_name}", hm, is_exec))
        return plan

    def _expand_library_templates(self) -> None:
        templates = self.cfg.get("library_templates") or {}
        env = Environment(undefined=StrictUndefined, autoescape=False)
        def to_dict(val):
            if isinstance(val, dict): return val
            if isinstance(val, list):
                acc = {}
                for frag in val:
                    if isinstance(frag, dict): acc = _deep_merge(acc, frag)
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
                tmpl_data = _fetch_remote_yaml(str(tmpl_name))
            elif tmpl_name in templates:
                tmpl_data = to_dict(templates[tmpl_name])
            else:
                out[key] = lib
                continue

            merged = _deep_merge(tmpl_data, lib)
            nm = str(merged.get("name") or posixpath.basename(str(key))).strip()
            derived = {
                "slug": _slug(nm), "snake": _snake(nm), "camel": _camel(nm),
                "project_snake": _snake(nm) or nm, "project_slug": _slug(nm), "project_camel": _camel(nm),
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
            track_slug = _slug(str(key)) if isinstance(key, str) else _slug(str(nm))
            target_snake = _snake(str(nm))

            fp_raw = item.get("find_package") if "find_package" in item else None
            finds = [f"{target_snake} CONFIG REQUIRED"] if "find_package" not in item else ([] if fp_raw is None else [str(x) for x in _coerce_list(fp_raw) if str(x).strip()])
            pkg_configs = [str(x) for x in _coerce_list(item.get("pkg_config")) if str(x).strip()]
            lk_raw = item.get("link")
            links = [f"{target_snake}::{target_snake}"] if lk_raw is None else [str(x) for x in _coerce_list(lk_raw) if str(x).strip()]

            idx[track_slug] = {
                "item": item, "name": nm, "slug": track_slug, "snake": target_snake,
                "raw_key": str(key),
                "finds": finds, "pkg_configs": pkg_configs, "links": links,
                "depends_raw": list(_coerce_list(item.get("depends_on"))),
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

        tests_cfg = self.cfg.get("tests", {})
        if isinstance(tests_cfg, dict):
            all_explicit_deps.extend(_coerce_list(tests_cfg.get("depends_on", [])))
            for t in (tests_cfg.get("targets") or []):
                if isinstance(t, dict):
                    all_explicit_deps.extend(_coerce_list(t.get("depends_on", [])))

        apps_cfg = self.cfg.get("apps", {})
        if isinstance(apps_cfg, dict):
            app_ctx = apps_cfg.get("context", {})
            if isinstance(app_ctx, dict):
                all_explicit_deps.extend(_coerce_list(app_ctx.get("depends_on", [])))

            for app_name, app_cfg in apps_cfg.items():
                if isinstance(app_cfg, dict):
                    all_explicit_deps.extend(_coerce_list(app_cfg.get("depends_on", [])))
                    for b in (app_cfg.get("binaries") or []):
                        if isinstance(b, dict):
                            all_explicit_deps.extend(_coerce_list(b.get("depends_on", [])))

        def _synthesize(d_raw):
            dep_name = _extract_dep_name(d_raw)
            if not dep_name: return None
            s = _slug(dep_name)

            if s not in idx and _snake(dep_name) not in by_snake:
                target_snake = _snake(dep_name)
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
                        idx[s]["depends_raw"] = list(_coerce_list(dep_data.get("depends_on", [])))
                    except Exception: pass
            return s if s in idx else by_snake.get(_snake(dep_name))

        for d in _dedupe(all_explicit_deps): _synthesize(d)

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
                v["depends"] = _dedupe(deps)

        return idx

    def _augment_with_libraries_tests_apps(self) -> None:
        idx = self._build_library_index(self.cfg)
        if not idx: return
        proj_slug = _slug(self.cfg.get("project_name", "") or self.cfg.get("project_slug", ""))
        if proj_slug not in idx:
            ps = _snake(self.cfg.get("project_name", ""))
            proj_slug = next((s for s, v in idx.items() if v["snake"] == ps), proj_slug)

        if proj_slug in idx:
            direct = idx[proj_slug]["depends"]
            dp = dict(self.cfg.get("deps") or {})

            if direct:
                dp.setdefault("find_packages", _dedupe([fp for d in direct for fp in (idx[d].get("finds") or []) if fp]))
                if not dp.get("pkg_config_deps"):
                    dp["pkg_config_deps"] = [{"module": m, "target": t} for m, t in {mod: idx[d]["snake"] for d in direct for mod in (idx[d].get("pkg_configs") or [])}.items()]
                dp.setdefault("link_libraries", _dedupe([lk for d in direct for lk in idx[d]["links"]]))
                dp.setdefault("deps_for_config", _dedupe([fp.strip().split()[0] for d in direct for fp in (idx[d].get("finds") or []) if fp.strip()]))

            all_roots = [proj_slug]

            tests_cfg = self.cfg.get("tests") or {}
            test_deps = []
            if isinstance(tests_cfg, dict):
                test_deps.extend(_coerce_list(tests_cfg.get("depends_on", [])))
                for t in (tests_cfg.get("targets") or []):
                    if isinstance(t, dict):
                        test_deps.extend(_coerce_list(t.get("depends_on", [])))
            all_roots.extend(self._resolve_dep_names_to_lib_slugs(test_deps, idx))

            apps_cfg = self.cfg.get("apps") or {}
            if isinstance(apps_cfg, dict):
                app_ctx = apps_cfg.get("context", {})
                if isinstance(app_ctx, dict):
                    all_roots.extend(self._resolve_dep_names_to_lib_slugs(_coerce_list(app_ctx.get("depends_on", [])), idx))

                for app_name, app_cfg in apps_cfg.items():
                    if app_name == "context":
                        continue
                    if isinstance(app_cfg, dict):
                        app_deps = _coerce_list(app_cfg.get("depends_on", []))
                        for b in (app_cfg.get("binaries") or []):
                            if isinstance(b, dict):
                                app_deps.extend(_coerce_list(b.get("depends_on", [])))
                        all_roots.extend(self._resolve_dep_names_to_lib_slugs(app_deps, idx))

            all_roots = _dedupe(all_roots)

            all_transitive = self._collect_transitive(idx, all_roots, exclude_roots=False)
            if proj_slug in all_transitive:
                all_transitive.remove(proj_slug)

            dp["libraries"] = [idx[s]["item"] for s in self._toposort_subset(idx, set(all_transitive))]

            apt_pkgs = []
            for s in set(all_transitive):
                item = idx[s]["item"]
                if str(item.get("kind")) == "system" and item.get("pkg"):
                    pkg = item["pkg"]
                    apt_pkgs.extend([str(x) for x in pkg if str(x).strip()] if isinstance(pkg, (list, tuple)) else [str(pkg)])
            dp["apt_packages"] = _dedupe([p for p in apt_pkgs if p and str(p).lower() not in ("none", "null")])

            self.cfg["deps"] = dp

        self.cfg = self._augment_tests_cfg(self.cfg, idx, proj_slug)
        self.cfg = self._normalize_apps_cfg(self.cfg, idx, proj_slug)

        dev_pkgs = []
        for pkg, constraint in (self.cfg.get("dev_packages") or {}).items() if isinstance(self.cfg.get("dev_packages"), dict) else {p: True for p in _coerce_list(self.cfg.get("dev_packages"))}.items():
            if constraint is False or constraint is None: continue
            elif constraint is True: dev_pkgs.append(str(pkg))
            else: dev_pkgs.append(f"{pkg}{str(constraint).strip()}" if str(constraint).strip() and str(constraint).strip()[0] in "=<>~" else f"{pkg}={str(constraint).strip()}")
        if dev_pkgs:
            dp = dict(self.cfg.get("deps") or {})
            dp["apt_dev_packages"] = _dedupe([p for p in dev_pkgs if p.strip()])
            self.cfg["deps"] = dp

        dp = dict(self.cfg.get("deps") or {})
        for k in ["sources", "libraries", "deps_for_config", "apt_packages", "apt_dev_packages", "find_packages", "pkg_config_deps", "link_libraries", "depends_on"]: dp.setdefault(k, [])
        self.cfg["deps"] = dp

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
        return _dedupe([p for p in pkgs if p and str(p).lower() not in ("none", "null")])

    def _normalize_build_targets(self, raw_items: Any, abs_dest_dir: Path, default_src_dir: str = "") -> list[dict]:
        if not raw_items: return []
        b_dict = {}
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
        for name, conf in b_dict.items():
            if not name: continue
            raw_sources = _coerce_list(conf.get("sources") or [f"{default_src_dir}/{name}.c" if default_src_dir else f"{name}.c"])
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
            ent = {"name": name, "sources": _dedupe(expanded_sources), "include_dirs": sorted(list(include_dirs))}
            for f in ("link_libraries", "depends_on", "find_packages"):
                if conf.get(f) is not None: ent[f] = [str(x) for x in _coerce_list(conf[f])]
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
            s = _slug(clean_nm)
            if s in idx: out.append(s)
            elif _snake(clean_nm) in by_snake: out.append(by_snake[_snake(clean_nm)])
        return _dedupe(out)

    def _derive_suite_deps_from_libs(self, lib_slugs: list[str], idx: dict) -> tuple[list[str], list[str]]:
        return _dedupe([fp for s in lib_slugs for fp in idx[s]["finds"]]), _dedupe([lk for s in lib_slugs for lk in idx[s]["links"]])

    def _augment_tests_cfg(self, cfg: dict, idx: dict, proj_slug: str) -> dict:
        ts = dict(cfg.get("tests") or {})
        norm_targets = self._normalize_build_targets(ts.get("targets") or ts.get("test_targets"), self.repo / "tests", "src")
        if norm_targets: ts["targets"] = ts["test_targets"] = norm_targets
        union_deps = [str(x) for x in _coerce_list(ts.get("depends_on"))]
        for t in (norm_targets or []): union_deps.extend(str(nm) for nm in t.get("depends_on") or [])
        lib_slugs = self._resolve_dep_names_to_lib_slugs(union_deps, idx)

        in_links, ext_finds, ext_links = [], [], []
        for s in lib_slugs:
            if s not in idx: continue
            if s == proj_slug: in_links.extend(idx[s].get("links", []))
            else:
                ext_finds.extend(idx[s].get("finds", []))
                ext_links.extend(idx[s].get("links", []))
        ts.setdefault("find_packages", _dedupe(ext_finds))
        ts.setdefault("link_libraries", _dedupe(in_links + ext_links))
        cfg["tests"] = ts
        return cfg

    def _compute_app_dest_dir(self, base_dest: str, ctx_dest: str | None, ctx_name: str) -> str:
        base = (base_dest or "apps").strip("/")
        return str(ctx_dest).strip().strip("/") if ctx_dest and str(ctx_dest).strip().startswith("/") else f"{base}/{(str(ctx_dest).strip('/') if ctx_dest else ctx_name)}"

    def _normalize_apps_cfg(self, cfg: dict, idx: dict, proj_slug: str) -> dict:
        apps = dict(cfg.get("apps") or {})
        if not apps: return cfg
        base_ctx = dict(apps.get("context") or {})
        base_dest = str(base_ctx.get("dest") or "apps")
        base_ctx_wo_dest = {k: v for k, v in base_ctx.items() if k != "dest"}

        out = {}
        for name, v in apps.items():
            if name == "context": continue
            ctx = _deep_merge(base_ctx_wo_dest, dict(v or {}))
            ctx["_apps_dest_dir"] = self._compute_app_dest_dir(base_dest, ctx.get("dest"), str(name))
            ctx["binaries"] = self._normalize_build_targets(ctx.get("binaries"), self.repo / ctx["_apps_dest_dir"], "src")
            union_deps = [str(x) for x in _coerce_list(base_ctx_wo_dest.get("depends_on")) + _coerce_list(ctx.get("depends_on"))]
            for b in ctx["binaries"]: union_deps.extend(str(nm) for nm in b.get("depends_on") or [])
            if not union_deps and proj_slug: union_deps.append(proj_slug)
            lib_slugs = self._resolve_dep_names_to_lib_slugs(_dedupe(union_deps), idx)
            if lib_slugs:
                finds, links = self._derive_suite_deps_from_libs(lib_slugs, idx)
                ctx.setdefault("find_packages", finds)
                ctx.setdefault("link_libraries", links)
            out[str(name)] = ctx
        apps.update(out)
        cfg["apps"] = apps
        return cfg

    def _compute_package_switches(self) -> None:
        env = Environment(undefined=StrictUndefined)
        def render_pat(p):
            if "{{" in p:
                try: return env.from_string(p).render(**self.cfg)
                except Exception: return p
            return p
        self.package_patterns = {name: [render_pat(str(x)) for x in _coerce_list(pats)] for name, pats in (self.cfg.get("template_packages") or {}).items()}
        raw_pkgs = self.cfg.get("packages") or {}
        enabled, flavors = set(), {}
        if isinstance(raw_pkgs, dict):
            for pkg, val in raw_pkgs.items():
                if val is False or val is None: continue
                enabled.add(pkg)
                if isinstance(val, str) and val.lower() != "true": flavors[pkg] = val
        else: enabled = set(str(x) for x in _coerce_list(raw_pkgs))
        self.enabled_packages = enabled
        self.cfg["package_flavors"] = flavors

    def _matches_disabled(self, rel: str) -> bool:
        matched = {pkg for pkg, pats in self.package_patterns.items() for pat in pats if fnmatch.fnmatch(rel, pat)}
        return bool(matched) and not any(pkg in self.enabled_packages for pkg in matched)

    def _is_valid_stack_rel(self, rel: str) -> bool:
        if not rel.startswith("stacks/"): return True
        stack = self.cfg.get("stack")
        if not stack: return False
        stack_type = self.cfg.get("stack_type")
        if stack_type and rel.startswith(f"stacks/{stack}/{stack_type}/"): return True
        if rel.startswith(f"stacks/{stack}/base/"): return True
        return False

    def _discover_jinja_items(self) -> list[dict]:
        items = []
        for rel, data, is_j2, origin in self.tmpl_src.iter_files():
            if not is_j2 or self._matches_disabled(rel) or rel.startswith("app-resources/") or posixpath.basename(rel) in {".scaffold-defaults.yaml", "aliases.yaml"}: continue

            if not self._is_valid_stack_rel(rel): continue

            text = data.decode("utf-8", errors="replace")
            meta, inline_template = _extract_annotation(text)

            if (meta or {}).get("on_init") and not self.is_init:
                continue

            dest = (meta or {}).get("dest") or self._strip_package_prefix(rel)[:-3]

            # --- THE TEST EXCLUSION OVERRIDE FOR DAY 0 ---
            if dest.startswith("tests/") and not self.is_init and not (self.cfg.get("tests") or {}).get("targets"): continue

            # NEW: Check meta dict first, fallback to file system permissions
            executable = bool((meta or {}).get("executable", False))
            if not executable and hasattr(origin, "exists") and origin.exists():
                import os
                executable = os.access(origin, os.X_OK)

            items.append({"rel": rel, "inline_template": inline_template, "dest": dest, "context": (meta or {}).get("context", "."), "updatable": bool((meta or {}).get("updatable", True)), "header_managed": (meta or {}).get("header_managed"), "origin": origin, "executable": executable})
        return items

    def _discover_copy_items(self) -> list[dict]:
        items = []
        for rel, data, is_j2, origin in self.tmpl_src.iter_files():
            if is_j2 or self._matches_disabled(rel) or rel.startswith("app-resources/") or posixpath.basename(rel) in {".scaffold-defaults.yaml", "aliases.yaml"}: continue

            if not self._is_valid_stack_rel(rel): continue

            dest = self._strip_package_prefix(rel)
            if dest.startswith("tests/") and not (self.cfg.get("tests") or {}).get("targets"): continue

            # NEW: Check file system permissions
            executable = False
            if hasattr(origin, "exists") and origin.exists():
                import os
                executable = os.access(origin, os.X_OK)

            items.append({"rel": rel, "dest": dest, "bytes": data, "origin": origin, "executable": executable})
        return items

    def _jinja_env_for_inline(self) -> Environment:
        loaders = [FileSystemLoader(str(self.tmpl_src._pkg_root))] if self.tmpl_src and self.tmpl_src._pkg_root else []
        env = Environment(loader=ChoiceLoader(loaders) if loaders else None, undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True)
        env.filters.setdefault("ternary", lambda v, a, b: a if bool(v) else b)
        return env

    def _render_with_help(self, env: Environment, it: dict, ctx: dict) -> str:
        try: return env.from_string(it["inline_template"]).render(**ctx)
        except Exception as e:
            lineno = getattr(e, "lineno", None) or getattr(getattr(e, "node", None), "lineno", None)
            frame = ""
            if lineno:
                lines = it["inline_template"].splitlines()
                start, end = max(0, lineno - 3), min(len(lines), lineno + 2)
                frame = "\n--- snippet around line {} ---\n{}\n".format(lineno, "\n".join(f"{i+1:5d}| {lines[i]}{'  <-- here' if (i + 1) == lineno else ''}" for i in range(start, end)))
            raise RuntimeError(f"Jinja render error in template '{it['rel']}' → output '{it['dest']}':\n{e}\n{frame}") from e

    def _base_from_cfg(self, cfg: dict) -> dict:
        return {k: v for k, v in cfg.items() if k not in ("deps", "tests", "files", "template_packages", "packages", "templates_dir")}
