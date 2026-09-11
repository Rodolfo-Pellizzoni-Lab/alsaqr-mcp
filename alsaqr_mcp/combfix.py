"""Assign-once rewrite of always_comb blocks (the fix for xsim's transient-propagation rings).

xsim turns each intermediate write of a default-then-override `always_comb` into a net event; on a combinational
ready/valid ring the blocks re-trigger each other forever. The rewrite renames every write of a listed variable
inside an always_comb block to a shadow `<var>_xc` and assigns the real variable once at the end of the block.
Semantics are unchanged (the block's final value is what other processes see either way).
"""
import re

TYPE_RE = r"(?:logic|bit|reg|wire|[A-Za-z_][\w:]*_[te]|[A-Za-z_]\w*::[A-Za-z_]\w*)"


class FixError(Exception):
    pass


def _find_type(lines: list[str], name: str, ports: bool) -> tuple[str | None, int | None, bool]:
    """Return (type, declaration line index, is_port) for `name`."""
    port_re = re.compile(r"^\s*output\s+(.+?)\s+" + name + r"\s*[,)]?\s*(//.*)?$")
    for i, l in enumerate(lines):
        m = port_re.match(l)
        if m:
            return m.group(1).strip(), i, True
    decl_re = re.compile(r"^\s*(" + TYPE_RE + r"(?:\s*\[[^\]]*\])*)\s+((?:\w+\s*(?:\[[^\]]*\])?\s*,\s*)*)" + name + r"\s*(?:\[[^\]]*\])?\s*[;,]")
    for i, l in enumerate(lines):
        m = decl_re.match(l)
        if m:
            return m.group(1).strip(), i, False
    # continuation line of a multi-line declaration: look back for the type
    cont_re = re.compile(r"^\s*(?:\w+\s*,\s*)*" + name + r"\s*[;,]")
    for i, l in enumerate(lines):
        if cont_re.match(l) and "=" not in l and "always" not in l:
            for b in range(i - 1, max(-1, i - 6), -1):
                m = re.match(r"^\s*(" + TYPE_RE + r"(?:\s*\[[^\]]*\])*)\s+", lines[b])
                if m:
                    return m.group(1).strip(), b, False
    return None, None, False


def rewrite(text: str, names: list[str]) -> tuple[str, dict]:
    """Return (new text, report). report: blocks_rewritten, shadows, warnings."""
    L = text.split("\n")
    info = {}
    missing = []
    for n in names:
        typ, line, is_port = _find_type(L, n, True)
        if typ is None:
            missing.append(n)
        else:
            info[n] = (typ, line, is_port)
    if missing:
        raise FixError(f"declaration/type not found for: {', '.join(missing)} (multi-name declarations must be split first)")
    out, i, nblk, warnings = [], 0, 0, []
    touched_any = set()
    while i < len(L):
        l = L[i]
        if re.match(r"^\s*always_comb\s*(\(\*.*?\*\)\s*)?begin", l):
            depth, j = 0, i
            while True:
                depth += len(re.findall(r"\bbegin\b", L[j])) - len(re.findall(r"\bend\b", L[j]))
                if depth == 0:
                    break
                j += 1
                if j >= len(L):
                    raise FixError(f"unbalanced begin/end after line {i + 1}")
            body = "\n".join(L[i:j + 1])
            touched = {}
            for n in names:
                for m in re.finditer(r"\b" + n + r"\b(\[[^\]]*\])?(\.(\w+))?[.\w\[\]]*\s*(<=|=)(?!=)", body):
                    touched.setdefault(n, set()).add(m.group(3) if m.group(3) else "*")
            if touched:
                nblk += 1
                for n, mem in touched.items():
                    touched_any.add(n)
                    if "*" in mem:
                        body = re.sub(r"\b" + n + r"\b", n + "_xc", body)
                        tail = f"      {n} = {n}_xc;"
                    else:
                        for mname in mem:
                            body = re.sub(r"\b" + n + r"(\[[^\]]*\])?\." + mname + r"\b",
                                          lambda m: n + "_xc" + (m.group(1) or "") + "." + mname, body)
                        tail = "\n".join(f"      {n}.{mname} = {n}_xc.{mname};" for mname in sorted(mem))
                    head, sep, rest = body.rpartition("end")
                    body = head + tail + " // xsim: outputs assigned once\n    end" + rest
                if re.search(r"\bgenvar\b", "\n".join(L[:i])) and re.search(r"\bfor\s*\(\s*genvar", "\n".join(L[:i])):
                    warnings.append(f"block at line {i + 1} may sit inside a generate loop: shadows are module-level, "
                                    "check that generate-scoped variables are not shared between iterations")
            out.extend(body.split("\n"))
            i = j + 1
        else:
            out.append(l)
            i += 1
    untouched = [n for n in names if n not in touched_any]
    if untouched:
        warnings.append(f"not written in any always_comb: {', '.join(untouched)} (no shadow added)")
    # shadow declarations: ports after the port list, internal variables right after their own declaration
    ins = {}
    for n in names:
        if n not in touched_any:
            continue
        typ, line, is_port = info[n]
        # re-locate the declaration in the rewritten text (line numbers shifted)
        if is_port:
            k = next(k for k, l in enumerate(out) if re.match(r"^\s*output\s+.*\b" + n + r"\s*[,)]?\s*(//.*)?$", l))
            j = k
            while j > 0 and not re.match(r"^\s*module\b", out[j]):
                j -= 1
            while not re.match(r"^\s*\);\s*$", out[j]):
                j += 1
            at = j
        else:
            k = next(k for k, l in enumerate(out) if re.search(r"\b" + n + r"\b\s*(\[[^\]]*\])?\s*[;,]", l)
                     and "_xc" not in l and "=" not in l and "always" not in l)
            at = k
            while ";" not in out[at]:
                at += 1
        ins.setdefault(at, []).append(f"  {typ} {n}_xc; // xsim: shadow of {n}")
    for at in sorted(ins, reverse=True):
        out[at + 1:at + 1] = ins[at]
    shadows = [f"{n}_xc" for n in names if n in touched_any]
    return "\n".join(out), {"blocks_rewritten": nblk, "shadows": shadows, "warnings": warnings}


def audit(original: str, rewritten: str, names: list[str]) -> list[str]:
    """Flag tails on variables the original never writes and doubly-driven whole-struct tails."""
    problems = []
    for n in names:
        tails = re.findall(r"^\s*" + n + r"(?:\.\w+)? = " + n + r"_xc", rewritten, re.M)
        if tails and not re.search(r"\b" + n + r"\b\s*(\[[^\]]*\]\s*)*(\.\w+\s*)*(\[[^\]]*\]\s*)*=(?!=)", original):
            problems.append(f"{n}: tail assignment added but the original never writes it")
        whole = re.findall(r"^\s*" + n + r" = " + n + r"_xc;", rewritten, re.M)
        if len(whole) > 1:
            problems.append(f"{n}: whole-variable tail in {len(whole)} blocks (multiple drivers of the same variable)")
        if whole and re.search(r"^\s*assign\s+" + n + r"\.", original, re.M):
            problems.append(f"{n}: whole-struct tail but some members are driven by continuous assigns; "
                            "rewrite only the members the block writes")
    return problems
