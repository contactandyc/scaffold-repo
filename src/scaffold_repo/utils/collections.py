from typing import Any, Iterable

def coerce_list(x) -> list[Any]:
    if x is None: return []
    if isinstance(x, (list, tuple)): return list(x)
    return [x]

def dedupe(seq: Iterable[Any]) -> list[Any]:
    out = []
    seen_hashable = set()
    for s in seq:
        try:
            if s in seen_hashable:
                continue
            seen_hashable.add(s)
            out.append(s)
        except TypeError:
            if s not in out:
                out.append(s)
    return out

def deep_merge(a, b):
    # If either 'a' or 'b' is not a dict (e.g., they are lists, strings, etc.),
    # 'b' overwrites 'a' (unless 'b' is None, in which case 'a' is kept).
    if not isinstance(a, dict) or not isinstance(b, dict):
        return b if b is not None else a

    out = dict(a)
    for k, v in b.items():
        out[k] = deep_merge(out.get(k), v)
    return out
