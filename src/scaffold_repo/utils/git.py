import subprocess
import sys
import hashlib
import re
from pathlib import Path
from .shell import _run

def ensure_clone(url: str, dest: Path, *, branch: str | None, shallow: bool | None) -> None:
    if not dest.exists():
        cmd = ["git", "clone"]
        if shallow: cmd += ["--depth", "1"]
        if branch: cmd += ["--branch", branch, "--single-branch"]
        cmd += [url, str(dest)]
        try: _run(cmd)
        except subprocess.CalledProcessError as e:
            if "Repository not found" in str(e) or "not found" in str(e):
                print(f"⚠️  skip {dest.name}: repo {url} not found")
                return
            raise
        return

    try: _run(["git", "-C", str(dest), "fetch", "--all", "--tags", "--prune"])
    except subprocess.CalledProcessError as e:
        print(f"⚠️  skip {dest.name}: fetch failed ({e})")
        return

    if branch:
        fetch_cmd = ["git", "-C", str(dest), "fetch", "origin", branch]
        if shallow: fetch_cmd += ["--depth", "1"]
        try: _run(fetch_cmd)
        except subprocess.CalledProcessError:
            print(f"⚠️  skip {dest.name}: branch/tag {branch} not found")
            return
        try: _run(["git", "-C", str(dest), "checkout", "-B", branch, f"origin/{branch}"])
        except subprocess.CalledProcessError:
            try: _run(["git", "-C", str(dest), "checkout", "-B", branch, "FETCH_HEAD"])
            except subprocess.CalledProcessError as e:
                print(f"⚠️  skip {dest.name}: cannot checkout {branch} ({e})")
                return
    else:
        try: _run(["git", "-C", str(dest), "pull", "--ff-only"])
        except subprocess.CalledProcessError as e:
            print(f"⚠️  skip {dest.name}: pull failed ({e})")

def sync_git_template_repo(url: str, ref: str, workspace_dir: Path) -> Path | None:
    repo_name = url.split("/")[-1].replace(".git", "")
    local_repo = workspace_dir / repo_name

    if local_repo.is_dir() and (local_repo / ".git").exists():
        try:
            res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=local_repo, capture_output=True, text=True)
            current_branch = res.stdout.strip()

            res_hash = subprocess.run(["git", "rev-parse", "HEAD"], cwd=local_repo, capture_output=True, text=True)
            current_hash = res_hash.stdout.strip()

            if current_branch == ref or current_hash.startswith(ref):
                print(f"  [Templates] \033[96mUsing local workspace repository: {local_repo.name}\033[0m")
                return local_repo
            else:
                print(f"  [Templates] Local repo '{local_repo.name}' is on '{current_branch}', but '{ref}' requested. Falling back to cache.")
        except Exception:
            pass

    safe_name = re.sub(r"[^0-9A-Za-z]+", "-", repo_name)
    slug = f"{safe_name}-{hashlib.md5(f'{url}@{ref}'.encode('utf-8')).hexdigest()[:8]}"
    cache_dir = Path.home() / ".cache" / "scaffold-repo" / "base_templates" / slug

    try:
        if not cache_dir.exists():
            print(f"  [Templates] Fetching base templates to cache: {url} @ {ref}...")
            cache_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", url, str(cache_dir)], capture_output=True, check=True)
            subprocess.run(["git", "checkout", ref], cwd=cache_dir, capture_output=True, check=True)
        else:
            subprocess.run(["git", "fetch", "--all"], cwd=cache_dir, capture_output=True)
            subprocess.run(["git", "checkout", ref], cwd=cache_dir, capture_output=True)
            subprocess.run(["git", "pull", "--rebase"], cwd=cache_dir, capture_output=True)
        return cache_dir
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.decode('utf-8').strip() if e.stderr else 'Unknown Git Error'
        print(f"  \033[91m! Error fetching base_templates from {url}:\n    {err_msg}\033[0m", file=sys.stderr)
        return None
