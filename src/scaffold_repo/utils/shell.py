import shlex
import subprocess
from pathlib import Path

def run(cmd: list[str] | str, *, cwd: Path | None = None, shell: bool = False) -> None:
    if shell:
        print(f"$ (in {cwd or Path.cwd()}) {cmd}")
        subprocess.run(cmd, cwd=cwd, shell=True, check=True, executable="/bin/bash")
    else:
        print("$", " ".join(shlex.quote(c) for c in (cmd if isinstance(cmd, list) else [cmd])))
        subprocess.run(cmd if isinstance(cmd, list) else [cmd], cwd=cwd, check=True)

def run_steps_chain(steps: list[str], *, cwd: Path, stack: str = "generic", stack_type: str = "") -> None:
    if not steps: return

    clean_print = " && \\\n  ".join(steps)
    print(f"$ {clean_print}")

    env_name = f"{stack}_{stack_type}".strip("_").lower()
    env_file = f".scaffoldrc_{env_name}" if env_name else ".scaffoldrc"

    prologue = f"""
        set -Eeuo pipefail
        nproc() {{ (command -v nproc >/dev/null && command nproc) || (sysctl -n hw.ncpu 2>/dev/null) || (getconf _NPROCESSORS_ONLN 2>/dev/null) || echo 4; }}
        
        _cur="$PWD"
        while [ "$_cur" != "/" ]; do
          if [ -f "$_cur/.scaffoldrc.yaml" ]; then
            [ -f "$_cur/{env_file}" ] && source "$_cur/{env_file}"
            break
          fi
          _cur="$(dirname "$_cur")"
        done
        [ -z "${{WORKSPACE_DIR:-}}" ] && [ -f "$HOME/{env_file}" ] && source "$HOME/{env_file}" || true
        
        SUDO=""
        if [[ "${{PREFIX:-/usr/local}}" == "/usr"* || "${{PREFIX:-/usr/local}}" == "/opt"* || "${{PREFIX:-/usr/local}}" == "/Library"* ]] && [[ "${{EUID:-$(id -u)}}" -ne 0 ]]; then
            SUDO="sudo "
        fi
    """

    chain = " && \\\n  ".join(steps)
    script = prologue + "\n" + chain + "\n"

    subprocess.run(["bash", "-lc", script], cwd=cwd, check=True)
