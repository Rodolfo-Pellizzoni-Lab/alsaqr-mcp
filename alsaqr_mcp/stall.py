"""sim_stall_trace: locate a stall precisely and rank the processes that spin there (xsim ptrace).

Asynchronous: start with run_id (or snapshot+at_ns), poll with trace_id.
"""
import json
import os
import re
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import sources
from .config import Config, load
from .session import debug_snapshot_name, debug_snapshot_ready, _generics, _plusargs, _tb_opts
from .status import run_dir, load_state, _watchdog, _scan_stdout

BUSY_THRESHOLD = 3000  # executions of one process in the window; normal activity is a few hundred


def _err(error, fix, **extra):
    d = {"error": error, "fix": fix}
    d.update(extra)
    return d


def traces_root(cfg: Config) -> Path:
    return cfg.traces


def _start(cfg: Config, run_id: str | None, at_ns: int | None, window_ns: int, binary: str | None,
           snapshot: str | None = None) -> dict:
    if run_id:
        d = run_dir(cfg, run_id)
        if d is None:
            return _err("unknown run_id", "pass the run_id of a stalled sim_run")
        st = load_state(d)
        binary = st["binary"]
        if at_ns is None:
            wd = _watchdog(d / "watchdog.txt")
            at_ns = wd.get("stalled_at_ns")
            if at_ns is None:
                last_tick_ps, _m, _u, _t = _scan_stdout(d / "stdout.log", None)
                at_ns = last_tick_ps // 1000 if last_tick_ps else None
        if at_ns is None:
            return _err("no stall time", "the run has no TICK heartbeat yet; pass at_ns explicitly")
    if not binary or at_ns is None:
        return _err("missing arguments", "pass run_id (of a stalled run) or binary + at_ns")
    fp = cfg.flow_problem()
    if fp:
        return fp
    snap = snapshot or debug_snapshot_name(cfg)
    if snapshot and not debug_snapshot_ready(cfg, snapshot):
        return _err("unknown snapshot", f"no elaborated debug snapshot {snapshot} in {cfg.work}/xsim.dir")
    need_elab = not debug_snapshot_ready(cfg, snap)
    tid = "tr_" + datetime.now().strftime("%H%M%S")
    td = traces_root(cfg) / tid
    n = 1
    while td.exists():
        n += 1
        td = traces_root(cfg) / f"{tid}-{n}"
    td.mkdir(parents=True)
    state = {"trace_id": td.name, "run_id": run_id, "binary": binary, "snapshot": snap, "phase": "queued",
             "start_ns": int(at_ns), "window_ns": int(window_ns), "created_at": datetime.now().isoformat(timespec="seconds")}
    (td / "state.json").write_text(json.dumps(state, indent=1))
    env = {
        "TRACE_DIR": str(td), "WORK": str(cfg.work), "SNAP": snap, "NEED_ELAB": "1" if need_elab else "0",
        "TB_FILE": str(cfg.tb_file), "TB_OPTS": _tb_opts(cfg), "DPI_DIR": str(cfg.dpi_dir), "GENERICS": _generics(cfg),
        "PLUSARGS": _plusargs(cfg, Path(binary)), "START_NS": str(int(at_ns)), "STEP_NS": "500", "MAX_STEPS": "40",
        "WINDOW_NS": str(int(window_ns)), "VIVADO_SETTINGS": cfg.vivado_settings,
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
    }
    runner = td / "runner.sh"
    runner.write_text((cfg.runner.parent / "stall_runner.sh").read_text())  # private copy
    with open(td / "runner.out", "w") as out:
        proc = subprocess.Popen(["setsid", "bash", str(runner)], cwd=str(cfg.work), env=env, stdout=out,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    state["runner_pid"] = proc.pid
    (td / "state.json").write_text(json.dumps(state, indent=1))
    return {"trace_id": td.name, "state": "queued", "start_ns": int(at_ns), "snapshot": snap,
            "elab": "rebuilding" if need_elab else "reused",
            "eta": "≈5 min elaboration (first time) + ≈1 min per simulated ms to reach the stall + ≈3 min tracing",
            "next": f"sim_stall_trace(trace_id='{td.name}') to get the result"}


def _block_writes(path: Path, line: int) -> dict:
    """Find the always_comb block containing `line` and the variables it writes."""
    try:
        L = path.read_text(errors="replace").split("\n")
    except OSError:
        return {}
    i = min(line - 1, len(L) - 1)
    start = None
    for k in range(i, -1, -1):
        if re.match(r"^\s*always_comb\b", L[k]):
            start = k
            break
        if re.match(r"^\s*(always_ff|always_latch|module|endmodule)\b", L[k]):
            break
    if start is None:
        return {}
    depth, j = 0, start
    while j < len(L):
        depth += len(re.findall(r"\bbegin\b", L[j])) - len(re.findall(r"\bend\b", L[j]))
        if depth == 0 and j > start:
            break
        j += 1
    body = "\n".join(L[start:j + 1])
    writes = re.findall(r"^\s*([A-Za-z_]\w*)(?:\[[^\]]*\])*(?:\.\w+)*(?:\[[^\]]*\])*\s*=(?!=)", body, re.M)
    seen, vars_ = set(), []
    for w in writes:
        if w not in seen and w not in ("automatic", "int", "logic"):
            seen.add(w)
            vars_.append(w)
    return {"block_line": start + 1, "block_end": j + 1, "variables_written": vars_}


_SRC_LINE_RE = re.compile(r"^INFO: (/\S+\.(?:sv|svh|v|vh|vhd|vhdl|vp)):\d+\s*$")


def _analyse(cfg: Config, td: Path, st: dict) -> dict:
    """ptrace prints, per executed process, its instance path followed by its source file:line."""
    log = td / "ptrace.log"
    if not log.exists():
        return {}
    procs, proc_line = Counter(), {}
    total, pending = 0, None
    with open(log, "rb") as f:
        for raw in f:
            if not raw.startswith(b"INFO: /"):
                continue
            l = raw.decode("utf-8", "replace").rstrip()
            if _SRC_LINE_RE.match(l):  # a source location (any directory), not an instance path
                if pending is not None:
                    proc_line.setdefault(pending, l[6:])
                    pending = None
                continue
            total += 1
            m = re.match(r"^INFO: /[^/]+/(.*)$", l)  # strip the escaped top scope
            pending = m.group(1) if m else l[6:]
            procs[pending] += 1
    top = procs.most_common(12)
    cycles = max(1, int(st.get("window_ns", 700)) // 10)  # SoC clock is ~10 ns: a process runs at most a few times per cycle
    kind = ("busy_loop" if top and top[0][1] >= BUSY_THRESHOLD and top[0][1] > 20 * cycles
            else ("idle" if total else "unknown"))
    def split_fl(fl):
        f, _, ln = fl.rpartition(":")
        return f, (int(ln) if ln.isdigit() else None)
    out = {"kind": kind, "events_in_window": total,
           "kinds": {"busy_loop": "a few processes execute over and over without simulated time advancing",
                     "idle": "almost nothing executes: the design is waiting for a signal that never comes"},
           "top_processes": [{"instance": p, "count": c, "file": split_fl(proc_line.get(p, ""))[0],
                              "line": split_fl(proc_line.get(p, ""))[1]} for p, c in top]}
    if kind == "busy_loop":
        f, ln = split_fl(proc_line.get(top[0][0], ""))
        blk = _block_writes(Path(f), ln) if f and ln else {}
        rel = sources.rel(cfg, f)
        out["busy_block"] = {
            "instance": top[0][0], "file": f, "repo_relative": rel, **blk,
            "also_busy": [{"instance": p, "file": split_fl(proc_line.get(p, ""))[0], "line": split_fl(proc_line.get(p, ""))[1]}
                          for p, c in top[1:4] if c > 20 * cycles],
            "explanation": "these processes keep re-triggering each other inside one simulation time step (the value "
                           "one writes wakes the other, which writes back). Look at how the listed variables are assigned "
                           "in this block and the blocks that read them; edit the file, then rtl_recompile it.",
        }
    elif kind == "idle":
        out["next"] = ("nothing is executing, so a handshake or interrupt never arrives. Open a sim_session, advance to "
                       "stall_ns and probe the request/response signals along the path the last activity was on "
                       "(sim_uart and sim_status markers show how far the program got).")
    return out


def _status(cfg: Config, trace_id: str) -> dict:
    td = traces_root(cfg) / trace_id
    if not (td / "state.json").exists():
        return _err("unknown trace_id", "use the trace_id returned when the trace was started")
    st = json.loads((td / "state.json").read_text())
    out = {"trace_id": trace_id, "state": st.get("phase"), "start_ns": st.get("start_ns"),
           "stall_ns": st.get("stall_ns"), "run_id": st.get("run_id")}
    if st.get("phase") == "done":
        res = td / "result.json"
        if res.exists():
            out.update(json.loads(res.read_text()))
        else:
            out.update(_analyse(cfg, td, st))
            (td / "result.json").write_text(json.dumps(out, indent=1))
        out["ptrace_log"] = str(td / "ptrace.log")
    elif st.get("phase") == "no_stall":
        out["note"] = (f"no stall reproduced between {st.get('start_ns')} and {st.get('last_ns')} ns "
                       "(the design advanced through the whole window); pass a later at_ns or check the run's "
                       "watchdog for the real stall time")
    elif st.get("phase") in ("elab_failed", "failed"):
        out["errors"] = (td / "errors.txt").read_text().splitlines()[:5] if (td / "errors.txt").exists() else []
    else:
        out["note"] = {"queued": "starting", "elab": "elaborating the debug snapshot (~5 min)",
                       "locate": "stepping to the exact stall time", "trace": "ptrace window running"}.get(st.get("phase"), "")
    return out


def sim_stall_trace(run_id: str | None = None, trace_id: str | None = None, at_ns: int | None = None,
                    window_ns: int = 700, binary: str | None = None, snapshot: str | None = None) -> dict:
    cfg = load()
    if trace_id:
        return _status(cfg, trace_id)
    return _start(cfg, run_id, at_ns, int(window_ns or 700), binary, snapshot)
