from typing import Any, Iterable

def _coerce_list(x) -> list[Any]:
    if x is None: return []
    if isinstance(x, (list, tuple)): return list(x)
    return [x]

def _dedupe(seq: Iterable[Any]) -> list[Any]:
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

def _deep_merge(a, b):
    if isinstance(a, list) and isinstance(b, list): return _dedupe(a + b)
    if not isinstance(a, dict) or not isinstance(b, dict): return b if b is not None else a
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out.get(k), v)
    return out
