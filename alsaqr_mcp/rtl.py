"""rtl_changes, rtl_recompile: see what was edited in the RTL/testbench (vs the git HEAD of the he-soc checkout)
and compile edited files into the simulator library."""
import os
import re
import subprocess
from pathlib import Path

from . import sources
from .config import Config, load

MAX_DIFF_LINES = 120
_SRC_RE = re.compile(r"\.(sv|svh|v|vh|vhd|vhdl)$")


def _err(error, fix, **extra):
    d = {"error": error, "fix": fix}
    d.update(extra)
    return d


# ---------------------------------------------------------------- paths
def _resolve(cfg: Config, path: str) -> Path | None:
    """Absolute path of a file given as absolute, 'hardware/...' or relative to hardware/."""
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        s = str(p)
        p = cfg.hardware / (s[len("hardware/"):] if s.startswith("hardware/") else s)
    p = p.resolve()
    try:
        p.relative_to(cfg.hardware.resolve())
    except ValueError:
        return None
    return p


def _git(cfg: Config, *args, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cfg.repo), *args], capture_output=True, text=True, timeout=120, check=check)


# ---------------------------------------------------------------- rtl_changes
def _changed(cfg: Config) -> list[tuple[str, Path]]:
    """(status, absolute path) of every HDL file under hardware/ that differs from HEAD. Untracked files count only
    when the build compiles them (the tree also holds untracked tool output, e.g. Vivado IP sources under fpga/)."""
    r = _git(cfg, "status", "--porcelain=v1", "-uall", "--", "hardware")
    compiled = {str(f) for f in sources.compiled_files(cfg)}
    out = []
    for l in r.stdout.splitlines():
        code, name = l[:2], l[3:].strip().strip('"')
        if " -> " in name:  # rename: keep the new name
            name = name.split(" -> ", 1)[1]
        if not _SRC_RE.search(name):
            continue
        p = cfg.repo / name
        if code == "??" and str(p) not in compiled:
            continue
        status = "new" if code == "??" or "A" in code else ("deleted" if "D" in code else "modified")
        out.append((status, p))
    return out


def _diff(cfg: Config, p: Path, untracked: bool) -> dict:
    if untracked:
        r = _git(cfg, "diff", "--no-index", "-U2", "--", "/dev/null", str(p))
    else:
        r = _git(cfg, "diff", "-U2", "HEAD", "--", str(p))
    d = [l for l in r.stdout.splitlines() if not l.startswith(("diff --git", "index ", "new file mode"))]
    added = sum(1 for l in d if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in d if l.startswith("-") and not l.startswith("---"))
    return {"added": added, "removed": removed, "lines": d[:MAX_DIFF_LINES], "truncated": len(d) > MAX_DIFF_LINES}


def rtl_changes(op: str, path: str | None = None, filter: str | None = None) -> dict:
    cfg = load()
    op = (op or "").lower()
    if not (cfg.repo / ".git").exists():
        return _err("not a git checkout", f"{cfg.repo} has no .git")
    if op == "list":
        recs, base = sources.compile_records(cfg), sources.baseline_ns(cfg)
        compiled = {str(f) for f in sources.compiled_files(cfg)}
        tb = str(cfg.tb_file.resolve())
        entries = []
        for status, p in _changed(cfg):
            r = sources.rel(cfg, p)
            if filter and filter not in r:
                continue
            e = {"path": r, "status": status}
            if str(p) == tb:
                e["in_library"] = "recompiled at every elaboration"
            elif str(p) in compiled:
                e["in_library"] = not sources.is_stale(cfg, p, recs, base)
            else:
                e["in_library"] = "not part of the build"
            entries.append(e)
        # back to the HEAD content (e.g. after revert) but the library still holds the edited version
        listed = {e["path"] for e in entries}
        for r in sources.stale_files(cfg):
            if r not in listed and (not filter or filter in r):
                entries.append({"path": r, "status": "same as HEAD", "in_library": False})
        out = {"count": len(entries), "changes": entries[:200],
               "note": "differences from the git HEAD of the he-soc checkout; in_library=false: rtl_recompile it"}
        return out
    if not path:
        return _err("missing path", "pass path (relative to hardware/, or absolute)")
    p = _resolve(cfg, path)
    if p is None:
        return _err("not a hardware file", f"{path} is not under {cfg.hardware}")
    rel = sources.rel(cfg, p)
    tracked = _git(cfg, "ls-files", "--error-unmatch", "--", str(p)).returncode == 0
    if op == "diff":
        if not p.exists() and not tracked:
            return _err("no such file", str(p))
        return {"path": rel, **_diff(cfg, p, untracked=not tracked)}
    if op == "revert":
        if not tracked:
            if not p.exists():
                return _err("no such file", str(p))
            return _err("not tracked by git", f"{rel} is a new file; delete it instead of reverting")
        r = _git(cfg, "checkout", "HEAD", "--", str(p))
        if r.returncode != 0:
            return _err("revert failed", r.stderr.strip()[:300])
        note = ("rtl_recompile(files=[path]) to put the original back into the library"
                if p in sources.compiled_files(cfg) and p != cfg.tb_file.resolve() else None)
        return {"path": rel, "result": "restored to the git HEAD version", "note": note}
    return _err("unknown op", "ops: list, diff, revert")


# ---------------------------------------------------------------- rtl_recompile
_ERR_RE = re.compile(r"^ERROR: \[[^\]]+\] (.*?) \[([^:\]]+):(\d+)\]\s*$")


def _compile_line_for(cfg: Config, src: Path) -> str | None:
    for l in (cfg.work / "compile.sh").read_text().splitlines():
        if re.match(r"^(xvlog|xvhdl) .*[ ]" + re.escape(str(src)) + r"( |$)", l):
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
    fp = cfg.flow_problem()
    if fp:
        return fp
    results, errors = [], []
    env = dict(os.environ)
    logdir = cfg.state / "recompile"
    logdir.mkdir(parents=True, exist_ok=True)
    for f in files:
        src = _resolve(cfg, f)
        if src is None or not src.exists():
            errors.append({"file": f, "line": None, "msg": f"no such file under {cfg.hardware}"})
            continue
        rel = sources.rel(cfg, src)
        line = _compile_line_for(cfg, src)
        if line is None:
            errors.append({"file": rel, "line": None, "msg": "not part of the build (no compile block in compile.sh)"})
            continue
        tool, opts = _options(line)
        for d in extra_defines or []:
            opts += ["-d", d]
        started = sources.now_ns()
        r = subprocess.run(["bash", "-c", f'source "{cfg.vivado_settings}" >/dev/null 2>&1 && exec "$@"', "_", tool, *opts, str(src)],
                           cwd=str(cfg.work), capture_output=True, text=True, env=env, timeout=900)
        log = logdir / f"{src.name}.log"
        log.write_text(r.stdout + r.stderr)
        file_errors = []
        for l in (r.stdout + r.stderr).splitlines():
            m = _ERR_RE.match(l)
            if m:
                file_errors.append({"file": sources.rel(cfg, m.group(2)), "line": int(m.group(3)), "msg": m.group(1)[:200]})
            elif l.startswith("ERROR"):
                file_errors.append({"file": rel, "line": None, "msg": l[:200]})
        if r.returncode != 0 or file_errors:
            errors += file_errors or [{"file": rel, "line": None, "msg": f"{tool} exited {r.returncode}"}]
            results.append({"file": rel, "ok": False, "log": str(log)})
        else:
            sources.record_compile(cfg, src, started)
            results.append({"file": rel, "ok": True, "log": str(log)})
    ok = [r for r in results if r["ok"]]
    out = {"compiled": ok, "errors": errors[:20]}
    if ok:
        out["note"] = "the library changed: the next sim_run / sim_session / sim_stall_trace elaborates a new snapshot"
    if errors:
        out["fix"] = "fix the reported lines and recompile"
    return out
