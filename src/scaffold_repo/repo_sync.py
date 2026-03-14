from __future__ import annotations

import json
import fnmatch
import os
import re
import stat
from pathlib import Path
from typing import Any

from jinja2 import Environment

from .config_reader import (
    ConfigReader,
    TemplateSource,
    _comment_style_for,
    _ensure_trailing_newline,
    _sha256,
)
import yaml


def verify_repo(
        repo: Path,
        *,
        fix_licenses: bool = False,
        no_prompt: bool = False,
        include_exts: set[str] | None = None,
        project_name: str | None = None,
        templates_dir: str | None = None,
        assume_yes: bool = False,
        show_diffs: bool = False,
) -> tuple[int, dict[str, Any]]:
    rc_apply = apply_repo(
        repo,
        project_name=project_name,
        templates_dir=templates_dir,
        assume_yes=assume_yes,
        show_diffs=show_diffs,
    )

    reader = ConfigReader(repo, project_name=project_name, templates_dir=templates_dir)
    reader.load()
    res = validate_licenses(
        repo,
        cfg=reader.effective_config,
        resource_reader=reader.read_resource_text,
        include_exts=include_exts,
        apply_fixes=fix_licenses,
        no_prompt=no_prompt,
    )
    exit_code = rc_apply if rc_apply != 0 else (1 if res.get("issues") else 0)
    return exit_code, res


def apply_repo(
        repo: Path,
        *,
        project_name: str | None = None,
        templates_dir: str | None = None,
        assume_yes: bool = False,
        show_diffs: bool = False,
) -> int:
    repo = Path(repo).resolve()
    reader = ConfigReader(repo, project_name=project_name, templates_dir=templates_dir)
    try:
        reader.load()
    except Exception as e:
        print(f"\n❌ Config/template load failed:\n{e}", file=os.sys.stderr)
        return 3

    jinja_plan = reader.plan_jinja(show_diffs=show_diffs)
    copy_plan = reader.plan_copy(show_diffs=show_diffs)

    state: dict[str, Any] = {}
    early = [i for i in (jinja_plan + copy_plan) if i.path == ".gitignore" and i.status in ("create", "update")]
    if early:
        print("\nApplying .gitignore early …")
        _apply_items(repo, early, state)
        jinja_plan = [i for i in jinja_plan if i not in early]
        copy_plan  = [i for i in copy_plan  if i not in early]

    _print_summary("Jinja templates (*.j2 → rendered)", jinja_plan)
    _print_summary("Non-Jinja files (verbatim copy)", copy_plan)

    jinja_to_apply = [i for i in jinja_plan if i.status in ("create", "update") and (i.status == "create" or i.updatable)]
    j_updates = [i for i in jinja_to_apply if i.status == "update"]

    if j_updates:
        print("\nJinja updates (batched):")
        for it in j_updates:
            print(f"  • {it.path}")
        if show_diffs:
            for it in j_updates:
                if it.diff:
                    print(f"\n--- diff: {it.path} ---")
                    print(it.diff.rstrip())
        if not assume_yes:
            ans = input("Apply Jinja updates? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                jinja_to_apply = [i for i in jinja_to_apply if i.status == "create"]

    copy_to_apply: list[Any] = []
    for it in copy_plan:
        if it.status == "create":
            copy_to_apply.append(it)
        elif it.status == "update":
            print(f"\nNon-Jinja update: {it.path}")
            if it.diff and show_diffs:
                print(it.diff.rstrip())
            if assume_yes:
                copy_to_apply.append(it)
            else:
                if it.diff and not show_diffs:
                    print(it.diff.rstrip())
                ans = input("Apply this update? [y/N] ").strip().lower()
                if ans in ("y", "yes"):
                    copy_to_apply.append(it)

    _apply_items(repo, jinja_to_apply, state)
    _apply_items(repo, copy_to_apply, state)

    changed = bool(jinja_to_apply or copy_to_apply)

    if changed:
        validate_licenses(
            repo,
            cfg=reader.effective_config,
            resource_reader=reader.read_resource_text,
            apply_fixes=True,
            no_prompt=True,
        )

    return 0


def _make_gitignore_matcher(repo: Path):
    gi = repo / ".gitignore"
    if not gi.exists():
        return None

    try:
        from pathspec import PathSpec
        spec = PathSpec.from_lines("gitwildmatch", gi.read_text(encoding="utf-8").splitlines())
        return spec.match_file
    except Exception:
        lines = gi.read_text(encoding="utf-8", errors="ignore").splitlines()
        pats, neg = [], []
        for raw in lines:
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            (neg if s.startswith("!") else pats).append(s[1:] if s.startswith("!") else s)

        from pathlib import PurePosixPath

        def _expand(pat: str):
            dir_only = pat.endswith("/")
            pat = pat[:-1] if dir_only else pat
            anchored = pat.startswith("/")
            pat = pat.lstrip("/")
            cores = [pat] if anchored else [pat, f"**/{pat}"]
            return [c + "/**" for c in cores] if dir_only else cores

        rules = [(_expand(p), False) for p in pats] + [(_expand(p), True) for p in neg]

        def _match(rel_posix: str) -> bool:
            path = PurePosixPath(rel_posix)
            ignored = False
            for cands, is_neg in rules:
                if any(path.match(c) for c in cands):
                    ignored = not is_neg
            return ignored

        return _match


def iter_repo_files_for_license_check(repo: Path, include_exts: set[str]):
    repo = Path(repo).resolve()
    is_ignored = _make_gitignore_matcher(repo)
    for p in sorted(repo.rglob("*")):
        if not p.is_file():
            continue
        if set(p.parts) & _OSS_SKIP_DIRS:
            continue
        rel = p.relative_to(repo).as_posix()
        if is_ignored and is_ignored(rel):
            continue
        if p.name != "CMakeLists.txt" and p.suffix.lower() not in include_exts:
            continue
        yield p


_OSS_HEADER_EXTS = {
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".java", ".js", ".ts",
    ".tsx", ".mjs", ".cjs", ".go", ".rs", ".swift", ".kt", ".cs",
    ".cmake", ".mk", ".make",
}

_OSS_NEWLINE_EXTS = {
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".py", ".sh", ".bash",
    ".zsh", ".md", ".txt",
}

_OSS_SKIP_DIRS = {
    ".git", ".svn", ".hg", ".tox", ".venv", "venv", "node_modules",
    "build", "dist", "__pycache__", ".idea", ".vscode",
}

def validate_licenses(
        repo: Path,
        *,
        cfg: dict | None = None,
        resource_reader: callable | None = None,
        include_exts: set[str] | None = None,
        apply_fixes: bool = False,
        no_prompt: bool = False,
) -> dict[str, Any]:
    repo = Path(repo).resolve()

    # ── Enforced Standards ──
    notice_file = "NOTICE"

    res: dict[str, Any] = {"repo": str(repo), "issues": [], "summary": {}}

    if cfg is None or resource_reader is None:
        reader = ConfigReader(repo)
        reader.load()
        if cfg is None:
            cfg = reader.effective_config
        if resource_reader is None:
            resource_reader = reader.read_resource_text

    licenses: dict[str, dict[str, Any]] = (cfg.get("licenses") or {})
    default_profile: str | None = cfg.get("license_profile")
    overrides_map: dict[str, str] = (cfg.get("license_overrides") or {})
    extras_map: dict[str, Any] = (cfg.get("license_extras") or {})

    if not licenses:
        res["issues"].append({"type": "config_error", "message": "No 'licenses' map found in defaults/repo config."})
        return res
    if not default_profile:
        default_profile = next(iter(licenses.keys()), None)
    if not default_profile or default_profile not in licenses:
        res["issues"].append({"type": "config_error", "message": "Missing or invalid 'license_profile'."})
        return res

    year = (str(cfg.get("date") or "")[:4]) or "2025"
    ctx = {
        "project_name": cfg.get("project_name") or cfg.get("project_title") or repo.name,
        "project_title": cfg.get("project_title") or cfg.get("project_name") or repo.name,
        "author": cfg.get("author", ""),
        "email": cfg.get("email", ""),
        "year": year,
        **{k: v for k, v in cfg.items() if k != "licenses"},
    }

    jenv = Environment(autoescape=False, keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True)

    decisions: dict[str, str] = {}

    files_checked = headers_added = headers_updated = unchanged = 0
    profiles_used: set[str] = set()
    profiles_order: list[str] = []

    cfg_exts = cfg.get("license_header_extensions")
    if include_exts is None:
        include_exts = {(e if str(e).startswith(".") else "." + str(e)).lower() for e in cfg_exts} if cfg_exts else set(_OSS_HEADER_EXTS)

    for p in iter_repo_files_for_license_check(repo, include_exts):
        if not p.is_file():
            continue
        if set(p.parts) & _OSS_SKIP_DIRS:
            continue
        if p.name != "CMakeLists.txt" and p.suffix.lower() not in include_exts:
            continue

        if p.suffix.lower() in _OSS_NEWLINE_EXTS:
            _oss_check_final_newline(p, res["issues"], apply_fixes=True)

        rel = p.relative_to(repo).as_posix()

        base_prof_name = default_profile
        base_prof = licenses.get(base_prof_name) or {}

        override_prof_name = None
        for pat, pn in (overrides_map or {}).items():
            if fnmatch.fnmatch(rel, pat):
                override_prof_name = pn
                break
        ovr_prof = licenses.get(override_prof_name) or {}

        effective_prof = dict(base_prof)
        for k, v in (ovr_prof or {}).items():
            if v is None or (isinstance(v, str) and not v.strip()):
                effective_prof.pop(k, None)
            else:
                effective_prof[k] = v
        if override_prof_name and "spdx" not in ovr_prof:
            effective_prof.pop("spdx", None)

        raw_spdx_eff = (effective_prof.get("spdx") or "").rstrip()
        try:
            spdx_text_eff = jenv.from_string(raw_spdx_eff).render(**ctx).rstrip()
        except Exception as e:
            res["issues"].append({"type": "config_error", "file": rel, "message": f"SPDX Jinja render failed: {e}"})
            unchanged += 1
            continue

        extra_names: list[str] = []
        for pat, val in (extras_map or {}).items():
            if fnmatch.fnmatch(rel, pat):
                if isinstance(val, (list, tuple)):
                    extra_names.extend([str(x) for x in val])
                else:
                    extra_names.append(str(val))

        spdx_extra_chunks: list[str] = []
        for ep_name in extra_names:
            ep_cfg = licenses.get(ep_name) or {}
            ep_raw = (ep_cfg.get("spdx") or "").rstrip()
            if not ep_raw:
                continue
            try:
                ep_spdx = jenv.from_string(ep_raw).render(**ctx).rstrip()
                if ep_spdx:
                    spdx_extra_chunks.append(ep_spdx)
            except Exception as e:
                res["issues"].append({"type": "config_error", "file": rel, "message": f"SPDX(extra) Jinja render failed: {e}"})

        spdx_combined = spdx_text_eff
        if spdx_extra_chunks:
            spdx_combined = (spdx_text_eff + "\n\n" if spdx_text_eff else "") + ("\n\n".join(spdx_extra_chunks))

        def _has_notice(prof_dict): return bool((prof_dict.get("notice") or "").strip())
        if _has_notice(ovr_prof):
            if override_prof_name and override_prof_name not in profiles_used:
                profiles_used.add(override_prof_name)
                profiles_order.append(override_prof_name)
        elif _has_notice(base_prof):
            if base_prof_name and base_prof_name not in profiles_used:
                profiles_used.add(base_prof_name)
                profiles_order.append(base_prof_name)
        for ep_name in extra_names:
            ep_cfg = licenses.get(ep_name) or {}
            if (ep_cfg.get("spdx") or ep_cfg.get("notice")) and ep_name not in profiles_used:
                profiles_used.add(ep_name)
                profiles_order.append(ep_name)

        style = _comment_style_for(p)
        text = p.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        start, end, block, has_spdx = _extract_header_region(lines, style)
        expected_lines = _render_spdx_as_comment(spdx_combined, style)

        found_body_norm = _norm_block(_strip_comment_prefix(block, style))
        expected_body_norm = _norm_block(spdx_combined.splitlines())

        files_checked += 1

        if not has_spdx:
            if apply_fixes:
                new_lines = lines[:start] + expected_lines + lines[start:]
                p.write_text(_ensure_trailing_newline("\n".join(new_lines)), encoding="utf-8")
                headers_added += 1
            else:
                res["issues"].append({"type": "file_missing_spdx_header", "file": rel})
                unchanged += 1
            continue

        if found_body_norm == expected_body_norm:
            unchanged += 1
            continue

        replacement_norm = decisions.get(found_body_norm)
        if replacement_norm and replacement_norm == expected_body_norm:
            if apply_fixes:
                new_lines = lines[:start] + expected_lines + lines[end:]
                p.write_text(_ensure_trailing_newline("\n".join(new_lines)), encoding="utf-8")
                headers_updated += 1
            else:
                res["issues"].append({"type": "file_spdx_header_mismatch", "file": rel, "action": "would_update"})
            continue

        if apply_fixes:
            if no_prompt or not os.sys.stdin.isatty():
                new_lines = lines[:start] + expected_lines + lines[end:]
                p.write_text(_ensure_trailing_newline("\n".join(new_lines)), encoding="utf-8")
                headers_updated += 1
            else:
                import difflib as _dif
                found_render = "\n".join(_strip_comment_prefix(block, style))
                expected_render = "\n".join(spdx_combined.rstrip().splitlines())
                uni = _dif.unified_diff(
                    found_render.splitlines(), expected_render.splitlines(), fromfile="found", tofile="expected", lineterm=""
                )
                print(f"\n— SPDX header mismatch: {rel}")
                print("\n".join(list(uni)) or "(content differs)")
                while True:
                    ans = input("[u]pdate once / [a]pply to all similar / [s]kip ? ").strip().lower()
                    if ans in {"u", "a", "s"}:
                        break
                if ans == "a":
                    decisions[found_body_norm] = expected_body_norm
                if ans != "s":
                    new_lines = lines[:start] + expected_lines + lines[end:]
                    p.write_text(_ensure_trailing_newline("\n".join(new_lines)), encoding="utf-8")
                    headers_updated += 1
                else:
                    res["issues"].append({"type": "file_spdx_header_mismatch", "file": rel, "action": "skipped_by_user"})
        else:
            res["issues"].append({"type": "file_spdx_header_mismatch", "file": rel})

    unique_notices: list[str] = []
    seen_norms: set[str] = set()

    ordered_profiles: list[str] = []
    if default_profile and default_profile in profiles_order:
        ordered_profiles.append(default_profile)
    ordered_profiles.extend([p for p in profiles_order if p != default_profile])

    for prof in ordered_profiles:
        rendered = _render_notice_from_profile(licenses.get(prof, {}) or {}, ctx, resource_reader)
        if not rendered:
            continue
        norm = re.sub(r"\s+", " ", rendered.strip())
        if norm in seen_norms:
            continue
        seen_norms.add(norm)
        unique_notices.append(rendered.rstrip())

    if unique_notices:
        new_notice = ("\n\n").join(unique_notices).rstrip() + "\n"
        nf = repo / notice_file
        old = nf.read_text(encoding="utf-8", errors="ignore") if nf.exists() else ""
        if old != new_notice:
            if apply_fixes:
                nf.write_text(new_notice, encoding="utf-8")
            else:
                res["issues"].append({"type": "notice_out_of_date", "file": notice_file})

    for prof in sorted(profiles_used):
        prof_cfg = licenses.get(prof, {}) or {}
        primary_path = (prof_cfg.get("license") or "").strip()
        canonical_rel = (prof_cfg.get("license_canonical") or "").strip()
        if primary_path and canonical_rel:
            if apply_fixes:
                _check_license_file(repo, [], primary_path, canonical_rel, resource_reader, apply_fixes=True)
            else:
                _check_license_file(repo, res["issues"], primary_path, canonical_rel, resource_reader, apply_fixes=False)
        for x in prof_cfg.get("extra_licenses") or []:
            pth = (x.get("path") or "").strip()
            can = (x.get("canonical") or "").strip()
            if not pth or not can:
                continue
            if apply_fixes:
                _check_license_file(repo, [], pth, can, resource_reader, apply_fixes=True)
            else:
                _check_license_file(repo, res["issues"], pth, can, resource_reader, apply_fixes=False)

    missing_license_texts: list[str] = []
    for prof in sorted(profiles_used):
        prof_cfg = licenses.get(prof, {}) or {}
        lic_path = prof_cfg.get("license")
        lic_can = prof_cfg.get("license_canonical")
        if lic_path and not lic_can and not (repo / lic_path).is_file():
            missing_license_texts.append(lic_path)
    for lic in sorted(set(missing_license_texts)):
        res["issues"].append({"type": "missing_license_text_file", "file": lic})

    res["summary"] = {
        "files_checked": files_checked,
        "headers_added": headers_added,
        "headers_updated": headers_updated,
        "unchanged": unchanged,
        "profiles_used": sorted(profiles_used),
    }
    return res


def _render_spdx_as_comment(spdx_text: str, style: dict[str, str]) -> list[str]:
    raw = spdx_text.rstrip("\n").splitlines()
    if not raw:
        return []
    if style["mode"] == "line":
        p = style["prefix"]
        return [f"{p} {ln}".rstrip() if ln.strip() else f"{p}" for ln in raw] + [""]
    out = [style["open"], *raw, style["close"], ""]
    return out

def _strip_comment_prefix(lines: list[str], style: dict[str, str]) -> list[str]:
    out: list[str] = []
    if not lines:
        return out
    if style["mode"] == "line":
        import re as _re
        pfx = _re.compile(rf"^\s*{_re.escape(style['prefix'])}\s?")
        for ln in lines:
            out.append(pfx.sub("", ln.rstrip()))
    else:
        open_, close_ = style["open"], style["close"]
        body = list(lines)
        if body and body[0].strip().startswith(open_):
            body = body[1:]
        if body and body[-1].strip().endswith(close_):
            body = body[:-1]
        out = [ln.rstrip() for ln in body]
    while out and not out[-1].strip():
        out.pop()
    return out

def _norm_block(lines: list[str]) -> str:
    import re as _re
    cleaned = [_re.sub(r"\s+", " ", ln).strip() for ln in lines]
    cleaned = [ln for ln in cleaned if ln]
    return "\n".join(cleaned)

def _extract_header_region(lines: list[str], style: dict[str, str]) -> tuple[int, int, list[str], bool]:
    def _is_blank(s: str) -> bool:
        return not s.strip()

    i = 1 if (lines and lines[0].startswith("#!")) else 0
    n = len(lines)

    if style["mode"] == "line":
        pref = style["prefix"]
        start = i
        j = start
        while j < n and (_is_blank(lines[j]) or lines[j].lstrip().startswith(pref)):
            j += 1
        top_end = j
        top_block = lines[start:top_end]
        if not top_block:
            return i, i, [], False
        spdx_idxs = [k for k, ln in enumerate(top_block) if "SPDX-" in ln]
        if not spdx_idxs:
            return i, i, [], False
        last_spdx = spdx_idxs[-1]
        k = last_spdx + 1
        while k < len(top_block) and (not _is_blank(top_block[k])) and top_block[k].lstrip().startswith(pref):
            k += 1
        if k < len(top_block) and _is_blank(top_block[k]):
            k += 1
        end = start + k
        block = lines[start:end]
        return start, end, block, True
    else:
        open_, close_ = style["open"], style["close"]
        start = i
        if start < n and lines[start].strip().startswith(open_):
            j = start + 1
            while j < n and not lines[j].strip().endswith(close_):
                j += 1
            j = min(j + 1, n)
            block = lines[start:j]
            has_spdx = any("SPDX-" in ln for ln in block)
            if not has_spdx:
                return i, i, [], False
            end = j
            if end < n and not lines[end].strip():
                end += 1
            return start, end, lines[start:end], True
        return i, i, [], False

def _render_notice_from_profile(prof: dict[str, Any], ctx: dict[str, Any], resource_reader) -> str | None:
    license_file = (prof.get("license") or "LICENSE").strip()
    extra_ctx = dict(ctx)
    extra_ctx["license_file"] = license_file
    tpl_path = str(prof.get("notice_template") or "").strip()
    if tpl_path:
        src = resource_reader(tpl_path)
        if src is None:
            return None
        return Environment(autoescape=False, keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True) \
            .from_string(src).render(**extra_ctx).rstrip() + "\n"
    txt = (prof.get("notice") or "").strip()
    if not txt:
        return None
    return Environment(autoescape=False, keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True) \
        .from_string(txt).render(**extra_ctx).rstrip() + "\n"

def _check_license_file(
        repo: Path,
        issues: list[dict[str, Any]],
        path_in_repo: str,
        canonical_rel: str,
        resource_reader,
        *,
        apply_fixes: bool,
) -> None:
    canon = resource_reader(canonical_rel)
    if canon is None:
        issues.append({"type": "config_error", "file": canonical_rel, "message": "canonical license resource not found"})
        return
    canon_n = _oss_norm_text(canon)
    dst = repo / path_in_repo
    if not dst.exists():
        issues.append({"type": "missing_license_text_file", "file": path_in_repo})
        if apply_fixes:
            dst.write_text(canon, encoding="utf-8")
        return
    have_n = _oss_norm_text(dst.read_text(encoding="utf-8", errors="replace"))
    if have_n != canon_n:
        issues.append({"type": "license_text_mismatch", "file": path_in_repo, "canonical": canonical_rel})
        if apply_fixes:
            dst.write_text(canon, encoding="utf-8")

def _oss_norm_text(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(ln.rstrip() for ln in s.splitlines()).strip() + "\n"

def _oss_check_final_newline(path: Path, issues: list[dict[str, Any]], apply_fixes: bool) -> None:
    text = path.read_bytes()
    if not text.endswith(b"\n"):
        issues.append({"type": "file_missing_final_newline", "file": path.as_posix()})
        if apply_fixes:
            path.write_bytes(text + b"\n")

def _apply_items(repo: Path, items, state: dict) -> None:
    for it in items:
        p = repo / it.path
        if it.status in ("create", "update"):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(it.new_bytes)
            if it.executable:
                mode = p.stat().st_mode
                p.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        files = state.setdefault("files", {})
        files[it.path] = {
            "kind": it.kind,
            "content_sha256": _sha256(p.read_bytes()) if p.exists() else _sha256(it.new_bytes),
            "template_sha256": it.template_sha256,
            **({"context": it.context_key} if it.kind == "jinja" else {}),
        }

def _print_summary(label: str, plan) -> None:
    by = {"create": [], "update": [], "unchanged": []}
    for i in plan:
        by[i.status].append(i.path)
    total = len(plan)
    print(f"\n{label} (total {total})")
    for k in ("create", "update", "unchanged"):
        if by[k]:
            print(f"  {k:8} {len(by[k]):2}  " + ", ".join(by[k])[:120])
