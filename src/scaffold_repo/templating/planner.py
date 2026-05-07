# src/scaffold_repo/templating/planner.py
from __future__ import annotations

import re
import sys
import posixpath
import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined

from ..utils.text import sha256, slug, snake, camel
from ..utils.collections import deep_merge, coerce_list

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
        self.enabled_features = config.get("enabled_features", set())

    def _is_updatable(self, dest_rel_path: str) -> bool:
        """
        Checks the merged configuration for template_rules.
        Returns False if the file matches a rule where updatable is false.
        """
        rules = self.cfg.get("template_rules", [])
        if not isinstance(rules, list):
            return True

        for rule in rules:
            if not isinstance(rule, dict):
                continue

            # Allow 'match' to be a single string or a list of strings
            matches = rule.get("match", [])
            if isinstance(matches, str):
                matches = [matches]

            for pattern in matches:
                # Use fnmatch so "*.gitignore" or "README.md" both work
                if fnmatch.fnmatch(dest_rel_path, pattern) or dest_rel_path == pattern:
                    if rule.get("updatable") is False:
                        return False

        return True


    # ── METADATA & RULE EVALUATION ──
    def _collect_rules(self, rel: str, stack: str | None, stack_type: str | None) -> list[dict]:
        """
        Gathers rules based on a strict sandboxed inheritance model:
        - global_rules: Apply to everything.
        - feature_rules: Apply ONLY to templates originating from a feature folder.
        - template_rules: Apply ONLY to templates originating from the base stack.
        """
        global_rules = []
        template_rules = []
        feature_rules = []

        # 1. Determine the origin of this specific file
        stripped = self._strip_stack_prefix(rel, stack, stack_type)
        m = re.match(r"^scaffold-features/([^/]+)", stripped)
        is_feature_file = bool(m)
        current_feat_name = m.group(1) if is_feature_file else None

        def _extract_rules(data: dict):
            if not isinstance(data, dict): return

            # Global rules always apply
            global_rules.extend(coerce_list(data.get("global_rules", [])))

            if is_feature_file:
                # If it's a feature file, ONLY extract its specific feature rules
                f_rules = data.get("feature_rules", {})
                if isinstance(f_rules, dict) and current_feat_name in f_rules:
                    feature_rules.extend(coerce_list(f_rules[current_feat_name]))
            else:
                # If it's a base file, ONLY extract template rules
                template_rules.extend(coerce_list(data.get("template_rules", [])))

        # 2. Physical Directory Chain (Root -> Stack -> Local)
        dir_path = posixpath.dirname(rel)
        parts = dir_path.split("/") if dir_path else []
        current = ""
        paths_to_check = [".scaffold.yaml"]
        for p in parts:
            if not p or p == ".": continue
            current = f"{current}/{p}" if current else p
            paths_to_check.append(f"{current}/.scaffold.yaml")

        for pth in paths_to_check:
            text = self.tmpl_src.read_resource_text(pth)
            if text:
                try:
                    data = yaml.safe_load(text) or {}
                    _extract_rules(data)
                except Exception:
                    pass

        # 3. Logical Feature Defaults (Cross-Branch)
        if is_feature_file and current_feat_name in self.enabled_features:
            feat_defaults_path = f"scaffold-features/{current_feat_name}/.scaffold.yaml"
            text = self.tmpl_src.read_resource_text(feat_defaults_path)
            if text:
                try:
                    data = yaml.safe_load(text) or {}
                    _extract_rules(data)
                except Exception:
                    pass

        # 4. User Overrides (from the generated project's scaffold.yaml)
        _extract_rules(self.cfg)

        return global_rules + template_rules + feature_rules

    def _evaluate_template_rules(self, logical_source: str, rules: list[dict]) -> dict:
        """Evaluates collected rules against the logical source path.
           Supports single strings or arrays of strings for 'match'.
        """
        merged_meta = {}
        for rule in rules:
            match_patterns = rule.get("match")
            if not match_patterns: continue

            # Normalize to a list to support array matching
            if isinstance(match_patterns, str):
                match_patterns = [match_patterns]

            # If any pattern in the list matches, apply the rule metadata
            if any(fnmatch.fnmatch(logical_source, pat) for pat in match_patterns):
                for k, v in rule.items():
                    if k != "match":
                        merged_meta[k] = v
        return merged_meta

    # ── DISCOVERY AND PLANNING ──

    def plan_jinja(self, *, show_diffs: bool = False) -> list[PlanItem]:
        env = self._jinja_env_for_inline()
        items = self._discover_jinja_items()
        plan: list[PlanItem] = []
        for it in items:
            ctx = self._build_ctx_inherited(it["context"])
            new_text = self._render_with_help(env, it, ctx)

            try:
                rendered_dest = env.from_string(it["dest"]).render(**ctx).strip()
            except Exception:
                rendered_dest = it["dest"].strip()

            if not rendered_dest or rendered_dest in (".", "/"):
                continue

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
            plan.append(PlanItem("copy", it["dest"], status, it.get("updatable", True), diff_text, new_bytes, sha256(new_bytes), None, False, it.get("executable", False)))
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

        if "prompt_answers" in self.cfg:
            ctx = deep_merge(ctx, self.cfg["prompt_answers"])

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
        return {k: v for k, v in cfg.items() if k not in ("deps", "tests", "files", "features", "packages", "templates_dir")}

    def _get_path_weight(self, rel: str, stack: str = None, stack_type: str = None) -> int:
        stack = stack or self.cfg.get("stack")
        stype = stack_type or self.cfg.get("stack_type")

        weight = 1

        if stack and rel.startswith(f"stacks/{stack}/"):
            weight = 3
            if stype and rel.startswith(f"stacks/{stack}/{stype}/"):
                weight = 5

        if "/scaffold-features/" in rel or rel.startswith("scaffold-features/"):
            weight += 1

        return weight

    def _strip_stack_prefix(self, rel: str, stack: str = None, stack_type: str = None) -> str:
        s = rel
        stack = stack or self.cfg.get("stack")
        stype = stack_type or self.cfg.get("stack_type")

        if stack and stype and s.startswith(f"stacks/{stack}/{stype}/base/"):
            return s[len(f"stacks/{stack}/{stype}/base/"):]

        if stack and stype and s.startswith(f"stacks/{stack}/{stype}/"):
            return s[len(f"stacks/{stack}/{stype}/"):]

        if stack and s.startswith(f"stacks/{stack}/base/"):
            return s[len(f"stacks/{stack}/base/"):]

        if stack and s.startswith(f"stacks/{stack}/"):
            return s[len(f"stacks/{stack}/"):]

        if s.startswith("base/"):
            return s[len("base/"):]

        return s

    def _is_active_path(self, rel: str, stack: str = None, stack_type: str = None) -> bool:
        if not rel.startswith(("base/", "stacks/", "scaffold-features/")):
            return False

        stack = stack or self.cfg.get("stack")
        stack_type = stack_type or self.cfg.get("stack_type")

        if rel.startswith("stacks/"):
            if not stack: return False
            valid_prefixes = [
                f"stacks/{stack}/base/",
            ]
            if stack_type:
                valid_prefixes.extend([
                    f"stacks/{stack}/{stack_type}/base/",
                    f"stacks/{stack}/{stack_type}/scaffold-features/",
                    f"stacks/{stack}/{stack_type}/",
                ])

            if not any(rel.startswith(pfx) for pfx in valid_prefixes):
                return False

        s = self._strip_stack_prefix(rel, stack, stack_type)

        if s.startswith("scaffold-features/"):
            if "scaffold-features/" in s[len("scaffold-features/"): ]:
                return False

            match = re.match(r"^scaffold-features/([^/]+)", s)
            if match and match.group(1) not in self.enabled_features:
                return False

        return True

    def _strip_routing_prefixes(self, rel: str, stack: str = None, stack_type: str = None) -> str:
        s = self._strip_stack_prefix(rel, stack, stack_type)
        m = re.match(r"^scaffold-features/[^/]+/(.*)$", s)
        if m:
            s = m.group(1)
        return s

    def _is_resource_file(self, rel: str) -> bool:
        resource_dirs = {
            rule.get("resource") for rule in self.cfg.get("subproject_rules", {}).values()
            if isinstance(rule, dict) and rule.get("resource")
        }
        for rd in resource_dirs:
            if f"/{rd}/" in rel or rel.startswith(f"{rd}/"):
                return True
        return False

    def _discover_jinja_items(self) -> list[dict]:
        items_dict = {}
        stack = self.cfg.get("stack")
        stack_type = self.cfg.get("stack_type")

        for rel, data, is_j2, origin in self.tmpl_src.iter_files():
            if not is_j2 or posixpath.basename(rel) in {".scaffold.yaml", "aliases.yaml", "scaffold.yaml.j2"}: continue
            if not self._is_active_path(rel): continue
            if self._is_resource_file(rel): continue

            text = data.decode("utf-8", errors="replace")
            inline_meta, inline_template = _extract_annotation(text)

            # Determine logical source for pattern matching
            stripped = self._strip_routing_prefixes(rel, stack, stack_type)
            logical_source = stripped[:-3] if (is_j2 and stripped.endswith('.j2')) else stripped

            # ── EVALUATE LOGICAL INHERITANCE RULES ──
            rules = self._collect_rules(rel, stack, stack_type)
            rule_meta = self._evaluate_template_rules(logical_source, rules)

            # Merge: Rule defaults < Inline overrides
            final_meta = deep_merge(rule_meta, inline_meta or {})

            if final_meta.get("on_init") and not self.is_init:
                continue

            # Evaluate final destination
            dest = final_meta.get("dest", logical_source)
            if not dest or (dest.startswith("tests/") and not self.is_init and not (self.cfg.get("tests") or {}).get("targets")):
                continue

            executable = bool(final_meta.get("executable", False))
            if not executable and hasattr(origin, "exists") and origin.exists():
                import os
                executable = os.access(origin, os.X_OK)

            weight = self._get_path_weight(rel)

            if dest not in items_dict or weight > items_dict[dest]["weight"]:
                items_dict[dest] = {
                    "rel": rel, "inline_template": inline_template, "dest": dest,
                    "context": final_meta.get("context", "."),
                    "updatable": bool(final_meta.get("updatable", True)),
                    "header_managed": final_meta.get("header_managed"),
                    "origin": origin, "executable": executable, "weight": weight
                }

        return list(items_dict.values())

    def _discover_copy_items(self) -> list[dict]:
        items_dict = {}
        stack = self.cfg.get("stack")
        stack_type = self.cfg.get("stack_type")

        for rel, data, is_j2, origin in self.tmpl_src.iter_files():
            if is_j2 or posixpath.basename(rel) in {".scaffold.yaml", "aliases.yaml", "scaffold.yaml"}: continue
            if not self._is_active_path(rel): continue
            if self._is_resource_file(rel): continue

            # Determine logical source for pattern matching
            logical_source = self._strip_routing_prefixes(rel, stack, stack_type)

            # ── EVALUATE LOGICAL INHERITANCE RULES ──
            rules = self._collect_rules(rel, stack, stack_type)
            final_meta = self._evaluate_template_rules(logical_source, rules)

            if final_meta.get("on_init") and not self.is_init:
                continue

            # Evaluate final destination
            dest = final_meta.get("dest", logical_source)
            if not dest or (dest.startswith("tests/") and not (self.cfg.get("tests") or {}).get("targets")):
                continue

            executable = bool(final_meta.get("executable", False))
            if not executable and hasattr(origin, "exists") and origin.exists():
                import os
                executable = os.access(origin, os.X_OK)

            weight = self._get_path_weight(rel)

            if dest not in items_dict or weight > items_dict[dest]["weight"]:
                items_dict[dest] = {
                    "rel": rel, "dest": dest, "bytes": data,
                    "updatable": bool(final_meta.get("updatable", True)),
                    "origin": origin, "executable": executable, "weight": weight
                }

        return list(items_dict.values())

    def _plan_subproject_resources(self, *, show_diffs: bool) -> list[PlanItem]:
        rules = self.cfg.get("subproject_rules", {})
        if not rules: return []

        env, plan, base = self._jinja_env_for_inline(), [], self._build_ctx_inherited("deps")

        for block_name, rule in rules.items():
            block_data = self.cfg.get(block_name)
            if not isinstance(block_data, dict): continue

            resource_dir = rule.get("resource")
            if not resource_dir: continue

            possible_resources = [(rel, data, is_j2, origin) for rel, data, is_j2, origin in self.tmpl_src.iter_files() if f"/{resource_dir}/" in rel or rel.startswith(f"{resource_dir}/")]
            if not possible_resources: continue

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

                rctx.setdefault("project_name", self.cfg.get("project_name") or "project")
                rctx.setdefault("project_slug", slug(rctx["project_name"]))
                rctx.setdefault("project_snake", snake(rctx["project_slug"]))

                app_scoped_key = f"{app_stack}_{app_stack_type}".strip("_").lower()
                if app_scoped_key in self.cfg:
                    rctx = deep_merge(rctx, self.cfg[app_scoped_key])

                if ctx_name == "default" or not ctx_name:
                    rctx.setdefault("subproject_name", f"{base.get('project_snake','project')}_{block_name}")
                else:
                    rctx.setdefault("subproject_name", f"{base.get('project_snake','project')}_{ctx_name}")

                rctx.setdefault("subproject_stack", app_stack)
                rctx.setdefault("subproject_stack_type", app_stack_type)
                rctx.setdefault("subproject_block", block_name)

                sub_plan_dict = {}

                for rel, data, is_j2, origin in possible_resources:
                    if not self._is_active_path(rel, stack=app_stack, stack_type=app_stack_type):
                        continue

                    stripped = self._strip_routing_prefixes(rel, stack=app_stack, stack_type=app_stack_type)
                    if not stripped.startswith(f"{resource_dir}/"):
                        continue

                    # Determine logical source for subprojects
                    sub_rel = stripped[len(f"{resource_dir}/"):]
                    logical_source = sub_rel[:-3] if (is_j2 and sub_rel.endswith('.j2')) else sub_rel

                    # Compute raw destination early just in case no rules alter it
                    raw_dest = f"{dest_dir}/{logical_source}"
                    dest_rel = posixpath.normpath(raw_dest).lstrip("./")

                    inline_meta = {}
                    if is_j2:
                        text = data.decode("utf-8", errors="replace")
                        inline_meta, inline_template = _extract_annotation(text)

                    # ── EVALUATE LOGICAL INHERITANCE RULES ──
                    template_rules = self._collect_rules(rel, app_stack, app_stack_type)
                    rule_meta = self._evaluate_template_rules(logical_source, template_rules)

                    final_meta = deep_merge(rule_meta, inline_meta or {})

                    if final_meta.get("on_init") and not self.is_init:
                        continue

                    if "dest" in final_meta:
                        try:
                            override_dest = env.from_string(str(final_meta["dest"])).render(**rctx).strip()
                        except Exception:
                            override_dest = str(final_meta["dest"]).strip()

                        if not override_dest:
                            continue
                        dest_rel = posixpath.normpath(f"{dest_dir}/{override_dest}").lstrip("./")

                    updatable = bool(final_meta.get("updatable", True))
                    header_managed_meta = final_meta.get("header_managed")
                    is_exec = bool(final_meta.get("executable", False))

                    if is_j2:
                        try: new_bytes = env.from_string(inline_template).render(**rctx).encode("utf-8")
                        except Exception as e: raise RuntimeError(f"Jinja render error in subproject resource '{rel}' → '{dest_rel}': {e}") from e
                        tmpl_sha = sha256(inline_template.encode("utf-8"))
                    else:
                        new_bytes, tmpl_sha = data, sha256(data)

                    hm = _header_managed_default(dest_rel) if header_managed_meta is None else bool(header_managed_meta)

                    if not is_exec and hasattr(origin, "exists") and origin.exists():
                        import os
                        is_exec = os.access(origin, os.X_OK)

                    weight = self._get_path_weight(rel, stack=app_stack, stack_type=app_stack_type)

                    if dest_rel not in sub_plan_dict or weight > sub_plan_dict[dest_rel]["weight"]:
                        target = self.repo / dest_rel
                        old_text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
                        cmp_new, cmp_old = _normalize_for_cmp(new_bytes.decode("utf-8", errors="replace"), target, hm), _normalize_for_cmp(old_text, target, hm)
                        status = "create" if not target.exists() else ("update" if cmp_old != cmp_new else "unchanged")
                        diff_text = _diff(old_text.encode("utf-8"), cmp_new.encode("utf-8"), dest_rel) if show_diffs and status in ("create", "update") else ""

                        sub_plan_dict[dest_rel] = {
                            "weight": weight,
                            "item": PlanItem("jinja" if is_j2 else "copy", dest_rel, status, updatable, diff_text, new_bytes, tmpl_sha, f"{block_name}.{ctx_name}", hm, is_exec)
                        }

                plan.extend([v["item"] for v in sub_plan_dict.values()])

        return plan
