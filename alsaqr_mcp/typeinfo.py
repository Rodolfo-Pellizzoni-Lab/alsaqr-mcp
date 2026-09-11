"""Best-effort struct field names for xsim's anonymous '{a,b,c} values.

Sources: `typedef struct packed { ... } name;` in the RTL tree, and the AXI/ACE typedef macros in
axi/typedef.svh (whose bodies are `typedef struct packed {...} req_t;` with macro parameters).
"""
import json
import re
from pathlib import Path

from .config import Config

_CACHE = None
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
    """{type_name: [member names]} from every .sv/.svh under the hardware tree (plus overrides)."""
    global _CACHE
    cache_file = cfg.simexp / "typeinfo_cache.json"
    if _CACHE is None and cache_file.exists():
        _CACHE = json.loads(cache_file.read_text())
        return _CACHE
    if _CACHE is not None:
        return _CACHE
    hw = Path(cfg.simexp / ".." / "he-soc" / "hardware").resolve()
    if not hw.exists():
        hw = Path.home() / "he-soc" / "hardware"
    index: dict[str, list[str]] = {}
    roots = [cfg.overrides, hw]
    seen = set()
    for root in roots:
        for p in root.rglob("*"):
            if p.suffix not in (".sv", ".svh") or not p.is_file():
                continue
            rel = str(p.relative_to(root))
            if rel in seen:
                continue
            seen.add(rel)
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
    _CACHE = index
    try:
        cache_file.write_text(json.dumps(index))
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
