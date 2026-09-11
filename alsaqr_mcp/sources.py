"""The repository files the xsim library is compiled from, and which of them changed after they were compiled.

A file is in the library as of the last full build (hardware/xsim/build.sh, marked by work/compile.log) or of its
last rtl_recompile (recorded in state/compiled.json). The testbench top (tb_file) is not tracked here: every
elaboration recompiles it.
"""
import json
import re
import time
from pathlib import Path

from .config import Config

_SRC_RE = re.compile(r"\.(sv|svh|v|vhd|vhdl)$")
_CACHE: dict = {}


def compiled_files(cfg: Config) -> list[Path]:
    """Every source file named in work/compile.sh (absolute paths), in compile order."""
    cs = cfg.work / "compile.sh"
    key = (str(cs), cs.stat().st_mtime_ns if cs.exists() else 0)
    if _CACHE.get("files_key") != key:
        files, seen = [], set()
        if cs.exists():
            for line in cs.read_text().splitlines():
                if not line.startswith(("xvlog", "xvhdl")):
                    continue
                for tok in line.split():
                    if _SRC_RE.search(tok) and tok not in seen:
                        seen.add(tok)
                        files.append(Path(tok))
        _CACHE["files_key"], _CACHE["files"] = key, files
    return _CACHE["files"]


def include_dirs(cfg: Config) -> list[Path]:
    cs = cfg.work / "compile.sh"
    dirs = set()
    if cs.exists():
        for m in re.finditer(r"-i (\S+)", cs.read_text()):
            dirs.add(m.group(1))
    return [Path(d) for d in sorted(dirs)]


def design_files(cfg: Config) -> list[Path]:
    """Compiled sources plus the headers in their include directories (what `include can pull in)."""
    files = [f for f in compiled_files(cfg) if f.exists()]
    for d in include_dirs(cfg):
        files += sorted(d.glob("*.svh")) + sorted(d.glob("*/*.svh")) + sorted(d.glob("*/*/*.svh"))
    return list(dict.fromkeys(files))


def rel(cfg: Config, path: Path | str) -> str:
    """Path below hardware/ when possible, else the path as given."""
    p = str(path)
    h = str(cfg.hardware) + "/"
    return p[len(h):] if p.startswith(h) else p


def fingerprint(cfg: Config) -> str:
    """Changes whenever a compiled source file (or the tb) is edited: count + newest mtime."""
    newest, count = 0, 0
    for f in compiled_files(cfg) + [cfg.tb_file]:
        try:
            newest = max(newest, f.stat().st_mtime_ns)
            count += 1
        except OSError:
            pass
    return f"{count}:{newest}"


# ---------------------------------------------------------------- compile records / staleness
def _records_file(cfg: Config) -> Path:
    return cfg.state / "compiled.json"


def compile_records(cfg: Config) -> dict:
    """{absolute source path: time_ns it was recompiled} since the last full build."""
    try:
        recs = json.loads(_records_file(cfg).read_text())
    except (OSError, ValueError):
        return {}
    base = baseline_ns(cfg)
    return {p: t for p, t in recs.items() if int(t) > base}  # a full build supersedes older recompiles


def record_compile(cfg: Config, path: Path, started_ns: int):
    """Remember that `path` went into the library at started_ns (the time xvlog was started)."""
    recs = compile_records(cfg)
    recs[str(path)] = started_ns
    cfg.state.mkdir(parents=True, exist_ok=True)
    _records_file(cfg).write_text(json.dumps(recs, indent=1))


def baseline_ns(cfg: Config) -> int:
    """End of the last full build (compile.log is written until the last block finishes)."""
    log = cfg.work / "compile.log"
    return log.stat().st_mtime_ns if log.exists() else 0


def is_stale(cfg: Config, path: Path, recs: dict | None = None, base: int | None = None) -> bool:
    recs = compile_records(cfg) if recs is None else recs
    base = baseline_ns(cfg) if base is None else base
    try:
        mt = path.stat().st_mtime_ns
    except OSError:
        return False
    return mt > max(base, int(recs.get(str(path), 0)))


def stale_files(cfg: Config) -> list[str]:
    """Compiled files edited after they went into the library (repo-relative), tb top excluded."""
    recs, base = compile_records(cfg), baseline_ns(cfg)
    tb = cfg.tb_file.resolve()
    out = []
    for f in compiled_files(cfg):
        if f.resolve() != tb and is_stale(cfg, f, recs, base):
            out.append(rel(cfg, f))
    return out


def now_ns() -> int:
    return time.time_ns()
