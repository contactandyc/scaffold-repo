#!/usr/bin/env python3
import argparse
import os
import sys
import subprocess

try:
    import pyperclip
except ImportError:
    pyperclip = None

# Built-in directory skips regardless of .gitignore
DEFAULT_SKIP_DIRS = {
    ".git", ".svn", ".hg",
    "__pycache__", ".tox", ".venv", "venv",
    "node_modules", "build", "dist",
    ".idea", ".vscode",
}

def parse_args():
    p = argparse.ArgumentParser(description="Show or reconstruct source files.")
    p.add_argument("--ext",       default="",   help="Comma-separated list of extensions to include (e.g. py,sh)")
    p.add_argument("--exclude",   default="",   help="Comma-separated list of paths to skip (substring match)")
    p.add_argument("--reconstruct", action="store_true",
                   help="Rebuild files & empty dirs from stdin or clipboard")
    p.add_argument("--output-dir", default=".",
                   help="Where to write reconstructed files/dirs")
    p.add_argument("--no-gitignore", action="store_true",
                   help="Do NOT honor .gitignore (default: honor if in a Git repo)")
    p.add_argument("paths", nargs="*", help="Files or dirs to scan (ignored in reconstruct mode)")
    return p.parse_args()

def should_include(path, exts, excludes):
    if excludes and any(excl in path for excl in excludes):
        return False
    if not exts:
        return True
    return any(path.endswith(f".{e}") for e in exts)

# ---------- .gitignore-aware matcher ----------
class GitIgnoreMatcher:
    def __init__(self, paths, disabled=False):
        self.disabled = disabled
        self.git_root = None
        if disabled:
            return
        # Try to find a git root from one of the provided paths or CWD
        candidates = [os.getcwd()]
        candidates = (paths or candidates)
        start = os.path.abspath(candidates[0])
        self.git_root = self._find_git_root(start)
        if self.git_root and not self._git_available():
            self.git_root = None  # no git → disable

    def _git_available(self):
        try:
            subprocess.run(["git", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            return True
        except Exception:
            return False

    def _find_git_root(self, start):
        # 1) ask git (fast/accurate)
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=start, capture_output=True, text=True, check=True
            )
            root = res.stdout.strip()
            if root:
                return os.path.abspath(root)
        except Exception:
            pass
        # 2) walk up looking for a .git dir
        cur = start
        while True:
            if os.path.isdir(os.path.join(cur, ".git")):
                return cur
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        return None

    def _to_git_rel(self, path, is_dir=False):
        # Convert to path relative to git_root using POSIX separators for git
        rel = os.path.relpath(os.path.abspath(path), self.git_root)
        rel = rel.replace(os.sep, "/")
        if is_dir and not rel.endswith("/"):
            rel += "/"
        return rel

    def batch_ignored(self, paths, is_dir=False):
        """
        Return the subset of 'paths' that are ignored by git according to .gitignore.
        """
        if self.disabled or not self.git_root or not paths:
            return set()

        rels = [self._to_git_rel(p, is_dir=is_dir) for p in paths]
        # Prepare NUL-separated input for git check-ignore
        data = b"\0".join(s.encode("utf-8", "surrogatepass") for s in rels) + b"\0"
        try:
            # Use -z for NUL delim, --stdin to feed many at once, -q to keep output minimal
            # Note: exit code is 0 if some matched, 1 if none matched.
            res = subprocess.run(
                ["git", "-C", self.git_root, "check-ignore", "-z", "--stdin"],
                input=data, capture_output=True
            )
            out = res.stdout.split(b"\0")
            matched_rels = {s.decode("utf-8", "replace") for s in out if s}
            # Map back to original absolute paths by index
            rel_to_abs = {rels[i]: paths[i] for i in range(len(rels))}
            return {rel_to_abs[r] for r in matched_rels if r in rel_to_abs}
        except Exception:
            # On any failure, don't exclude via git
            return set()

def show_sources(exts, excludes, paths, matcher: "GitIgnoreMatcher"):
    # Normalize exclude fragments once
    excludes = excludes or []

    def prune_dirs(dirpath, dirnames):
        # 1) Drop built-in junk and explicit excludes
        kept = []
        to_check = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if d in DEFAULT_SKIP_DIRS:
                continue
            if excludes and any(excl in full for excl in excludes):
                continue
            kept.append(d)
            to_check.append(full)
        # 2) Remove those ignored by git
        ignored = matcher.batch_ignored(to_check, is_dir=True)
        dirnames[:] = [d for d in kept if os.path.join(dirpath, d) not in ignored]

    def filter_files(dirpath, filenames):
        # First apply ext/exclude filters
        included = [f for f in filenames if should_include(os.path.join(dirpath, f), exts, excludes)]
        if not included:
            return []
        fulls = [os.path.join(dirpath, f) for f in included]
        ignored = matcher.batch_ignored(fulls, is_dir=False)
        return [f for f in included if os.path.join(dirpath, f) not in ignored]

    for root in paths:
        if os.path.isfile(root):
            # Single-file mode: honor gitignore and excludes
            if matcher.batch_ignored([root]) or not should_include(root, exts, excludes):
                continue
            process_file(root, exts, excludes)
        else:
            for dirpath, dirnames, filenames in os.walk(root):
                prune_dirs(dirpath, dirnames)
                included = filter_files(dirpath, filenames)
                # Emit empty-dir block if nothing left here
                if not dirnames and not included:
                    print(f"Directory: {dirpath}")
                    print("~~~")
                    print("~~~")
                for fname in included:
                    process_file(os.path.join(dirpath, fname), exts, excludes)

def process_file(path, exts, excludes):
    if not should_include(path, exts, excludes):
        return
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        print(f"Warning: could not read {path}: {e}", file=sys.stderr)
        return

    print(f"Source: {path}")
    print("~~~")
    sys.stdout.write(content)
    if not content.endswith("\n"):
        print()
    print("~~~")

def flush_file(output_dir, relpath, buffer):
    out = os.path.join(output_dir, relpath)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.writelines(buffer)
    print(f"Reconstructed file: {out}")

def reconstruct(output_dir):
    # grab from stdin or clipboard
    if not sys.stdin.isatty():
        lines = sys.stdin.read().splitlines(keepends=True)
    elif pyperclip:
        lines = pyperclip.paste().splitlines(keepends=True)
    else:
        sys.exit("No input: pipe into stdin or install pyperclip for clipboard support.")

    idx = 0
    total = len(lines)
    current_file = None
    buffer = []
    reading = False

    while idx < total:
        line = lines[idx]

        # ---- directory marker ----
        if line.startswith("Directory: "):
            d = line[len("Directory: "):].rstrip("\n")
            os.makedirs(os.path.join(output_dir, d), exist_ok=True)
            print(f"Reconstructed dir: {os.path.join(output_dir, d)}")
            # skip the next two “~~~” lines
            idx += 1
            if idx < total and lines[idx].strip() == "~~~":
                idx += 1
            if idx < total and lines[idx].strip() == "~~~":
                idx += 1
            continue

        # ---- new file start ----
        if line.startswith("Source: "):
            if current_file and buffer:
                flush_file(output_dir, current_file, buffer)
            current_file = line[len("Source: "):].rstrip("\n")
            buffer = []
            reading = False
            idx += 1
            continue

        # ---- delimiter ----
        if line.strip() == "~~~":
            if not reading:
                reading = True
            else:
                if current_file is not None:
                    flush_file(output_dir, current_file, buffer)
                current_file = None
                buffer = []
                reading = False
            idx += 1
            continue

        # ---- content lines ----
        if reading and current_file is not None:
            buffer.append(line)

        idx += 1

    if current_file and buffer:
        flush_file(output_dir, current_file, buffer)

def main():
    args = parse_args()
    exts     = [e for e in args.ext.split(",")     if e]
    excludes = [e for e in args.exclude.split(",") if e]

    if args.reconstruct:
        reconstruct(args.output_dir)
        return

    matcher = GitIgnoreMatcher(args.paths, disabled=args.no_gitignore)
    # Default scan target if none provided
    paths = args.paths or ["."]
    show_sources(exts, excludes, paths, matcher)

if __name__ == "__main__":
    main()
