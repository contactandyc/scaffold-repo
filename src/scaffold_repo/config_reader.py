from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

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

def _coerce_list(x) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, (list, tuple)):
        return list(x)
    return [x]

def _dedupe(seq: Iterable[Any]) -> List[Any]:
    out, seen = [], set()
    for s in seq:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out

def _deep_merge(a, b):
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

# Supported header‑managed file types for SPDX stripping during comparisons
_OSS_HEADER_EXTS = {
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".cxx",
    ".java",
    ".js",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".swift",
    ".kt",
    ".cs",
    ".cmake",
    ".mk",
    ".make",
}
def _header_managed_default(dest: str) -> bool:
    p = Path(dest)
    return p.name == "CMakeLists.txt" or (p.suffix.lower() in _OSS_HEADER_EXTS)

# Minimal comment style detector for stripping SPDX when diffing
_LINE = "line"
_BLOCK = "block"
def _comment_style_for(path: Path) -> Dict[str, str]:
    name = path.name.lower()
    ext = path.suffix.lower()
    if name == "cmakelists.txt" or ext == ".cmake" or name.startswith("makefile") or ext in {".mk", ".make"}:
        return {"mode": _LINE, "prefix": "#"}
    if ext in {".py", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"}:
        return {"mode": _LINE, "prefix": "#"}
    if ext in {".sql"}:
        return {"mode": _LINE, "prefix": "--"}
    if ext in {
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cc",
        ".cxx",
        ".java",
        ".js",
        ".ts",
        ".tsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rs",
        ".swift",
        ".kt",
        ".cs",
    }:
        return {"mode": _LINE, "prefix": "//"}
    if ext in {".html", ".xml", ".xsd", ".svg"}:
        return {"mode": _BLOCK, "open": "<!--", "close": "-->"}
    if ext in {".css", ".scss"}:
        return {"mode": _BLOCK, "open": "/*", "close": "*/"}
    return {"mode": _LINE, "prefix": "#"}

def _strip_spdx_for_compare(path: Path, text: str) -> str:
    """
    Remove the leading SPDX header (if present) to keep template diffs clean.
    The logic mirrors the validator but is intentionally simpler here.
    """
    style = _comment_style_for(path)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    i = 1 if (lines and lines[0].startswith("#!")) else 0
    n = len(lines)
    if style["mode"] == _LINE:
        p = style["prefix"]
        # scan top comment+blank run
        start = i
        j = start
        while j < n and (not lines[j].strip() or lines[j].lstrip().startswith(p)):
            j += 1
        top = lines[start:j]
        if not any("SPDX-" in ln for ln in top):
            return text
        # cut that top block
        return "\n".join(lines[:start] + lines[j:])
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
                # drop block + single trailing blank line
                if j < n and not lines[j].strip():
                    j += 1
                return "\n".join(lines[:start] + lines[j:])
    return text

def _strip_spdx_for_compare(path: Path, text: str) -> str:
    """
    Remove only the SPDX header region at the top (plus at most one trailing blank line),
    not the entire top comment run. This keeps non‑SPDX intro comments intact.
    """
    style = _comment_style_for(path)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    i = 1 if (lines and lines[0].startswith("#!")) else 0
    n = len(lines)

    def _is_blank(s: str) -> bool:
        return not s.strip()

    if style["mode"] == _LINE:
        pref = style["prefix"]
        # Find the top comment/blank run
        start = i
        j = start
        while j < n and (_is_blank(lines[j]) or lines[j].lstrip().startswith(pref)):
            j += 1
        top = lines[start:j]
        if not top:
            return text
        # If there is no SPDX in that run, leave text unchanged
        spdx_idxs = [k for k, ln in enumerate(top) if "SPDX-" in ln]
        if not spdx_idxs:
            return text
        # Header region ends after the last SPDX line and any immediately following
        # non-blank comment lines; include at most one trailing blank line.
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
    # collapse trailing newlines to one
    return re.sub(r"\n+\Z", "\n", (text.rstrip("\n") + "\n"))

# ──────────────────────────────────────────────────────────────────────────────
# Template source (packaged defaults OR user‑provided templates_dir)
# ──────────────────────────────────────────────────────────────────────────────

class TemplateSource:
    """
    Abstraction over the template tree. Supports:
      - A filesystem dir (templates_dir from repo config), OR
      - The packaged 'templates' tree inside this package (for defaults).
    """
    def __init__(self, fs_dir: Optional[Path] = None, pkg_rel: Optional[str] = "templates"):
        import importlib.resources as resources
        self.fs_dir: Optional[Path] = fs_dir if (fs_dir and fs_dir.is_dir()) else None
        self._pkg_root = None
        self._resources = resources
        if pkg_rel:
            try:
                self._pkg_root = resources.files("scaffold_repo").joinpath(pkg_rel)
                # touch to ensure it exists
                _ = list(self._pkg_root.iterdir())
            except Exception:
                self._pkg_root = None
        if self.fs_dir is None and self._pkg_root is None:
            # dev fallback (repo checkout structure)
            dev = Path(__file__).resolve().parents[2] / "templates"
            if dev.is_dir():
                self.fs_dir = dev

    def iter_files(self):
        """Yield (rel_path, data_bytes, is_j2, origin)."""
        if self.fs_dir:
            base = self.fs_dir
            for p in base.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(base).as_posix()
                    yield rel, p.read_bytes(), rel.endswith(".j2"), "fs"
            return
        if self._pkg_root:
            def walk(node, prefix=""):
                for child in node.iterdir():
                    name = child.name
                    rel = f"{prefix}{name}" if not prefix else f"{prefix}{name}"
                    if child.is_file():
                        yield rel, child.read_bytes(), rel.endswith(".j2"), "pkg"
                    elif child.is_dir():
                        yield from walk(child, rel + "/")
            yield from walk(self._pkg_root)

    def load_defaults_yaml(self) -> dict:
        """Return defaults from templates/scaffold-repo.yaml."""
        if self.fs_dir and (self.fs_dir / "scaffold-repo.yaml").is_file():
            return yaml.safe_load((self.fs_dir / "scaffold-repo.yaml").read_text(encoding="utf-8")) or {}
        if self._pkg_root:
            try:
                d = self._pkg_root.joinpath("scaffold-repo.yaml")
                if d.is_file():
                    return yaml.safe_load(d.read_text(encoding="utf-8")) or {}
            except Exception:
                pass
        return {}

    def read_resource_text(self, rel_path: str) -> Optional[str]:
        """Read a resource file inside templates/ by relative path (UTF‑8)."""
        for rel, data, _is_j2, _origin in self.iter_files():
            if rel == rel_path:
                return data.decode("utf-8", errors="replace")
        return None

# ──────────────────────────────────────────────────────────────────────────────
# Jinja front‑matter helper: allow small header to set context/dest/updatable
# ──────────────────────────────────────────────────────────────────────────────

import yaml as _yaml
_FM_RE = re.compile(r"^\s*\{#-?\s*(.*?)\s*-?#\}\s*", re.S)

def _extract_annotation(text: str):
    """
    Return (meta_dict_or_None, body_without_frontmatter).
    Front‑matter sits inside a top‑of‑file Jinja comment, e.g.:
      {#- scaffold-repo: { context: cmake, dest: "CMakeLists.txt", updatable: true } -#}
    """
    m = _FM_RE.match(text)
    if not m:
        return None, text
    data = _yaml.safe_load(m.group(1))
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

# ──────────────────────────────────────────────────────────────────────────────
# Planning structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PlanItem:
    kind: str  # 'jinja'|'copy'
    path: str
    status: str  # 'create'|'update'|'unchanged'
    updatable: bool
    diff: str
    new_bytes: bytes
    template_sha256: str
    context_key: Optional[str] = None
    header_managed: bool = True
    executable: bool = False

# ──────────────────────────────────────────────────────────────────────────────
# Main: ConfigReader — the reusable “understands scaffold-repo YAML + templates” layer
# ──────────────────────────────────────────────────────────────────────────────

class ConfigReader:
    """
    Load & normalize the configuration (defaults + repo config), overlay the
    selected project, derive cmake/tests/apps, and prepare **plans** for how the
    templates would render. Pure I/O for reading + planning — no writes here.

    Public entrypoints:
      - ConfigReader.load()
      - reader.effective_config
      - reader.plan_jinja(), reader.plan_copy()
      - reader.read_resource_text(rel_path)
    """

    def __init__(self, repo: Path, *,
                 project_name: Optional[str] = None,
                 templates_dir: Optional[str] = None):
        self.repo = repo.resolve()

        # Effective/derived
        self.cfg: dict = {}
        self.project_name: Optional[str] = project_name

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

        # Mapping for enabled template packages
        self.enabled_packages: set[str] = set()
        self.package_patterns: Dict[str, List[str]] = {}

    # ── High‑level API ────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load defaults + repo config, overlay project, derive plan context."""
        self._load_repo_and_defaults()
        self._select_project()
        self._render_contributors()
        self._normalize_keys_autofill()
        self._expand_library_templates()
        self._augment_with_libraries_tests_apps()
        self._compute_package_switches()

    @property
    def effective_config(self) -> dict:
        """Deep‑copy not needed for typical usage; return derived dict."""
        return self.cfg

    def read_resource_text(self, rel_path: str) -> Optional[str]:
        return self.tmpl_src.read_resource_text(rel_path) if self.tmpl_src else None

    # ── Planning API (Jinja + non‑Jinja) ─────────────────────────────────────

    def plan_jinja(self, *, show_diffs: bool = False) -> List[PlanItem]:
        """
        Scan templates/*.j2, render with the proper context, and compare with
        filesystem to produce a plan. Includes 'apps' resources (*.j2) too.
        """
        env = self._jinja_env_for_inline()
        items = self._discover_jinja_items()

        plan: List[PlanItem] = []
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

            diff_text = ""
            if show_diffs and status in ("create", "update"):
                diff_text = _diff(old_text.encode("utf-8"), new_text.encode("utf-8"), it["dest"])

            # propagate executable bit from source template file
            is_exec = it.get("executable", False)

            plan.append(
                PlanItem(
                    kind="jinja",
                    path=it["dest"],
                    status=status,
                    updatable=it.get("updatable", True),
                    diff=diff_text,
                    new_bytes=new_text.encode("utf-8"),
                    template_sha256=_sha256(it["inline_template"].encode("utf-8")),
                    context_key=it["context"],
                    header_managed=header_managed,
                    executable=is_exec,
                )
            )

        # Apps resources (*.j2 under resources/apps/) → also render + plan
        plan.extend(self._plan_apps_resources(show_diffs=show_diffs))
        return plan

    def plan_copy(self, *, show_diffs: bool = False) -> List[PlanItem]:
        """Scan non‑Jinja template files and plan verbatim copies."""
        plan: List[PlanItem] = []
        for it in self._discover_copy_items():
            # Normalize template text to end with exactly one newline
            new_text = it["bytes"].decode("utf-8", errors="replace")
            new_norm = _ensure_trailing_newline(new_text)
            new_bytes = new_norm.encode("utf-8")

            target = self.repo / it["rel"]
            old_text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
            old_norm = _ensure_trailing_newline(old_text)

            # Compare normalized→normalized so idempotent runs don’t ping‑pong
            status = "create" if not target.exists() else ("update" if old_norm != new_norm else "unchanged")
            diff_text = ""
            if show_diffs and status in ("create", "update"):
                diff_text = _diff(old_norm.encode("utf-8"), new_norm.encode("utf-8"), it["rel"])

            plan.append(
                PlanItem(
                    kind="copy",
                    path=it["rel"],
                    status=status,
                    updatable=True,
                    diff=diff_text,
                    new_bytes=new_bytes,
                    template_sha256=_sha256(new_bytes),
                    context_key=None,
                    header_managed=False,
                    executable=it.get("executable", False),
                )
            )
        return plan

    # ── Internals: loading & normalization ───────────────────────────────────

    def _load_repo_and_defaults(self) -> None:
        # If repo config specifies templates_dir, point template source there
        self.cfg = self.tmpl_src.load_defaults_yaml() or {}

    def _render_contributors(self) -> None:
        """Render string fields inside contributors with Jinja (self + repo keys)."""
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
        self.cfg["contributors"] = {key: (render_fields(val) if isinstance(val, dict) else val)
                                    for key, val in contribs.items()}

    def _select_project(self) -> None:
        """Overlay the selected library onto top‑level keys to become the active project."""
        proj = self.project_name or self.cfg.get("project_name")
        if not proj:
            return
        libs = self.cfg.get("libraries") or []
        if not isinstance(libs, list):
            return
        want_slug, want_snake = _slug(str(proj)), _snake(str(proj))
        picked = None
        for item in libs:
            nm = str(item.get("name") or "").strip()
            if nm and (nm == proj or _slug(nm) == want_slug or _snake(nm) == want_snake):
                picked = item
                break
        if not picked:
            return
        self.cfg = _deep_merge(self.cfg, picked)
        if not self.cfg.get("project_name"):
            self.cfg["project_name"] = picked.get("name") or str(proj)

    def _normalize_keys_autofill(self) -> None:
        """Bridge legacy keys; auto‑detect library sources/tests if missing."""
        cm = dict(self.cfg.get("cmake") or {})
        ts = dict(self.cfg.get("tests") or {})

        # library_sources (new) <-> cmake.sources (legacy)
        lib_srcs = self.cfg.get("library_sources") or cm.get("sources")
        if not lib_srcs:
            # autodetect from repo/src
            exts = {".c", ".cc"}
            src_root = self.repo / "src"
            auto = []
            if src_root.is_dir():
                for p in src_root.rglob("*"):
                    if p.is_file() and p.suffix.lower() in exts:
                        auto.append(p.relative_to(self.repo).as_posix())
            if auto:
                lib_srcs = sorted(auto)
        if lib_srcs:
            norm = [str(s) for s in (lib_srcs if isinstance(lib_srcs, (list, tuple)) else [lib_srcs])]
            self.cfg["library_sources"] = norm
            cm["sources"] = norm

        # tests.targets (legacy) / tests.test_targets / top-level test_targets
        test_tgts = self.cfg.get("test_targets") or ts.get("test_targets") or ts.get("targets")
        if not test_tgts:
            # autodetect tests/src/test_*.c
            tests_src = self.repo / "tests" / "src"
            exts = {".c", ".cc"}
            auto = []
            if tests_src.is_dir():
                for p in tests_src.glob("test_*"):
                    if p.is_file() and p.suffix.lower() in exts:
                        auto.append({"name": p.stem, "sources": [f"src/{p.name}"]})
            if auto:
                test_tgts = sorted(auto, key=lambda t: t["name"])
        if test_tgts:
            ts["targets"] = test_tgts

        self.cfg["cmake"] = cm
        self.cfg["tests"] = ts

    def _expand_library_templates(self) -> None:
        """Apply 'library_templates' to each library entry (string field Jinja‑render)."""
        templates = self.cfg.get("library_templates") or {}
        if not templates:
            return
        env = Environment(undefined=StrictUndefined, autoescape=False)
        def to_dict(val):
            if isinstance(val, dict):
                return val
            if isinstance(val, list):
                acc = {}
                for frag in val:
                    if isinstance(frag, dict):
                        acc = _deep_merge(acc, frag)
                return acc
            return {}

        out = []
        for lib in self.cfg.get("libraries") or []:
            if not isinstance(lib, dict):
                out.append(lib)
                continue
            tmpl_name = lib.get("template")
            if not tmpl_name or tmpl_name not in templates:
                out.append(lib)
                continue

            base = to_dict(templates[tmpl_name])
            merged = _deep_merge(base, lib)

            nm = str(merged.get("name", "")).strip()
            lib_slug = _slug(nm) if nm else ""
            lib_snake = _snake(nm) if nm else ""
            lib_camel = _camel(nm) if nm else ""

            derived = {
                "slug": lib_slug,
                "snake": lib_snake,
                "camel": lib_camel,
                "cmake_project": lib_snake or nm,
                "project_slug": lib_slug,
                "project_camel": lib_camel,
            }
            if "project_name" not in merged:
                derived["project_name"] = lib_snake or nm

            ctx = {}
            ctx.update(self.cfg)
            ctx.update(derived)
            ctx.update(merged)

            def render_any(obj):
                if isinstance(obj, str):
                    try:
                        return env.from_string(obj).render(**ctx)
                    except Exception:
                        return obj
                if isinstance(obj, list):
                    return [render_any(x) for x in obj]
                if isinstance(obj, dict):
                    return {k: render_any(v) for k, v in obj.items()}
                return obj

            out.append(render_any(merged))
        self.cfg["libraries"] = out

    # Dependency modeling + derivations used by cmake/tests/apps planning
    def _build_library_index(self, cfg: dict) -> Dict[str, dict]:
        libs = cfg.get("libraries") or []
        if not isinstance(libs, list) or not libs:
            return {}
        idx: Dict[str, dict] = {}
        for item in libs:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            slug = _slug(str(name))
            snake = _snake(str(name))

            fp_raw = item.get("find_package") if "find_package" in item else None
            if "find_package" not in item:
                # no key → supply default
                finds = [f"{snake} CONFIG REQUIRED"]
            elif fp_raw is None:
                # explicit null → no find_package
                finds = []
            else:
                finds = [str(x) for x in _coerce_list(fp_raw) if str(x).strip()]

            lk_raw = item.get("link")
            links = [f"{snake}::{snake}"] if lk_raw is None else [
                str(x) for x in _coerce_list(lk_raw) if str(x).strip()
            ]
            idx[slug] = {
                "item": item,
                "name": name,
                "slug": slug,
                "snake": snake,
                "finds": finds,
                "links": links,
                "depends_raw": list(_coerce_list(item.get("depends_on"))),
            }
            if "build_steps" not in item:
                item["build_steps"] = []

        by_snake = {v["snake"]: k for k, v in idx.items()}
        def resolve(nm: str) -> Optional[str]:
            if not nm:
                return None
            s = _slug(str(nm))
            if s in idx:
                return s
            sn = _snake(str(nm))
            return by_snake.get(sn)
        for k, v in idx.items():
            deps = []
            for nm in v.pop("depends_raw", []):
                r = resolve(str(nm))
                if r:
                    deps.append(r)
            v["depends"] = _dedupe(deps)
        return idx

    def _collect_transitive(self, idx: Dict[str, dict], roots: Iterable[str], *, exclude_roots=False) -> List[str]:
        roots = [r for r in roots if r in idx]
        seen: set[str] = set()
        stack = list(roots)
        while stack:
            s = stack.pop()
            if s in seen:
                continue
            seen.add(s)
            for d in idx[s]["depends"]:
                if d in idx:
                    stack.append(d)
        if exclude_roots:
            for r in roots:
                seen.discard(r)
        return list(seen)

    def _toposort_subset(self, idx: Dict[str, dict], subset: Iterable[str]) -> List[str]:
        S = set(subset)
        adj: Dict[str, List[str]] = {s: [] for s in S}
        indeg: Dict[str, int] = {s: 0 for s in S}
        for s in S:
            for d in idx[s]["depends"]:
                if d in S:
                    indeg[s] += 1
                    adj[d].append(s)
        queue = [s for s in S if indeg[s] == 0]
        ordered: List[str] = []
        while queue:
            n = sorted(queue, key=lambda x: idx[x]["name"].lower()).pop(0)
            queue.remove(n)
            ordered.append(n)
            for m in adj[n]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    queue.append(m)
        if len(ordered) != len(S):
            remaining = [s for s in S if s not in ordered]
            ordered.extend(sorted(remaining, key=lambda x: idx[x]["name"].lower()))
        return ordered

    def _gather_apt_packages(self, cfg: dict, idx: Dict[str, dict], proj_slug: str) -> List[str]:
        if proj_slug not in idx:
            return []
        trans = set(self._collect_transitive(idx, [proj_slug], exclude_roots=True))
        pkgs: List[str] = []
        for s in trans:
            item = idx[s]["item"]
            if str(item.get("kind")) != "system":
                continue
            pkg = item.get("pkg")
            if not pkg:
                continue
            if isinstance(pkg, (list, tuple)):
                pkgs.extend([str(x) for x in pkg if str(x).strip()])
            else:
                pkgs.append(str(pkg))
        return _dedupe([p for p in pkgs if p and str(p).lower() not in ("none", "null")])

    def _normalize_tests_targets(self, raw_targets: Any) -> list[dict]:
        norm: list[dict] = []
        if not raw_targets:
            return norm
        items = raw_targets if isinstance(raw_targets, (list, tuple)) else [raw_targets]
        for item in items:
            if isinstance(item, str):
                name = item.strip()
                if name:
                    norm.append({"name": name, "sources": [f"src/{name}.c"]})
                continue
            if isinstance(item, dict) and "name" in item:
                name = str(item["name"]).strip()
                if not name:
                    continue
                sources = item.get("sources") or [f"src/{name}.c"]
                ent = {"name": name, "sources": [str(s) for s in (sources if isinstance(sources, (list, tuple)) else [sources])]}
                if "depends_on" in item and item["depends_on"] is not None:
                    dep = item["depends_on"]
                    ent["depends_on"] = [str(x) for x in (dep if isinstance(dep, (list, tuple)) else [dep])]
                norm.append(ent)
                continue
            if isinstance(item, dict) and item:
                k = next(iter(item.keys()))
                v = item[k]
                name = str(k).strip()
                if isinstance(v, str) or v is None:
                    sources = [v] if isinstance(v, str) and v.strip() else [f"src/{name}.c"]
                    norm.append({"name": name, "sources": [str(s) for s in sources]})
                elif isinstance(v, dict):
                    sources = v.get("sources") or [f"src/{name}.c"]
                    ent = {"name": name, "sources": [str(s) for s in (sources if isinstance(sources, (list, tuple)) else [sources])]}
                    if "depends_on" in v and v["depends_on"] is not None:
                        dep = v["depends_on"]
                        ent["depends_on"] = [str(x) for x in (dep if isinstance(dep, (list, tuple)) else [dep])]
                    norm.append(ent)
                continue
        return norm

    def _resolve_dep_names_to_lib_slugs(self, dep_names: list[str], idx: dict) -> list[str]:
        by_snake = {v["snake"]: k for k, v in idx.items()}
        out: list[str] = []
        for nm in dep_names or []:
            if not nm:
                continue
            s = _slug(str(nm))
            if s in idx:
                out.append(s)
                continue
            sn = _snake(str(nm))
            if sn in by_snake:
                out.append(by_snake[sn])
        return _dedupe(out)

    def _derive_suite_deps_from_libs(self, lib_slugs: list[str], idx: dict) -> tuple[list[str], list[str]]:
        finds = _dedupe([fp for s in lib_slugs for fp in idx[s]["finds"]])
        links = _dedupe([lk for s in lib_slugs for lk in idx[s]["links"]])
        return finds, links

    def _augment_tests_cfg(self, cfg: dict, idx: dict, proj_slug: str) -> dict:
        ts = dict(cfg.get("tests") or {})
        raw_targets = ts.get("targets") or ts.get("test_targets")
        norm_targets = self._normalize_tests_targets(raw_targets)
        if norm_targets:
            ts["targets"] = norm_targets
            # Back-compat alias expected by older templates
            ts.setdefault("test_targets", list(norm_targets))

        union_dep_names: list[str] = []
        if ts.get("depends_on"):
            union_dep_names.extend([str(x) for x in _coerce_list(ts["depends_on"])])
        for t in (norm_targets or []):
            for nm in t.get("depends_on") or []:
                union_dep_names.append(str(nm))

        lib_slugs = self._resolve_dep_names_to_lib_slugs(union_dep_names, idx)

        in_tree_links, external_finds, external_links = [], [], []
        for slug in lib_slugs:
            lib_info = idx.get(slug)
            if not lib_info:
                continue
            if slug == proj_slug:
                in_tree_links.extend(lib_info.get("links", []))
            else:
                external_finds.extend(lib_info.get("finds", []))
                external_links.extend(lib_info.get("links", []))

        if not ts.get("find_packages"):
            ts["find_packages"] = _dedupe(external_finds)
        if not ts.get("link_libraries"):
            ts["link_libraries"] = _dedupe(in_tree_links + external_links)

        cfg["tests"] = ts
        return cfg

    def _compute_app_dest_dir(self, base_dest: str, ctx_dest: Optional[str], ctx_name: str) -> str:
        base = (base_dest or "apps").strip("/")
        if ctx_dest:
            d = str(ctx_dest).strip()
            if d.startswith("/"):
                return d.strip("/")
            return f"{base}/{d.strip('/')}"
        return f"{base}/{ctx_name}"

    def _normalize_binaries_for_apps(self, app_ctx: dict, ctx_name: str, base_dest: str) -> list[dict]:
        raw = app_ctx.get("binaries") or []
        norm: list[dict] = []
        for item in (raw if isinstance(raw, (list, tuple)) else [raw]):
            if isinstance(item, str):
                nm = item.strip()
                if nm:
                    norm.append({"name": nm, "sources": [f"src/{nm}.c"]})
            elif isinstance(item, dict) and item:
                k = next(iter(item.keys()))
                conf = item.get(k) or {}
                srcs = conf.get("sources") or [f"src/{k}.c"]
                ent = {"name": k, "sources": [str(s) for s in (srcs if isinstance(srcs, (list, tuple)) else [srcs])]}
                if "link_libraries" in conf and conf["link_libraries"] is not None:
                    ent["link_libraries"] = [str(x) for x in _coerce_list(conf["link_libraries"])]
                if "c_standard" in conf:
                    ent["c_standard"] = conf["c_standard"]
                if "cxx_standard" in conf:
                    ent["cxx_standard"] = conf["cxx_standard"]
                if "depends_on" in conf and conf["depends_on"] is not None:
                    ent["depends_on"] = [str(x) for x in _coerce_list(conf["depends_on"])]
                norm.append(ent)
        return norm

    def _normalize_apps_cfg(self, cfg: dict, idx: dict, proj_slug: str) -> dict:
        apps = dict(cfg.get("apps") or {})
        if not apps:
            return cfg

        base_ctx = dict(apps.get("context") or {})
        base_dest = str(base_ctx.get("dest") or "apps")
        base_ctx_wo_dest = dict(base_ctx)
        base_ctx_wo_dest.pop("dest", None)

        out: dict = {}
        for name, v in apps.items():
            if name == "context":
                continue
            ctx_name = str(name)
            raw_ctx = dict(v or {})
            ctx = _deep_merge(base_ctx_wo_dest, raw_ctx)
            dest_dir = self._compute_app_dest_dir(base_dest, ctx.get("dest"), ctx_name)
            ctx["_apps_dest_dir"] = dest_dir
            ctx["binaries"] = self._normalize_binaries_for_apps(ctx, ctx_name, base_dest)

            union_dep: list[str] = []
            if base_ctx_wo_dest.get("depends_on"):
                union_dep.extend([str(x) for x in _coerce_list(base_ctx_wo_dest["depends_on"])])
            if ctx.get("depends_on"):
                union_dep.extend([str(x) for x in _coerce_list(ctx["depends_on"])])
            for b in ctx["binaries"]:
                for nm in (b.get("depends_on") or []):
                    union_dep.append(str(nm))

            if not union_dep and proj_slug:
                # fallback to project lib
                union_dep.append(proj_slug)

            lib_slugs = self._resolve_dep_names_to_lib_slugs(_dedupe(union_dep), idx)
            if lib_slugs:
                if not ctx.get("find_packages"):
                    finds, links = self._derive_suite_deps_from_libs(lib_slugs, idx)
                    ctx["find_packages"] = finds
                    if not ctx.get("link_libraries"):
                        ctx["link_libraries"] = links
            out[ctx_name] = ctx

        apps.update(out)
        cfg["apps"] = apps
        return cfg

    def _augment_with_libraries_tests_apps(self) -> None:
        """Derive cmake.find_packages/link_libraries etc., tests/apps suite deps, dev tools."""
        cfg = self.cfg
        idx = self._build_library_index(cfg)
        if not idx:
            self.cfg = cfg
            return

        # Determine project slug in libraries
        proj_slug = _slug(cfg.get("project_name", "") or cfg.get("project_slug", ""))
        if proj_slug not in idx:
            ps = _snake(cfg.get("project_name", ""))
            for s, v in idx.items():
                if v["snake"] == ps:
                    proj_slug = s
                    break

        if proj_slug in idx:
            direct = idx[proj_slug]["depends"]
            cm = dict(cfg.get("cmake") or {})
            if direct:
                if not cm.get("find_packages"):
                    pkgs: list[str] = []
                    for d in direct:
                        for fp in (idx[d].get("finds") or []):
                            if fp:  # skip null/empty
                                pkgs.append(fp)
                    cm["find_packages"] = _dedupe(pkgs)
                if not cm.get("link_libraries"):
                    cm["link_libraries"] = _dedupe([lk for d in direct for lk in idx[d]["links"]])
                if not cm.get("deps_for_config"):
                    def _base_pkg(x: str) -> str:
                        return (x.strip().split()[0] if isinstance(x, str) and x.strip() else "")

                    bases: list[str] = []
                    for d in direct:
                        # only include deps that actually declare find_package entries
                        for fp in (idx[d].get("finds") or []):
                            b = _base_pkg(fp)
                            if b:
                                bases.append(b)

                    cm["deps_for_config"] = _dedupe(bases)
                trans = set(self._collect_transitive(idx, [proj_slug], exclude_roots=True))
                order = self._toposort_subset(idx, trans)
                cm["libraries"] = [idx[s]["item"] for s in order]
                cm["apt_packages"] = self._gather_apt_packages(cfg, idx, proj_slug)
                self.cfg["cmake"] = cm

        # tests/apps derivations
        self.cfg = self._augment_tests_cfg(self.cfg, idx, proj_slug)
        self.cfg = self._normalize_apps_cfg(self.cfg, idx, proj_slug)

        # dev packages → cmake.apt_dev_packages
        dev_pkgs = _coerce_list(self.cfg.get("dev_packages"))
        if dev_pkgs:
            cm = dict(self.cfg.get("cmake") or {})
            cm["apt_dev_packages"] = _dedupe([str(p) for p in dev_pkgs if str(p).strip()])
            self.cfg["cmake"] = cm

        # Ensure useful keys always exist
        cm = dict(self.cfg.get("cmake") or {})
        for k in [
            "libraries",
            "deps_for_config",
            "apt_packages",
            "apt_dev_packages",
            "find_packages",
            "link_libraries",
            "depends_on",
        ]:
            cm.setdefault(k, [])
        self.cfg["cmake"] = cm

    def _compute_package_switches(self) -> None:
        def package_patterns(cfg: dict) -> dict[str, list[str]]:
            merged = cfg.get("template_packages") or {}
            out = {}
            for name, pats in merged.items():
                if isinstance(pats, str):
                    out[name] = [pats]
                elif isinstance(pats, list):
                    out[name] = [str(x) for x in pats]
            return out

        self.enabled_packages = set(self.cfg.get("packages") or [])
        self.package_patterns = package_patterns(self.cfg)

    # ── Jinja planning helpers ────────────────────────────────────────────────

    def _matches_disabled(self, rel: str) -> bool:
        matched = {pkg for pkg, pats in self.package_patterns.items() for pat in pats if fnmatch.fnmatch(rel, pat)}
        if not matched:
            return False
        return not any(pkg in self.enabled_packages for pkg in matched)

    def _discover_jinja_items(self) -> List[dict]:
        """List *.j2 templates with their metadata (front‑matter) and source exec bit."""
        jinja_items: List[dict] = []
        for rel, data, is_j2, origin in self.tmpl_src.iter_files():
            if not is_j2:
                continue
            if self._matches_disabled(rel):
                continue

            # detect source file exec bit (only for fs origin)
            is_exec = False
            if origin == "fs" and self.tmpl_src.fs_dir:
                src_path = self.tmpl_src.fs_dir / rel
                if src_path.exists():
                    is_exec = bool(src_path.stat().st_mode & stat.S_IXUSR)

            text = data.decode("utf-8", errors="replace")
            meta, body = _extract_annotation(text)
            default_dest = rel[:-3]
            jinja_items.append(
                {
                    "rel": rel,
                    "inline_template": (body if meta else text),
                    "dest": (meta.get("dest") if meta else None) or default_dest,
                    "context": (meta.get("context") if meta else "."),
                    "updatable": True if not meta else bool(meta.get("updatable", True)),
                    "header_managed": None if not meta else meta.get("header_managed", None),
                    "origin": origin,
                    "executable": is_exec,
                }
            )
        return jinja_items

    def _discover_copy_items(self) -> List[dict]:
        """List non‑Jinja files to be copied verbatim (and their exec bit)."""
        copy_items: List[dict] = []
        for rel, data, is_j2, origin in self.tmpl_src.iter_files():
            if is_j2:
                continue
            if self._matches_disabled(rel):
                continue

            is_exec = False
            if origin == "fs" and self.tmpl_src.fs_dir:
                src_path = self.tmpl_src.fs_dir / rel
                if src_path.exists():
                    is_exec = bool(src_path.stat().st_mode & stat.S_IXUSR)

            copy_items.append({"rel": rel, "bytes": data, "origin": origin, "executable": is_exec})
        return copy_items

    def _jinja_env_for_inline(self) -> Environment:
        loaders = []
        if self.tmpl_src and self.tmpl_src.fs_dir:
            loaders.append(FileSystemLoader(str(self.tmpl_src.fs_dir)))
        env = Environment(
            loader=ChoiceLoader(loaders) if loaders else None,
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        # tiny filter used in some templates
        env.filters.setdefault("ternary", lambda v, a, b: a if bool(v) else b)
        return env

    def _render_with_help(self, env: Environment, it: dict, ctx: dict) -> str:
        try:
            return env.from_string(it["inline_template"]).render(**ctx)
        except Exception as e:
            # Try to show a small code frame if Jinja surfaced a line number
            lineno = getattr(e, "lineno", None) or getattr(getattr(e, "node", None), "lineno", None)
            frame = ""
            if lineno:
                lines = it["inline_template"].splitlines()
                start = max(0, lineno - 3)
                end = min(len(lines), lineno + 2)
                seg = []
                for i in range(start, end):
                    marker = "  <-- here" if (i + 1) == lineno else ""
                    seg.append(f"{i+1:5d}| {lines[i]}{marker}")
                frame = "\n--- snippet around line {} ---\n{}\n".format(lineno, "\n".join(seg))
            raise RuntimeError(
                f"Jinja render error in template '{it['rel']}' → output '{it['dest']}' (context='{it['context']}'):\n{e}\n{frame}"
            ) from e

    def _base_from_cfg(self, cfg: dict) -> dict:
        return {k: v for k, v in cfg.items() if k not in ("cmake", "tests", "files", "template_packages", "packages", "templates_dir")}

    def _build_ctx_inherited(self, key: str | None) -> dict:
        base = self._base_from_cfg(self.cfg)
        sub = {} if not key or key == "." else (self.cfg.get(key) or {})
        ctx = _deep_merge(base, sub)

        ctx.setdefault("project_name", self.cfg.get("project_name") or "project")
        ctx.setdefault("project_slug", _slug(ctx.get("project_name", "project")))
        ctx.setdefault("cmake_project", _snake(ctx["project_slug"]))
        ctx.setdefault("project_camel", _camel(ctx.get("project_name", "project")))
        # NEW: ensure project_title exists for templates that reference it
        ctx.setdefault("project_title", ctx.get("project_title", ctx.get("project_name")))

        date = str(self.cfg.get("date") or ctx.get("date") or "")
        ctx.setdefault("year", date[:4] if len(date) >= 4 else "2025")

        # Always provide these nested maps
        ctx.setdefault("cmake", self.cfg.get("cmake") or {})
        ctx.setdefault("tests", self.cfg.get("tests") or {})

        # Back-compat alias expected by older templates
        tests_cfg = self.cfg.get("tests") or {}
        tt = tests_cfg.get("test_targets") or tests_cfg.get("targets") or []
        ctx.setdefault("test_targets", tt)
        return ctx

    def _plan_apps_resources(self, *, show_diffs: bool) -> List[PlanItem]:
        """
        Render templates/resources/apps/**/* into each apps context destination.
        """
        APPS_RES_ROOT = "resources/apps/"
        entries: list[Tuple[str, bytes, bool, str]] = []
        for rel, data, is_j2, origin in self.tmpl_src.iter_files():
            if rel == APPS_RES_ROOT.rstrip("/"):
                continue
            if rel.startswith(APPS_RES_ROOT):
                entries.append((rel, data, is_j2, origin))
        if not entries:
            return []

        apps = self.cfg.get("apps") or {}
        contexts = [k for k in apps.keys() if k != "context"]
        if not contexts:
            return []

        env = self._jinja_env_for_inline()

        plan: List[PlanItem] = []
        # base ctx for cmake-like values
        base = self._build_ctx_inherited("cmake")

        for ctx_name in contexts:
            ctx = dict(apps.get(ctx_name) or {})
            dest_dir = ctx.get("_apps_dest_dir")
            if not dest_dir:
                base_dest = str((apps.get("context") or {}).get("dest") or "apps")
                dest_dir = self._compute_app_dest_dir(base_dest, ctx.get("dest"), ctx_name)

            rctx = _deep_merge(base, ctx)
            rctx.setdefault("project_name", self.cfg.get("project_name") or "project")
            rctx.setdefault("project_slug", _slug(rctx["project_name"]))
            rctx.setdefault("cmake_project", _snake(rctx["project_slug"]))
            rctx.setdefault("language", self.cfg.get("language", "C"))
            rctx.setdefault("app_project_name", f"{base.get('project_name','project')}_{ctx_name}")
            rctx.setdefault("app_languages", base.get("language", "C"))

            for rel, data, is_j2, origin in entries:
                sub_rel = rel[len(APPS_RES_ROOT):]
                out_rel = sub_rel[:-3] if (is_j2 and sub_rel.endswith(".j2")) else sub_rel
                dest_rel = f"{dest_dir}/{out_rel}"
                target = self.repo / dest_rel

                if is_j2:
                    raw_tpl = data.decode("utf-8", errors="replace")
                    try:
                        new_text = env.from_string(raw_tpl).render(**rctx)
                    except Exception as e:
                        raise RuntimeError(
                            f"Jinja render error in apps resource '{rel}' → '{dest_rel}': {e}"
                        ) from e
                    new_bytes = new_text.encode("utf-8")
                    tmpl_sha = _sha256(raw_tpl.encode("utf-8"))
                else:
                    new_bytes = data
                    tmpl_sha = _sha256(data)

                old_text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
                new_text_for_cmp = new_bytes.decode("utf-8", errors="replace")
                header_managed = _header_managed_default(dest_rel)
                cmp_new = _normalize_for_cmp(new_text_for_cmp, target, header_managed)
                cmp_old = _normalize_for_cmp(old_text, target, header_managed)
                status = "create" if not target.exists() else ("update" if cmp_old != cmp_new else "unchanged")

                diff_text = ""
                if show_diffs and status in ("create", "update"):
                    diff_text = _diff(old_text.encode("utf-8"), cmp_new.encode("utf-8"), dest_rel)

                # exec bit propagation from source
                is_exec = False
                if origin == "fs" and self.tmpl_src.fs_dir:
                    src_path = self.tmpl_src.fs_dir / rel
                    if src_path.exists():
                        is_exec = os.access(src_path, os.X_OK)

                plan.append(
                    PlanItem(
                        kind=("jinja" if is_j2 else "copy"),
                        path=dest_rel,
                        status=status,
                        updatable=True,
                        diff=diff_text,
                        new_bytes=new_bytes,
                        template_sha256=tmpl_sha,
                        context_key=f"apps.{ctx_name}",
                        header_managed=header_managed,
                        executable=is_exec,
                    )
                )
        return plan
