from __future__ import annotations

import fnmatch
import glob
import hashlib
import os
import posixpath
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined

# ──────────────────────────────────────────────────────────────────────────────
# Small, self‑contained utilities (pure, no I/O side effects)
# ──────────────────────────────────────────────────────────────────────────────

_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")

def _slug(s: str) -> str:
    s = s.strip().replace("_", "-")
    s = _NON_ALNUM.sub("-", s)
    return re.sub(r"-{2,}", "-", s).strip("-").lower() or "project"

def _snake(s: str) -> str:
    s = s.strip().replace("-", "_")
    s = _NON_ALNUM.sub("_", s)
    return re.sub(r"_{2,}", "_", s).strip("_").lower() or "project"

def _camel(s: str) -> str:
    parts = re.split(r"[^0-9A-Za-z]+", s)
    return "".join(p[:1].upper() + p[1:].lower() for p in parts if p) or "Project"

def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def _coerce_list(x) -> list[Any]:
    if x is None:
        return []
    if isinstance(x, (list, tuple)):
        return list(x)
    return [x]

def _dedupe(seq: Iterable[Any]) -> list[Any]:
    out, seen = [], set()
    for s in seq:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out

def _deep_merge(a, b):
    if isinstance(a, list) and isinstance(b, list):
        return _dedupe(a + b)
    if not isinstance(a, dict) or not isinstance(b, dict):
        return b if b is not None else a
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out.get(k), v)
    return out

def _ensure_trailing_newline(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    if not s.endswith("\n"):
        return s + "\n"
    return s

def _diff(old: bytes, new: bytes, path: str) -> str:
    import difflib
    old_lines = old.decode("utf-8", errors="replace").splitlines(keepends=True)
    new_lines = new.decode("utf-8", errors="replace").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}")
    )

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

    def _is_blank(s: str) -> bool:
        return not s.strip()

    if style["mode"] == _LINE:
        pref = style["prefix"]
        start = i
        j = start
        while j < n and (_is_blank(lines[j]) or lines[j].lstrip().startswith(pref)):
            j += 1
        top = lines[start:j]
        if not top:
            return text
        spdx_idxs = [k for k, ln in enumerate(top) if "SPDX-" in ln]
        if not spdx_idxs:
            return text
        last = spdx_idxs[-1]
        k = last + 1
        while k < len(top) and (not _is_blank(top[k])) and top[k].lstrip().startswith(pref):
            k += 1
        if k < len(top) and _is_blank(top[k]):
            k += 1
        end = start + k
        return "\n".join(lines[:start] + lines[end:])
    else:
        open_, close_ = style["open"], style["close"]
        start = i
        if start < n and lines[start].strip().startswith(open_):
            j = start + 1
            while j < n and not lines[j].strip().endswith(close_):
                j += 1
            j = min(j + 1, n)
            block = lines[start:j]
            if any("SPDX-" in ln for ln in block):
                end = j
                if end < n and not lines[end].strip():
                    end += 1
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
    def __init__(self, fs_dir: Path | None = None, pkg_rel: str | None = "templates"):
        import importlib.resources as resources
        self.fs_dir: Path | None = fs_dir if (fs_dir and fs_dir.is_dir()) else None
        self._pkg_root = None
        self._resources = resources
        if pkg_rel:
            try:
                self._pkg_root = resources.files("scaffold_repo").joinpath(pkg_rel)
                _ = list(self._pkg_root.iterdir())
            except Exception:
                self._pkg_root = None
        if self.fs_dir is None and self._pkg_root is None:
            dev = Path(__file__).resolve().parents[2] / "templates"
            if dev.is_dir():
                self.fs_dir = dev

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
            except Exception:
                pass

        if self._pkg_root:
            tgt = self._pkg_root
            for part in clean_prefix.split("/"):
                if part: tgt = tgt.joinpath(part)
            if tgt.is_dir():
                scan_node(tgt, clean_prefix)

        if self.fs_dir:
            tgt = self.fs_dir
            for part in clean_prefix.split("/"):
                if part: tgt = tgt / part
            if tgt.is_dir():
                scan_node(tgt, clean_prefix)

        return _dedupe(out)

    def _load_logical_path(self, rel_path: str, seen: set[str] | None = None) -> dict:
        if seen is None:
            seen = set()

        rel_path = posixpath.normpath(rel_path)
        if rel_path in seen:
            return {}

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

            includes = data.pop("includes", [])
            if not isinstance(includes, list):
                includes = [includes]

            root_data = data.pop("root", {}) if isinstance(data, dict) else {}

            rel_no_ext = posixpath.splitext(rel_path)[0]
            parts = rel_no_ext.split("/", 1)
            if len(parts) == 2 and parts[0] in ("libraries", "apps", "licenses", "library-templates", "app-templates"):
                folder = parts[0].replace("-", "_")
                key = parts[1]
                if folder not in data:
                    data = {folder: {key: data}}

            if root_data:
                data = _deep_merge(root_data, data)

            def _process_ref(ref: Any, default_folder: str = "", add_include: bool = True) -> Any:
                if not isinstance(ref, str): return ref

                is_ref = ("/" in ref) or ref.endswith((".yaml", ".yml")) or bool(default_folder)
                if is_ref:
                    inc_path = ref if ref.endswith((".yaml", ".yml")) else f"{ref}.yaml"
                    if default_folder and not inc_path.startswith((f"{default_folder}/", "./", "../", "/")):
                        inc_path = f"{default_folder}/{inc_path}"

                    # Only append to includes if explicitly allowed
                    if add_include and inc_path not in includes:
                        includes.append(inc_path)

                    ret_key = posixpath.splitext(inc_path)[0]
                    if default_folder and ret_key.startswith(f"{default_folder}/"):
                        ret_key = ret_key[len(default_folder)+1:]
                    elif default_folder == "library-templates" and ret_key.startswith("library_templates/"):
                        ret_key = ret_key[len("library_templates/"):]
                    elif default_folder == "app-templates" and ret_key.startswith("app_templates/"):
                        ret_key = ret_key[len("app_templates/"):]
                    return ret_key
                return ref

            if "profile" in data:
                data["profile"] = _process_ref(data["profile"], "profiles")
            if "license_profile" in data:
                data["license_profile"] = _process_ref(data["license_profile"], "licenses")

            for folder_key in ["libraries", "apps", "licenses", "library_templates", "app_templates"]:
                raw_dict = data.get(folder_key)
                if isinstance(raw_dict, dict):
                    new_dict = {}
                    for k, v in raw_dict.items():
                        clean_name = str(k)
                        if isinstance(v, dict):
                            if "name" not in v: v["name"] = posixpath.basename(clean_name)

                            if "template" in v: v["template"] = _process_ref(v["template"], "library-templates" if folder_key=="libraries" else "app-templates")
                            if "profile" in v: v["profile"] = _process_ref(v["profile"], "profiles")
                            if "license_profile" in v: v["license_profile"] = _process_ref(v["license_profile"], "licenses")

                            # Block peer library configs from merging into the global scope
                            if "depends_on" in v: v["depends_on"] = [_process_ref(d, "libraries", add_include=False) for d in _coerce_list(v["depends_on"])]

                            if "license_extras" in v and isinstance(v["license_extras"], dict):
                                v["license_extras"] = {ek: _process_ref(ev, "licenses") for ek, ev in v["license_extras"].items()}
                            if "license_overrides" in v and isinstance(v["license_overrides"], dict):
                                v["license_overrides"] = {ek: _process_ref(ev, "licenses") for ek, ev in v["license_overrides"].items()}

                            if "binaries" in v and isinstance(v["binaries"], list):
                                for b in v["binaries"]:
                                    if isinstance(b, dict):
                                        for bk, bv in b.items():
                                            if isinstance(bv, dict) and "depends_on" in bv:
                                                # Block peer library configs from merging into the global scope
                                                bv["depends_on"] = [_process_ref(d, "libraries", add_include=False) for d in _coerce_list(bv["depends_on"])]
                        new_dict[clean_name] = v
                    data[folder_key] = new_dict

            base = {}
            for inc in includes:
                if not inc: continue
                inc_str = str(inc)

                if inc_str.startswith("/"):
                    new_rel = posixpath.normpath(inc_str.lstrip("/"))
                elif inc_str.startswith("./") or inc_str.startswith("../"):
                    new_rel = posixpath.normpath(posixpath.join(posixpath.dirname(rel_path), inc_str))
                else:
                    new_rel = posixpath.normpath(inc_str)

                inc_data = self._load_logical_path(new_rel, seen_next)
                base = _deep_merge(base, inc_data)

            return _deep_merge(base, data)

        pkg_data = {}
        if self._pkg_root:
            try:
                cand = self._pkg_root
                for part in rel_path.split("/"):
                    if part and part != ".":
                        cand = cand.joinpath(part)
                if cand.is_file():
                    pkg_data = parse_and_resolve(cand)
            except Exception:
                pass

        fs_data = {}
        if self.fs_dir:
            cand = (self.fs_dir / rel_path).resolve()
            try:
                cand.relative_to(self.fs_dir)
                if cand.is_file():
                    fs_data = parse_and_resolve(cand)
            except ValueError:
                pass

        if not pkg_data and not fs_data:
            actual_basename = posixpath.basename(rel_path)
            if actual_basename != ".scaffold-defaults.yaml" and rel_path != ".scaffold-defaults.yaml":
                print(f"Warning: Included file '{rel_path}' not found in templates or overlay.", file=sys.stderr)

        return _deep_merge(pkg_data, fs_data)

    def iter_files(self):
        SKIP_DIRS = {"libraries", "apps", "profiles", "licenses", "library-templates", "app-templates"}
        files_map = {}

        if self._pkg_root:
            def walk(node, prefix=""):
                for child in node.iterdir():
                    if child.is_dir() and not prefix and child.name in SKIP_DIRS:
                        continue
                    name = child.name
                    rel = f"{prefix}{name}" if prefix else name
                    if child.is_file():
                        files_map[rel] = (child.read_bytes(), rel.endswith(".j2"), "pkg", child)
                    elif child.is_dir():
                        walk(child, rel + "/")
            walk(self._pkg_root)

        if self.fs_dir:
            def walk_fs(dir_path, prefix=""):
                try:
                    for entry in os.scandir(dir_path):
                        if entry.is_dir(follow_symlinks=False):
                            if not prefix and entry.name in SKIP_DIRS:
                                continue
                            walk_fs(entry.path, f"{prefix}{entry.name}/")
                        elif entry.is_file(follow_symlinks=False):
                            rel = f"{prefix}{entry.name}"
                            with open(entry.path, "rb") as f:
                                files_map[rel] = (f.read(), rel.endswith(".j2"), "fs", Path(entry.path))
                except PermissionError:
                    pass
            walk_fs(self.fs_dir)

        for rel, (data, is_j2, origin, _path) in files_map.items():
            yield rel, data, is_j2, origin

    def load_defaults_yaml(self) -> dict:
        return self._load_logical_path(".scaffold-defaults.yaml")

    def read_resource_text(self, rel_path: str) -> str | None:
        for rel, data, _is_j2, _origin in self.iter_files():
            if rel == rel_path:
                return data.decode("utf-8", errors="replace")
        return None

_FM_RE = re.compile(r"^\s*\{#-?\s*(.*?)\s*-?#\}\s*", re.S)

def _extract_annotation(text: str):
    m = _FM_RE.match(text)
    if not m:
        return None, text
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None, text

    meta = {}
    if isinstance(data, dict):
        val = data.get("scaffold-repo", data.get("scaffold_repo", data))
        if isinstance(val, str):
            meta = {"context": val}
        elif isinstance(val, dict):
            meta = dict(val)
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

class ConfigReader:
    def __init__(self, repo: Path, *, project_name: str | None = None, templates_dir: str | None = None):
        self.repo = repo.resolve()
        self.cfg: dict = {}
        self.project_name: str | None = project_name
        self.templates_dir = templates_dir
        fs_dir = None
        if templates_dir:
            fs_dir = Path(templates_dir)
            if not fs_dir.is_absolute():
                fs_dir = (self.repo / fs_dir)
            fs_dir = fs_dir.resolve()
            if not fs_dir.is_dir():
                raise FileNotFoundError(f"templates_dir not found: {fs_dir}")

        self.tmpl_src = TemplateSource(fs_dir=fs_dir, pkg_rel=None if fs_dir else "templates")
        self.enabled_packages: set[str] = set()
        self.package_patterns: dict[str, list[str]] = {}

    def load(self) -> None:
        self.cfg = self.tmpl_src.load_defaults_yaml() or {}

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

        self._select_project()
        self._render_contributors()
        self._normalize_keys_autofill()
        self._expand_library_templates()
        self._augment_with_libraries_tests_apps()
        self._compute_package_switches()

    @property
    def effective_config(self) -> dict:
        return self.cfg

    def read_resource_text(self, rel_path: str) -> str | None:
        return self.tmpl_src.read_resource_text(rel_path) if self.tmpl_src else None

    def _strip_package_prefix(self, rel: str) -> str:
        for pkg_name, pats in self.package_patterns.items():
            if pkg_name == "resources":
                continue
            for pat in pats:
                if pat.endswith("/**"):
                    prefix = pat[:-2]
                    if rel.startswith(prefix):
                        return rel[len(prefix):]
        return rel

    def plan_jinja(self, *, show_diffs: bool = False) -> list[PlanItem]:
        env = self._jinja_env_for_inline()
        items = self._discover_jinja_items()
        plan: list[PlanItem] = []
        for it in items:
            ctx = self._build_ctx_inherited(it["context"])
            new_text = self._render_with_help(env, it, ctx)
            target = self.repo / it["dest"]
            old_text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
            hm_meta = it.get("header_managed")
            header_managed = _header_managed_default(it["dest"]) if hm_meta is None else bool(hm_meta)
            cmp_new = _normalize_for_cmp(new_text, target, header_managed)
            cmp_old = _normalize_for_cmp(old_text, target, header_managed)
            status = "create" if not target.exists() else ("update" if cmp_old != cmp_new else "unchanged")
            diff_text = _diff(old_text.encode("utf-8"), new_text.encode("utf-8"), it["dest"]) if show_diffs and status in ("create", "update") else ""
            is_exec = it.get("executable", False)
            plan.append(PlanItem("jinja", it["dest"], status, it.get("updatable", True), diff_text, new_text.encode("utf-8"), _sha256(it["inline_template"].encode("utf-8")), it["context"], header_managed, is_exec))
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
        if not isinstance(contribs, dict) or not contribs:
            return
        env = Environment(undefined=StrictUndefined, autoescape=False)
        def render_fields(fields: dict) -> dict:
            ctx = {**self.cfg, **fields}
            out = {}
            for k, v in fields.items():
                if isinstance(v, str):
                    try:
                        out[k] = env.from_string(v).render(**ctx)
                    except Exception:
                        out[k] = v
                else:
                    out[k] = v
            return out
        self.cfg["contributors"] = {key: (render_fields(val) if isinstance(val, dict) else val) for key, val in contribs.items()}

    def _select_project(self) -> None:
        proj = self.project_name or self.cfg.get("project_name")
        if not proj:
            return

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
            if picked:
                break

        if not picked:
            return

        self.cfg = _deep_merge(self.cfg, picked)
        if not self.cfg.get("project_name"):
            self.cfg["project_name"] = picked.get("name") or proj_base

    def _normalize_keys_autofill(self) -> None:
        cm = dict(self.cfg.get("cmake") or {})
        ts = dict(self.cfg.get("tests") or {})
        lib_srcs = self.cfg.get("library_sources") or cm.get("sources")
        if not lib_srcs:
            exts = {".c", ".cc"}
            src_root = self.repo / "src"
            auto = [p.relative_to(self.repo).as_posix() for p in src_root.rglob("*") if p.is_file() and p.suffix.lower() in exts] if src_root.is_dir() else []
            if auto: lib_srcs = sorted(auto)

        norm_srcs = [str(s) for s in (lib_srcs if isinstance(lib_srcs, (list, tuple)) else [lib_srcs])] if lib_srcs else []
        self.cfg["library_sources"] = cm["sources"] = norm_srcs

        test_tgts = self.cfg.get("test_targets") or ts.get("test_targets") or ts.get("targets")
        if not test_tgts:
            tests_src = self.repo / "tests" / "src"
            exts = {".c", ".cc"}
            auto = [{"name": p.stem, "sources": [f"src/{p.name}"]} for p in tests_src.glob("test_*") if p.is_file() and p.suffix.lower() in exts] if tests_src.is_dir() else []
            test_tgts = sorted(auto, key=lambda t: t["name"]) if auto else []

        ts["targets"] = test_tgts or []
        self.cfg["cmake"], self.cfg["tests"] = cm, ts

    def _expand_library_templates(self) -> None:
        templates = self.cfg.get("library_templates") or {}
        if not templates:
            return
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

            if isinstance(raw_libs, dict):
                lib["name"] = lib.get("name") or posixpath.basename(str(key))

            tmpl_name = lib.get("template")
            if not tmpl_name or tmpl_name not in templates:
                out[key] = lib
                continue

            merged = _deep_merge(to_dict(templates[tmpl_name]), lib)

            nm = str(merged.get("name") or posixpath.basename(str(key))).strip()
            derived = {
                "slug": _slug(nm), "snake": _snake(nm), "camel": _camel(nm),
                "cmake_project": _snake(nm) or nm, "project_slug": _slug(nm), "project_camel": _camel(nm),
            }
            if "project_name" not in merged: derived["project_name"] = derived["cmake_project"]

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
                "raw_key": str(key),  # <-- Required for namespace matching
                "finds": finds, "pkg_configs": pkg_configs, "links": links,
                "depends_raw": list(_coerce_list(item.get("depends_on"))),
            }
            item.setdefault("build_steps", [])
            item.setdefault("kind", "local")

        by_snake = {v["snake"]: k for k, v in idx.items()}
        for k, v in idx.items():
            deps = []
            for d_raw in v.pop("depends_raw", []):
                s = _slug(str(d_raw))
                r = s if s in idx else by_snake.get(_snake(str(d_raw)))
                if r: deps.append(r)
            v["depends"] = _dedupe(deps)
        return idx

    def _collect_transitive(self, idx: dict[str, dict], roots: Iterable[str], *, exclude_roots=False) -> list[str]:
        roots = [r for r in roots if r in idx]
        seen, stack = set(), list(roots)
        while stack:
            s = stack.pop()
            if s in seen: continue
            seen.add(s)
            stack.extend(d for d in idx[s]["depends"] if d in idx)
        if exclude_roots:
            seen.difference_update(roots)
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
            n = sorted(queue, key=lambda x: idx[x]["name"].lower()).pop(0)
            queue.remove(n)
            ordered.append(n)
            for m in adj[n]:
                indeg[m] -= 1
                if indeg[m] == 0: queue.append(m)
        if len(ordered) != len(S):
            ordered.extend(sorted([s for s in S if s not in ordered], key=lambda x: idx[x]["name"].lower()))
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
        if not raw_items:
            return []

        b_dict = {}
        if isinstance(raw_items, list):
            for item in raw_items:
                if isinstance(item, str) and item.strip():
                    name = item.strip()
                    b_dict[name] = {"sources": [f"{default_src_dir}/{name}.c" if default_src_dir else f"{name}.c"]}
                elif isinstance(item, dict) and item:
                    if "name" in item:
                        name = str(item.pop("name")).strip()
                        b_dict[name] = item
                    else:
                        k = next(iter(item.keys()))
                        v = item[k]
                        if isinstance(v, list):
                            b_dict[str(k).strip()] = {"sources": v}
                        elif isinstance(v, str):
                            b_dict[str(k).strip()] = {"sources": [v]}
                        elif isinstance(v, dict):
                            b_dict[str(k).strip()] = v
        elif isinstance(raw_items, dict):
            for k, v in raw_items.items():
                if isinstance(v, list):
                    b_dict[str(k).strip()] = {"sources": v}
                elif isinstance(v, str):
                    b_dict[str(k).strip()] = {"sources": [v]}
                elif isinstance(v, dict):
                    b_dict[str(k).strip()] = v
                elif v is None:
                    b_dict[str(k).strip()] = {"sources": [f"{default_src_dir}/{k}.c" if default_src_dir else f"{k}.c"]}
        else:
            name = str(raw_items).strip()
            b_dict[name] = {"sources": [f"{default_src_dir}/{name}.c" if default_src_dir else f"{name}.c"]}

        norm = []
        for name, conf in b_dict.items():
            if not name: continue

            raw_sources = _coerce_list(conf.get("sources") or [f"{default_src_dir}/{name}.c" if default_src_dir else f"{name}.c"])
            expanded_sources = []
            include_dirs = set()

            for src in raw_sources:
                src_str = str(src).strip()
                if "*" in src_str or "?" in src_str:
                    # Globbing is handled safely across OS and directories
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

            ent = {
                "name": name,
                "sources": _dedupe(expanded_sources),
                "include_dirs": sorted(list(include_dirs))
            }
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
            s = _slug(str(nm))
            if s in idx: out.append(s)
            elif _snake(str(nm)) in by_snake: out.append(by_snake[_snake(str(nm))])
        return _dedupe(out)

    def _derive_suite_deps_from_libs(self, lib_slugs: list[str], idx: dict) -> tuple[list[str], list[str]]:
        return _dedupe([fp for s in lib_slugs for fp in idx[s]["finds"]]), _dedupe([lk for s in lib_slugs for lk in idx[s]["links"]])

    def _augment_tests_cfg(self, cfg: dict, idx: dict, proj_slug: str) -> dict:
        ts = dict(cfg.get("tests") or {})
        raw_targets = ts.get("targets") or ts.get("test_targets")

        abs_test_dir = self.repo / "tests"
        norm_targets = self._normalize_build_targets(raw_targets, abs_test_dir, "src")

        if norm_targets:
            ts["targets"] = ts["test_targets"] = norm_targets

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

            abs_app_dir = self.repo / ctx["_apps_dest_dir"]
            ctx["binaries"] = self._normalize_build_targets(ctx.get("binaries"), abs_app_dir, "src")

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

    def _augment_with_libraries_tests_apps(self) -> None:
        cfg = self.cfg
        idx = self._build_library_index(cfg)
        if not idx:
            return

        proj_slug = _slug(cfg.get("project_name", "") or cfg.get("project_slug", ""))
        if proj_slug not in idx:
            ps = _snake(cfg.get("project_name", ""))
            proj_slug = next((s for s, v in idx.items() if v["snake"] == ps), proj_slug)

        if proj_slug in idx:
            direct = idx[proj_slug]["depends"]
            cm = dict(cfg.get("cmake") or {})
            if direct:
                cm.setdefault("find_packages", _dedupe([fp for d in direct for fp in (idx[d].get("finds") or []) if fp]))
                if not cm.get("pkg_config_deps"):
                    pc_map = {mod: idx[d]["snake"] for d in direct for mod in (idx[d].get("pkg_configs") or [])}
                    cm["pkg_config_deps"] = [{"module": m, "target": t} for m, t in pc_map.items()]
                cm.setdefault("link_libraries", _dedupe([lk for d in direct for lk in idx[d]["links"]]))
                cm.setdefault("deps_for_config", _dedupe([fp.strip().split()[0] for d in direct for fp in (idx[d].get("finds") or []) if fp.strip()]))

                order = self._toposort_subset(idx, set(self._collect_transitive(idx, [proj_slug], exclude_roots=True)))
                cm["libraries"] = [idx[s]["item"] for s in order]
                cm["apt_packages"] = self._gather_apt_packages(cfg, idx, proj_slug)
                self.cfg["cmake"] = cm

        self.cfg = self._augment_tests_cfg(self.cfg, idx, proj_slug)
        self.cfg = self._normalize_apps_cfg(self.cfg, idx, proj_slug)

        raw_dev = self.cfg.get("dev_packages") or {}
        dev_pkgs = []
        if isinstance(raw_dev, dict):
            for pkg, constraint in raw_dev.items():
                if constraint is False or constraint is None: continue
                elif constraint is True: dev_pkgs.append(str(pkg))
                else:
                    val = str(constraint).strip()
                    dev_pkgs.append(f"{pkg}{val}" if val and val[0] in "=<>~" else f"{pkg}={val}")
        else:
            dev_pkgs = _coerce_list(raw_dev)

        if dev_pkgs:
            cm = dict(self.cfg.get("cmake") or {})
            cm["apt_dev_packages"] = _dedupe([p for p in dev_pkgs if p.strip()])
            self.cfg["cmake"] = cm

        cm = dict(self.cfg.get("cmake") or {})
        for k in ["sources", "libraries", "deps_for_config", "apt_packages", "apt_dev_packages", "find_packages", "pkg_config_deps", "link_libraries", "depends_on"]:
            cm.setdefault(k, [])
        self.cfg["cmake"] = cm

    def _compute_package_switches(self) -> None:
        merged = self.cfg.get("template_packages") or {}

        env = Environment(undefined=StrictUndefined)
        def render_pat(p):
            if "{{" in p:
                try: return env.from_string(p).render(**self.cfg)
                except Exception: return p
            return p

        self.package_patterns = {name: [render_pat(str(x)) for x in _coerce_list(pats)] for name, pats in merged.items()}

        raw_pkgs = self.cfg.get("packages") or {}
        enabled, flavors = set(), {}
        if isinstance(raw_pkgs, dict):
            for pkg, val in raw_pkgs.items():
                if val is False or val is None: continue
                enabled.add(pkg)
                if isinstance(val, str) and val.lower() != "true":
                    flavors[pkg] = val
        else:
            enabled = set(str(x) for x in _coerce_list(raw_pkgs))

        self.enabled_packages = enabled
        self.cfg["package_flavors"] = flavors

    def _matches_disabled(self, rel: str) -> bool:
        matched = {pkg for pkg, pats in self.package_patterns.items() for pat in pats if fnmatch.fnmatch(rel, pat)}
        return bool(matched) and not any(pkg in self.enabled_packages for pkg in matched)

    def _discover_jinja_items(self) -> list[dict]:
        items = []
        for rel, data, is_j2, origin in self.tmpl_src.iter_files():
            if not is_j2 or self._matches_disabled(rel) or rel.startswith("app-resources/"):
                continue

            text = data.decode("utf-8", errors="replace")
            meta, inline_template = _extract_annotation(text)
            meta = meta or {}

            sub_rel = self._strip_package_prefix(rel)
            dest = meta.get("dest") or sub_rel[:-3]

            # Skip generating test scaffolding if there are no test targets
            test_tgts = (self.cfg.get("tests") or {}).get("targets") or []
            if dest.startswith("tests/") and not test_tgts:
                continue

            context_key = meta.get("context", ".")
            updatable = bool(meta.get("updatable", True))
            header_managed = meta.get("header_managed")

            executable = False
            if origin == "fs" and self.tmpl_src.fs_dir:
                src_path = self.tmpl_src.fs_dir / rel
                if src_path.exists():
                    executable = os.access(src_path, os.X_OK)

            items.append({
                "rel": rel,
                "inline_template": inline_template,
                "dest": dest,
                "context": context_key,
                "updatable": updatable,
                "header_managed": header_managed,
                "origin": origin,
                "executable": executable
            })
        return items

    def _discover_copy_items(self) -> list[dict]:
        items = []
        for rel, data, is_j2, origin in self.tmpl_src.iter_files():
            if is_j2 or self._matches_disabled(rel) or rel.startswith("app-resources/"):
                continue

            if rel == ".scaffold-defaults.yaml":
                continue

            dest = self._strip_package_prefix(rel)

            # Skip generating test scaffolding if there are no test targets
            test_tgts = (self.cfg.get("tests") or {}).get("targets") or []
            if dest.startswith("tests/") and not test_tgts:
                continue

            executable = False
            if origin == "fs" and self.tmpl_src.fs_dir and (src_path := self.tmpl_src.fs_dir / rel).exists():
                executable = os.access(src_path, os.X_OK)

            items.append({"rel": rel, "dest": dest, "bytes": data, "origin": origin, "executable": executable})
        return items

    def _jinja_env_for_inline(self) -> Environment:
        loaders = [FileSystemLoader(str(self.tmpl_src.fs_dir))] if self.tmpl_src and self.tmpl_src.fs_dir else []
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
            raise RuntimeError(f"Jinja render error in template '{it['rel']}' → output '{it['dest']}' (context='{it['context']}'):\n{e}\n{frame}") from e

    def _base_from_cfg(self, cfg: dict) -> dict:
        return {k: v for k, v in cfg.items() if k not in ("cmake", "tests", "files", "template_packages", "packages", "templates_dir")}

    def _build_ctx_inherited(self, key: str | None) -> dict:
        ctx = _deep_merge(self._base_from_cfg(self.cfg), {} if not key or key == "." else (self.cfg.get(key) or {}))
        ctx.setdefault("project_name", self.cfg.get("project_name") or "project")
        ctx.setdefault("project_slug", _slug(ctx.get("project_name", "project")))
        ctx.setdefault("cmake_project", _snake(ctx["project_slug"]))
        ctx.setdefault("project_camel", _camel(ctx.get("project_name", "project")))
        ctx.setdefault("project_title", ctx.get("project_title", ctx.get("project_name")))
        ctx.setdefault("year", str(self.cfg.get("date") or ctx.get("date") or "")[:4] if len(str(self.cfg.get("date") or ctx.get("date") or "")) >= 4 else "2025")
        ctx.setdefault("cmake", self.cfg.get("cmake") or {})
        ctx.setdefault("tests", self.cfg.get("tests") or {})
        ctx.setdefault("test_targets", (self.cfg.get("tests") or {}).get("test_targets") or (self.cfg.get("tests") or {}).get("targets") or [])
        return ctx

    def _plan_apps_resources(self, *, show_diffs: bool) -> list[PlanItem]:
        apps = self.cfg.get("apps") or {}
        contexts = [k for k in apps.keys() if k != "context"]
        if not contexts: return []

        all_app_resources = []
        for rel, data, is_j2, origin in self.tmpl_src.iter_files():
            if rel.startswith("app-resources/") and not rel.endswith("/"):
                all_app_resources.append((rel, data, is_j2, origin))

        if not all_app_resources: return []

        env, plan, base = self._jinja_env_for_inline(), [], self._build_ctx_inherited("cmake")

        for ctx_name in contexts:
            ctx = dict(apps.get(ctx_name) or {})
            dest_dir = ctx.get("_apps_dest_dir") or self._compute_app_dest_dir(str((apps.get("context") or {}).get("dest") or "apps"), ctx.get("dest"), ctx_name)
            rctx = _deep_merge(base, ctx)

            app_flavor = ctx.get("flavor") or ctx.get("app_flavor") or self.cfg.get("repo_flavor")

            active_prefix = f"app-resources/{app_flavor}/" if app_flavor else None
            global_prefix = "app-resources/global/"

            rctx.setdefault("project_name", self.cfg.get("project_name") or "project")
            rctx.setdefault("project_slug", _slug(rctx["project_name"]))
            rctx.setdefault("cmake_project", _snake(rctx["project_slug"]))
            rctx.setdefault("language", self.cfg.get("language", "C"))
            rctx.setdefault("app_project_name", f"{base.get('project_name','project')}_{ctx_name}")
            rctx.setdefault("app_languages", base.get("language", "C"))

            for rel, data, is_j2, origin in all_app_resources:
                root_prefix = None
                if active_prefix and rel.startswith(active_prefix):
                    root_prefix = active_prefix
                elif rel.startswith(global_prefix):
                    root_prefix = global_prefix

                if not root_prefix:
                    continue

                sub_rel = rel[len(root_prefix):]
                dest_rel = f"{dest_dir}/{sub_rel[:-3] if (is_j2 and sub_rel.endswith('.j2')) else sub_rel}"
                target = self.repo / dest_rel

                if is_j2:
                    raw_tpl = data.decode("utf-8", errors="replace")
                    try: new_bytes = env.from_string(raw_tpl).render(**rctx).encode("utf-8")
                    except Exception as e: raise RuntimeError(f"Jinja render error in apps resource '{rel}' → '{dest_rel}': {e}") from e
                    tmpl_sha = _sha256(raw_tpl.encode("utf-8"))
                else:
                    new_bytes, tmpl_sha = data, _sha256(data)

                old_text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
                hm = _header_managed_default(dest_rel)
                cmp_new, cmp_old = _normalize_for_cmp(new_bytes.decode("utf-8", errors="replace"), target, hm), _normalize_for_cmp(old_text, target, hm)
                status = "create" if not target.exists() else ("update" if cmp_old != cmp_new else "unchanged")
                diff_text = _diff(old_text.encode("utf-8"), cmp_new.encode("utf-8"), dest_rel) if show_diffs and status in ("create", "update") else ""

                is_exec = False
                if origin == "fs" and self.tmpl_src.fs_dir and (src_path := self.tmpl_src.fs_dir / rel).exists():
                    is_exec = os.access(src_path, os.X_OK)

                plan.append(PlanItem("jinja" if is_j2 else "copy", dest_rel, status, True, diff_text, new_bytes, tmpl_sha, f"apps.{ctx_name}", hm, is_exec))
        return plan
