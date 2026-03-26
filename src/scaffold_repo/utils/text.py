import re
import hashlib

_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")

def slug(s: str) -> str:
    s = s.strip().replace("_", "-")
    s = _NON_ALNUM.sub("-", s)
    return re.sub(r"-{2,}", "-", s).strip("-").lower() or "project"

def snake(s: str) -> str:
    s = s.strip().replace("-", "_")
    s = _NON_ALNUM.sub("_", s)
    return re.sub(r"_{2,}", "_", s).strip("_").lower() or "project"

def camel(s: str) -> str:
    parts = re.split(r"[^0-9A-Za-z]+", s)
    return "".join(p[:1].upper() + p[1:].lower() for p in parts if p) or "Project"

def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()
