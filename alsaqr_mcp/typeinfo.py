"""Best-effort struct field names for xsim's anonymous '{a,b,c} values.

Sources: `typedef struct packed { ... } name;` in the RTL tree, and the AXI/ACE typedef macros in
axi/typedef.svh (whose bodies are `typedef struct packed {...} req_t;` with macro parameters).
"""
import json
import re

from . import sources
from .config import Config

_CACHE = None
_CACHE_FP = None
_TYPEDEF_RE = re.compile(r"typedef\s+struct\s+packed\s*\{(.*?)\}\s*([A-Za-z_]\w*)\s*;", re.S)
_MEMBER_RE = re.compile(r"^\s*(?:[A-Za-z_][\w:]*(?:\s*\[[^\]]*\])*\s+)+([A-Za-z_]\w*)\s*(?:\[[^\]]*\])*\s*;", re.M)


def _strip_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def _members(body: str) -> list[str]:
    body = _strip_comments(body)
    out = []
    for stmt in body.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        # "<type> a, b [3], c;" -> split declarators at top-level commas, keep each declarator's identifier
        depth, cur, decls = 0, "", []
        for ch in stmt:
            if ch in "[(":
                depth += 1
            elif ch in "])":
                depth -= 1
            if ch == "," and depth == 0:
                decls.append(cur)
                cur = ""
            else:
                cur += ch
        decls.append(cur)
        first = decls[0]
        m = re.match(r"^(.*?)([A-Za-z_]\w*)\s*(\[[^\]]*\])*$", first.strip(), re.S)
        if not (m and m.group(1).strip()):
            continue
        out.append(m.group(2))
        for dcl in decls[1:]:
            m2 = re.match(r"^\s*([A-Za-z_]\w*)\s*(\[[^\]]*\])*\s*$", dcl)
            if m2:
                out.append(m2.group(1))
    return out


def build_index(cfg: Config) -> dict:
    """{type_name: [member names]} from the compiled sources and the headers they can include. Cached on disk and
    rebuilt when a compiled source file changes."""
    global _CACHE, _CACHE_FP
    fp = sources.fingerprint(cfg)
    if _CACHE is not None and _CACHE_FP == fp:
        return _CACHE
    cache_file = cfg.state / "typeinfo_cache.json"
    try:
        cached = json.loads(cache_file.read_text())
        if cached.get("fingerprint") == fp:
            _CACHE, _CACHE_FP = cached["index"], fp
            return _CACHE
    except (OSError, ValueError, AttributeError, KeyError):
        pass
    index: dict[str, list[str]] = {}
    for p in sources.design_files(cfg):
        if p.suffix not in (".sv", ".svh"):
            continue
        try:
            txt = p.read_text(errors="replace")
        except OSError:
            continue
        if "typedef" not in txt:
            continue
        txt = txt.replace("\\\n", "\n")  # macro line continuations
        for m in _TYPEDEF_RE.finditer(txt):
            name, members = m.group(2), _members(m.group(1))
            if members and members not in index.setdefault(name, []):
                index[name].append(members)  # a name may carry several distinct layouts (packages, macros)
    _CACHE, _CACHE_FP = index, fp
    try:
        cfg.state.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"fingerprint": fp, "index": index}))
    except OSError:
        pass
    return index


def _split_top(value: str) -> list[str] | None:
    """Split a value like "'{a,'{b,c},d}" or "a,'{b,c},d" (bare outer struct) into top-level elements."""
    def split(inner: str) -> list[str]:
        depth, cur, out = 0, "", []
        for ch in inner:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            if ch == "," and depth == 0:
                out.append(cur.strip())
                cur = ""
            else:
                cur += ch
        out.append(cur.strip())
        return out
    v = value.strip()
    if v.startswith("'{") and v.endswith("}"):
        # is the whole string one balanced group?  ("'{a},'{b}" is not)
        depth = 0
        for k, ch in enumerate(v):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and k != len(v) - 1:
                    return split(v)
        return split(v[2:-1])
    if "," in v:
        return split(v)
    return None


def name_fields(cfg: Config, value: str, type_hint: str | None) -> dict | None:
    """Return {member: value} for a struct value when a typedef with matching arity is known."""
    parts = _split_top(value)
    if not parts:
        return None
    index = build_index(cfg)
    if type_hint:
        for tok in re.findall(r"[A-Za-z_]\w*", type_hint):
            for members in index.get(tok, []):
                if len(members) == len(parts):
                    return {"type": tok, "fields": dict(zip(members, parts))}
    return None


def candidates_by_arity(cfg: Config, n: int, limit: int = 80) -> list[tuple[str, list[str]]]:
    """Typedefs with exactly n members, deduplicated by member list (order preserved by name)."""
    index = build_index(cfg)
    seen, out = set(), []
    for name in sorted(index):
        for members in index[name]:
            if len(members) != n:
                continue
            key = tuple(members)
            if key in seen:
                continue
            seen.add(key)
            out.append((name, members))
    return out[:limit]
