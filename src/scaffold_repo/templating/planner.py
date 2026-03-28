# src/scaffold_repo/templating/planner.py
from __future__ import annotations

import fnmatch
import re
import sys
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined

from ..utils.text import sha256, slug, snake, camel
from ..utils.collections import deep_merge

_OSS_HEADER_EXTS = {
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".java", ".js", ".ts",
    ".tsx", ".mjs", ".cjs", ".go", ".rs", ".swift", ".kt", ".cs",
    ".cmake", ".mk", ".make", ".py", ".sh", ".bash", ".zsh"
}

_LINE = "line"
_BLOCK = "block"

def _header_managed_default(dest: str) -> bool:
    p = Path(dest)
    return p.name == "CMakeLists.txt" or (p.suffix.lower() in _OSS_HEADER_EXTS)

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

def _ensure_trailing_newline(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    if not s.endswith("\n"): return s + "\n"
    return s

def _diff(old: bytes, new: bytes, path: str) -> str:
    import difflib
    old_lines = old.decode("utf-8", errors="replace").splitlines(keepends=True)
    new_lines = new.decode("utf-8", errors="replace").splitlines(keepends=True)
    return "".join(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}"))

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

class TemplatePlanner:
    def __init__(self, repo: Path, tmpl_src, config: dict, is_init: bool = False):
        self.repo = repo
        self.tmpl_src = tmpl_src
        self.cfg = config
        self.is_init = is_init
        self.package_patterns = config.get("package_patterns", {})
        self.enabled_packages = config.get("enabled_packages", set())

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
            plan.append(PlanItem("jinja", rendered_dest, status, it.get("updatable", True), diff_text, new_text.encode("utf-8"), sha256(it["inline_template"].encode("utf-8")), it["context"], header_managed, is_exec))

        plan.extend(self._plan_subproject_resources(show_diffs=show_diffs))
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
            plan.append(PlanItem("copy", it["dest"], status, True, diff_text, new_bytes, sha256(new_bytes), None, False, it.get("executable", False)))
        return plan

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

    def _build_ctx_inherited(self, key: str | None) -> dict:
        ctx = deep_merge(self._base_from_cfg(self.cfg), {} if not key or key == "." else (self.cfg.get(key) or {}))
        ctx.setdefault("project_name", self.cfg.get("project_name") or "project")
        ctx.setdefault("project_slug", slug(ctx.get("project_name", "project")))
        ctx.setdefault("project_snake", snake(ctx["project_slug"]))
        ctx.setdefault("project_camel", camel(ctx.get("project_name", "project")))
        ctx.setdefault("project_title", ctx.get("project_title", ctx.get("project_name")))
        ctx.setdefault("version", str(self.cfg.get("version") or "0.1.0"))

        stack = self.cfg.get("stack", "generic")
        stack_type = self.cfg.get("stack_type", "")
        ctx.setdefault("stack", stack)
        ctx.setdefault("stack_type", stack_type)

        scoped_key = f"{stack}_{stack_type}".strip("_").lower()
        if scoped_key in self.cfg:
            ctx = deep_merge(ctx, self.cfg[scoped_key])

        ctx.setdefault("deps", self.cfg.get("deps") or {})
        ctx.setdefault("tests", self.cfg.get("tests") or {})
        ctx.setdefault("test_targets", (self.cfg.get("tests") or {}).get("test_targets") or (self.cfg.get("tests") or {}).get("targets") or [])

        ctx.setdefault("kind", "compiled")
        ctx.setdefault("is_cli_app", "Library")
        return ctx

    def _base_from_cfg(self, cfg: dict) -> dict:
        return {k: v for k, v in cfg.items() if k not in ("deps", "tests", "files", "template_packages", "packages", "templates_dir")}

    def _strip_package_prefix(self, rel: str) -> str:
        for pkg_name, pats in self.package_patterns.items():
            if pkg_name == "resources": continue
            for pat in pats:
                if pat.endswith("/**"):
                    prefix = pat[:-2]
                    if rel.startswith(prefix): return rel[len(prefix):]
        return rel

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

    def _is_resource_file(self, rel: str) -> bool:
        """Helper to ensure generic subproject templates aren't swept up by the main loop."""
        resource_dirs = {rule.get("resource") for rule in self.cfg.get("subproject_rules", {}).values() if rule.get("resource")}
        for rd in resource_dirs:
            if f"/{rd}/" in rel or rel.startswith(f"{rd}/"): return True
        return False

    def _discover_jinja_items(self) -> list[dict]:
        items = []
        for rel, data, is_j2, origin in self.tmpl_src.iter_files():
            if not is_j2 or self._matches_disabled(rel) or posixpath.basename(rel) in {".scaffold-defaults.yaml", "aliases.yaml"}: continue
            if self._is_resource_file(rel): continue
            if not self._is_valid_stack_rel(rel): continue

            text = data.decode("utf-8", errors="replace")
            meta, inline_template = _extract_annotation(text)

            if (meta or {}).get("on_init") and not self.is_init:
                continue

            dest = (meta or {}).get("dest") or self._strip_package_prefix(rel)[:-3]

            if dest.startswith("tests/") and not self.is_init and not (self.cfg.get("tests") or {}).get("targets"): continue

            executable = bool((meta or {}).get("executable", False))
            if not executable and hasattr(origin, "exists") and origin.exists():
                import os
                executable = os.access(origin, os.X_OK)

            items.append({"rel": rel, "inline_template": inline_template, "dest": dest, "context": (meta or {}).get("context", "."), "updatable": bool((meta or {}).get("updatable", True)), "header_managed": (meta or {}).get("header_managed"), "origin": origin, "executable": executable})
        return items

    def _discover_copy_items(self) -> list[dict]:
        items = []
        for rel, data, is_j2, origin in self.tmpl_src.iter_files():
            if is_j2 or self._matches_disabled(rel) or posixpath.basename(rel) in {".scaffold-defaults.yaml", "aliases.yaml"}: continue
            if self._is_resource_file(rel): continue
            if not self._is_valid_stack_rel(rel): continue

            dest = self._strip_package_prefix(rel)
            if dest.startswith("tests/") and not (self.cfg.get("tests") or {}).get("targets"): continue

            executable = False
            if hasattr(origin, "exists") and origin.exists():
                import os
                executable = os.access(origin, os.X_OK)

            items.append({"rel": rel, "dest": dest, "bytes": data, "origin": origin, "executable": executable})
        return items

    def _plan_subproject_resources(self, *, show_diffs: bool) -> list[PlanItem]:
        rules = self.cfg.get("subproject_rules", {})
        if not rules: return []

        env, plan, base = self._jinja_env_for_inline(), [], self._build_ctx_inherited("deps")

        for block_name, rule in rules.items():
            block_data = self.cfg.get(block_name)
            if not isinstance(block_data, dict): continue

            resource_dir = rule.get("resource")
            if not resource_dir: continue

            # Gather all templates mapped to this resource directory
            all_resources = [(rel, data, is_j2, origin) for rel, data, is_j2, origin in self.tmpl_src.iter_files() if f"/{resource_dir}/" in rel or rel.startswith(f"{resource_dir}/")]
            if not all_resources: continue

            for ctx_name, ctx in block_data.items():
                if ctx_name in ("context", "depends_on"): continue
                if not isinstance(ctx, dict): continue

                dest_dir = ctx.get("_dest_dir", f"{block_name}/{ctx_name}")
                rctx = deep_merge(base, ctx)

                raw_stack = str(ctx.get("stack") or self.cfg.get("stack", "")).strip()
                raw_type = str(ctx.get("stack_type") or self.cfg.get("stack_type", "")).strip()

                if "/" in raw_stack:
                    app_stack, derived_type = raw_stack.split("/", 1)
                    app_stack = app_stack.lower()
                    app_stack_type = raw_type.lower() or derived_type.lower()
                else:
                    app_stack = raw_stack.lower()
                    app_stack_type = raw_type.lower() or "base"

                app_defaults = self.tmpl_src.get_stacked_defaults(f"stacks/{app_stack}/{app_stack_type}/_")
                rctx = deep_merge(app_defaults, rctx)

                active_prefix = f"stacks/{app_stack}/{app_stack_type}/{resource_dir}/" if app_stack else None
                base_prefix = f"stacks/{app_stack}/base/{resource_dir}/" if app_stack else None
                global_prefix = f"{resource_dir}/global/"

                rctx.setdefault("project_name", self.cfg.get("project_name") or "project")
                rctx.setdefault("project_slug", slug(rctx["project_name"]))
                rctx.setdefault("project_snake", snake(rctx["project_slug"]))

                app_scoped_key = f"{app_stack}_{app_stack_type}".strip("_").lower()
                if app_scoped_key in self.cfg:
                    rctx = deep_merge(rctx, self.cfg[app_scoped_key])

                for rel, data, is_j2, origin in all_resources:
                    root_prefix = active_prefix if active_prefix and rel.startswith(active_prefix) else (base_prefix if base_prefix and rel.startswith(base_prefix) else (global_prefix if rel.startswith(global_prefix) else None))
                    if not root_prefix: continue

                    sub_rel = rel[len(root_prefix):]
                    dest_rel = f"{dest_dir}/{sub_rel[:-3] if (is_j2 and sub_rel.endswith('.j2')) else sub_rel}"
                    target = self.repo / dest_rel

                    if is_j2:
                        raw_tpl = data.decode("utf-8", errors="replace")
                        try: new_bytes = env.from_string(raw_tpl).render(**rctx).encode("utf-8")
                        except Exception as e: raise RuntimeError(f"Jinja render error in subproject resource '{rel}' → '{dest_rel}': {e}") from e
                        tmpl_sha = sha256(raw_tpl.encode("utf-8"))
                    else: new_bytes, tmpl_sha = data, sha256(data)

                    old_text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
                    hm = _header_managed_default(dest_rel)
                    cmp_new, cmp_old = _normalize_for_cmp(new_bytes.decode("utf-8", errors="replace"), target, hm), _normalize_for_cmp(old_text, target, hm)
                    status = "create" if not target.exists() else ("update" if cmp_old != cmp_new else "unchanged")
                    diff_text = _diff(old_text.encode("utf-8"), cmp_new.encode("utf-8"), dest_rel) if show_diffs and status in ("create", "update") else ""

                    is_exec = False
                    if hasattr(origin, "exists") and origin.exists():
                        import os
                        is_exec = os.access(origin, os.X_OK)

                    plan.append(PlanItem("jinja" if is_j2 else "copy", dest_rel, status, True, diff_text, new_bytes, tmpl_sha, f"{block_name}.{ctx_name}", hm, is_exec))
        return plan
