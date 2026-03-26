# src/scaffold_repo/templating/source.py
from __future__ import annotations

import hashlib
import posixpath
import sys
import urllib.request
import urllib.error
from pathlib import Path

import yaml

from ..utils.collections import coerce_list, deep_merge, dedupe

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

        return dedupe(out)

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
                        stacked = deep_merge(stacked, data)
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
            if root_data: data = deep_merge(root_data, data)

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
                include_keys = coerce_list(inc.get("include", []))
                exclude_keys = coerce_list(inc.get("exclude", []))

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

                base = deep_merge(base, inc_data)

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

            return deep_merge(base, data)

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
