# src/scaffold_repo/cli/ui.py
import sys
from typing import Any

def interactive_select(prompt: str, options: list[str], default_idx: int = 0, multiselect: bool = False) -> list[int] | int:
    if multiselect:
        print(f"\n{prompt}")
        for i, opt in enumerate(options):
            print(f"  {i+1}) {opt}")
        while True:
            ans = input(f"Select one or more [1-{len(options)}] (comma-separated): ").strip()
            if not ans: return [default_idx]
            try:
                indices = [int(x.strip()) - 1 for x in ans.split(",")]
                if all(0 <= x < len(options) for x in indices):
                    return indices
            except ValueError: pass
            print("Invalid selection. Use comma-separated numbers (e.g., 1,3).")

    try:
        import tty, termios
    except ImportError:
        print(prompt)
        for i, opt in enumerate(options):
            print(f"  {i+1}) {opt}")
        while True:
            ans = input(f"Select [1-{len(options)}] (default {default_idx+1}): ").strip()
            if not ans: return default_idx
            if ans.isdigit() and 1 <= int(ans) <= len(options): return int(ans) - 1
            print("Invalid selection.")

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    selected = default_idx
    sys.stdout.write("\033[?25l")
    try:
        tty.setraw(fd)
        while True:
            sys.stdout.write(f"\r{prompt}\n")
            for i, opt in enumerate(options):
                prefix = "  ❯ " if i == selected else "    "
                color = "\033[96m" if i == selected else ""
                reset = "\033[0m"
                sys.stdout.write(f"\r{prefix}{color}{opt}{reset}\033[K\n")
            sys.stdout.flush()
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                ch2 = sys.stdin.read(2)
                if ch2 == '[A': selected = max(0, selected - 1)
                elif ch2 == '[B': selected = min(len(options) - 1, selected + 1)
            elif ch in ('\r', '\n'): break
            elif ch == '\x03': raise KeyboardInterrupt
            sys.stdout.write(f"\033[{len(options) + 1}A")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
    sys.stdout.write(f"\033[{len(options) + 1}A\r\033[K\033[J")
    sys.stdout.flush()
    return selected

def print_text_result(res: dict[str, Any]) -> None:
    s = res.get("summary", {})
    print(f"\n\033[1m=== {res['repo']} ===\033[0m")
    print(f"files_checked: {s.get('files_checked', 0)}  headers_added: {s.get('headers_added', 0)}  headers_updated: {s.get('headers_updated', 0)}  unchanged: {s.get('unchanged', 0)}")
    if s.get("profiles_used"): print(f"profiles_used: {', '.join(s['profiles_used'])}")
    for it in res.get("issues", []):
        kind = it.get("type", "issue")
        print(f"- {kind}: {it['file']}" if "file" in it else f"- {kind}: {it.get('message', '')}")
