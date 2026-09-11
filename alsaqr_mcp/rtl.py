"""Category B: rtl_override, rtl_recompile, rtl_fix_ring — edit RTL as scratch overrides and recompile them."""
import difflib
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import combfix
from .config import Config, load

MAX_DIFF_LINES = 120


def _err(error, fix, **extra):
    d = {"error": error, "fix": fix}
    d.update(extra)
    return d


# ---------------------------------------------------------------- paths
def _rel(cfg: Config, path: str) -> str | None:
    """Repo-relative path (below hardware/) for a repo path, an override path or a relative path."""
    p = Path(os.path.expanduser(path))
    for root in (cfg.hardware, cfg.overrides):
        try:
            if p.is_absolute():
                return str(p.resolve().relative_to(root.resolve()))
        except ValueError:
            continue
    s = str(p)
    if s.startswith("hardware/"):
        s = s[len("hardware/"):]
    return s if (cfg.hardware / s).exists() or (cfg.overrides / s).exists() else None


def _map_entries(cfg: Config) -> list[tuple[str, str]]:
    if not cfg.override_map.exists():
        return []
    out = []
    for l in cfg.override_map.read_text().splitlines():
        parts = l.split()
        if len(parts) == 2:
            out.append((parts[0], parts[1]))
    return out


def _map_add(cfg: Config, rel: str):
    repo, ov = str(cfg.hardware / rel), str(cfg.overrides / rel)
    if any(r == repo for r, _ in _map_entries(cfg)):
        return
    with open(cfg.override_map, "a") as f:
        f.write(f"{repo} {ov}\n")


def _map_remove(cfg: Config, rel: str):
    repo = str(cfg.hardware / rel)
    keep = [f"{r} {o}" for r, o in _map_entries(cfg) if r != repo]
    cfg.override_map.write_text("\n".join(keep) + ("\n" if keep else ""))


def _diff(a: Path, b: Path, label_a: str, label_b: str) -> dict:
    ta = a.read_text(errors="replace").splitlines() if a.exists() else []
    tb = b.read_text(errors="replace").splitlines() if b.exists() else []
    d = list(difflib.unified_diff(ta, tb, label_a, label_b, lineterm="", n=2))
    added = sum(1 for l in d if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in d if l.startswith("-") and not l.startswith("---"))
    return {"added": added, "removed": removed, "lines": d[:MAX_DIFF_LINES], "truncated": len(d) > MAX_DIFF_LINES}


# ---------------------------------------------------------------- rtl_override
def rtl_override(op: str, path: str | None = None, filter: str | None = None) -> dict:
    cfg = load()
    op = (op or "").lower()
    if op == "list":
        entries = []
        for repo, ov in _map_entries(cfg):
            rel = repo.replace(str(cfg.hardware) + "/", "")
            if filter and filter not in rel:
                continue
            entries.append({"path": rel, "override_exists": Path(ov).exists(),
                            "differs": Path(ov).exists() and Path(repo).exists() and
                            Path(ov).read_bytes() != Path(repo).read_bytes()})
        return {"count": len(entries), "overrides": entries[:200], "map": str(cfg.override_map)}
    if not path:
        return _err("missing path", "pass path (repo-relative below hardware/, or absolute)")
    rel = _rel(cfg, path)
    if rel is None:
        return _err("unknown file", f"{path} is neither under {cfg.hardware} nor {cfg.overrides}")
    repo, ov = cfg.hardware / rel, cfg.overrides / rel
    if op == "create":
        if not repo.exists():
            return _err("no such repo file", str(repo))
        existed = ov.exists()
        if not existed:
            ov.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo, ov)
        _map_add(cfg, rel)
        return {"path": rel, "override_path": str(ov), "created": not existed,
                "note": "edit the override, then rtl_recompile(files=[path]); the next sim_run re-elaborates"}
    if op == "diff":
        if not ov.exists():
            return _err("no override", f"{rel} has no override; rtl_override(op='create') first")
        return {"path": rel, **_diff(repo, ov, f"repo/{rel}", f"override/{rel}")}
    if op == "show":
        target = ov if ov.exists() else repo
        return {"path": rel, "source": "override" if ov.exists() else "repo", "file": str(target),
                "lines": target.read_text(errors="replace").count("\n")}
    if op == "revert":
        if not ov.exists():
            return _err("no override", f"{rel} has no override")
        shutil.copyfile(repo, ov)
        return {"path": rel, "result": "override reset to the repo content",
                "note": "rtl_recompile(files=[path]) to put the original back into the library"}
    if op == "remove":
        if ov.exists():
            ov.unlink()
        _map_remove(cfg, rel)
        return {"path": rel, "result": "override removed", "note": "rtl_recompile the repo file to update the library"}
    return _err("unknown op", "ops: create, diff, show, revert, remove, list")


# ---------------------------------------------------------------- rtl_recompile
_ERR_RE = re.compile(r"^ERROR: \[[^\]]+\] (.*?) \[([^:\]]+):(\d+)\]\s*$")


def _compile_line_for(cfg: Config, basename: str) -> str | None:
    for l in (cfg.work / "compile.sh").read_text().splitlines():
        if re.match(r"^(xvlog|xvhdl) .*[ /]" + re.escape(basename) + r"( |$)", l):
            return l
    return None


def _options(line: str) -> tuple[str, list[str]]:
    """(tool, options without the source files) of a compile.sh line."""
    toks = line.split()
    tool = toks[0]
    opts = []
    for tok in toks[1:]:
        if tok in (">", ">>", "2>", "2>&1", "||", "&&", ";", "|") or tok.startswith((">", "2>")):
            break  # shell redirections / chaining written by the compile-script generator
        if re.search(r"\.(sv|svh|v|vhd|vhdl)$", tok):
            continue
        opts.append(tok)
    return tool, opts


def rtl_recompile(files: list[str], extra_defines: list[str] | None = None) -> dict:
    cfg = load()
    if not files:
        return _err("no files", "pass files: [path, ...]")
    results, errors = [], []
    env = dict(os.environ)
    for f in files:
        rel = _rel(cfg, f)
        if rel is None:
            errors.append({"file": f, "line": None, "msg": "not a repo/override file"})
            continue
        src = cfg.overrides / rel if (cfg.overrides / rel).exists() else cfg.hardware / rel
        line = _compile_line_for(cfg, Path(rel).name)
        if line is None:
            errors.append({"file": rel, "line": None, "msg": "no compile block for this file in compile.sh"})
            continue
        tool, opts = _options(line)
        # drop obsolete "-d XSIM_FORCE_RESET" pairs from the generated options
        cleaned, skip = [], False
        for k, o in enumerate(opts):
            if skip:
                skip = False
                continue
            if o == "-d" and k + 1 < len(opts) and opts[k + 1] == "XSIM_FORCE_RESET":
                skip = True
                continue
            cleaned.append(o)
        opts = cleaned
        for d in cfg.recompile_defines.get(rel, []) + list(extra_defines or []):
            opts += ["-d", d]
        r = subprocess.run(["bash", "-c", f'source "{cfg.vivado_settings}" >/dev/null 2>&1 && exec "$@"', "_", tool, *opts, str(src)],
                           cwd=str(cfg.work), capture_output=True, text=True, env=env, timeout=900)
        log = cfg.work / f"recompile_{Path(rel).name}.log"
        log.write_text(r.stdout + r.stderr)
        file_errors = []
        for l in (r.stdout + r.stderr).splitlines():
            m = _ERR_RE.match(l)
            if m:
                file_errors.append({"file": m.group(2).replace(str(cfg.overrides) + "/", ""), "line": int(m.group(3)), "msg": m.group(1)[:200]})
            elif l.startswith("ERROR"):
                file_errors.append({"file": rel, "line": None, "msg": l[:200]})
        if r.returncode != 0 or file_errors:
            errors += file_errors or [{"file": rel, "line": None, "msg": f"{tool} exited {r.returncode}"}]
            results.append({"file": rel, "source": "override" if src == cfg.overrides / rel else "repo", "ok": False, "log": str(log)})
        else:
            results.append({"file": rel, "source": "override" if src == cfg.overrides / rel else "repo", "ok": True, "log": str(log)})
    ok = [r for r in results if r["ok"]]
    out = {"compiled": ok, "errors": errors[:20]}
    if ok:
        out["note"] = "the library changed: the next sim_run / sim_session / sim_stall_trace elaborates a new snapshot"
    if errors:
        out["fix"] = "fix the reported lines in the override and recompile"
    return out


# ---------------------------------------------------------------- rtl_fix_ring
def rtl_fix_ring(file: str, vars: list[str], dry_run: bool = False) -> dict:
    cfg = load()
    if not vars:
        return _err("no vars", "pass vars: the variables the always_comb block writes (from sim_stall_trace.suggestion)")
    rel = _rel(cfg, file)
    if rel is None:
        return _err("unknown file", f"{file} is neither under {cfg.hardware} nor {cfg.overrides}")
    ov = cfg.overrides / rel
    created = False
    if not ov.exists():
        ov.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cfg.hardware / rel, ov)
        _map_add(cfg, rel)
        created = True
    original = ov.read_text(errors="replace")
    try:
        new, report = combfix.rewrite(original, list(vars))
    except combfix.FixError as e:
        return _err("rewrite failed", str(e), file=rel)
    problems = combfix.audit(original, new, list(vars))
    d = list(difflib.unified_diff(original.splitlines(), new.splitlines(), f"before/{rel}", f"after/{rel}", lineterm="", n=1))
    out = {"file": rel, "override_path": str(ov), "override_created": created, "dry_run": bool(dry_run),
           **report, "audit": problems, "diff": d[:MAX_DIFF_LINES], "diff_truncated": len(d) > MAX_DIFF_LINES}
    if problems:
        out["warning"] = "audit found problems: review the diff before recompiling"
    if not dry_run:
        ov.write_text(new)
        out["next"] = f"rtl_recompile(files=['{rel}']) then sim_run again"
    return out
