"""sim_status, sim_uart, sim_kill: observe and stop runs started by sim_run."""
import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .config import Config, load

_TICK_RE = re.compile(r"^TICK (\d+)")
_XSIM_T_RE = re.compile(r"t=(\d+)")
_UART_RE = re.compile(r"^(?:(\d\d:\d\d:\d\d|--:--:--) )?Mock uart\s+(\d+)(?: t=(\d+))?:\s?(.*)$")
_MARKERS = (
    ("success", re.compile(r"\[JTAG\] SUCCESS")),
    ("fail", re.compile(r"\[JTAG\] FAILED|FAILED: return code")),
    ("fatal", re.compile(r"Fatal|\$fatal|Simulation engine not responding|terminated in an unexpected")),
    ("finish", re.compile(r"\$finish called")),
    ("jtag", re.compile(r"^\[JTAG\]")),
    ("preload", re.compile(r"^\[XSIM-L[23]\]")),
    ("error", re.compile(r"^(ERROR|Error):|^\*\* Error")),
)
MAX_MARKERS = 40
MAX_WAIT_S = 900  # Claude Code's MCP tool-call timeout is far longer (MCP_TOOL_TIMEOUT)
CONSOLE_TAIL = 20
_ACTIVE = ("queued", "elab", "running")


def _err(error, fix, **extra):
    d = {"error": error, "fix": fix}
    d.update(extra)
    return d


def run_dir(cfg: Config, run_id: str) -> Path | None:
    d = cfg.runs / run_id
    return d if (d / "run.json").exists() else None


def load_state(d: Path) -> dict:
    return json.loads((d / "run.json").read_text())


def _parse_time(s: str) -> str | None:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _scan_stdout(path: Path, since_ns: int | None):
    """Walk the xsim stdout once: last TICK (ps), markers with approximate sim time, uart line count."""
    last_tick_ps = None
    markers, uart_lines = [], 0
    if not path.exists():
        return None, markers, 0, False
    with open(path, "rb") as f:
        for raw in f:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            m = _TICK_RE.match(line)
            if m:
                last_tick_ps = int(m.group(1))
                continue
            if line.startswith("Mock uart"):
                uart_lines += 1
                continue
            for kind, rx in _MARKERS:
                if rx.search(line):
                    mt = _XSIM_T_RE.search(line)
                    t_ns = int(mt.group(1)) // 1000 if mt else (last_tick_ps // 1000 if last_tick_ps is not None else None)
                    approx = mt is None
                    if since_ns is not None and t_ns is not None and t_ns <= since_ns:
                        break
                    markers.append({"t_ns": t_ns, "approx": approx, "kind": kind, "text": line.strip()[:160]})
                    break
    truncated = len(markers) > MAX_MARKERS
    if truncated:
        markers = markers[-MAX_MARKERS:]
    return last_tick_ps, markers, uart_lines, truncated


def _watchdog(path: Path):
    """Last watchdog line -> (rss_mb, idle_s, stalled_at_ps, rss_first, rss_last, ticks_per_min)."""
    if not path.exists():
        return {}
    lines = path.read_text().splitlines()
    wd = [l for l in lines if l.startswith("WD ")]
    stalled = next((l for l in lines if l.startswith("STALLED")), None)
    info = {}
    if wd:
        def field(l, k):
            m = re.search(k + r"=(\d+)", l)
            return int(m.group(1)) if m else None
        first, last = wd[0], wd[-1]
        info["rss_mb"] = field(last, "rss")
        info["idle_s"] = field(last, "idle")
        rf, rl = field(first, "rss"), field(last, "rss")
        info["rss_growing"] = bool(rf and rl and rl > rf * 1.5 and rl - rf > 500)
        if len(wd) >= 2:
            t0, t1 = field(wd[-2], "tick"), field(last, "tick")
            if t0 is not None and t1 is not None and t1 >= t0:
                info["sim_ns_per_wall_min"] = int((t1 - t0) / 1000 * 2)  # watchdog samples every 30 s
    if stalled:
        m = re.search(r"STALLED at (\d+)", stalled)
        info["stalled_at_ns"] = int(m.group(1)) // 1000 if m else None
    return info


def _wait(d: Path, wait_s: int, until: str | None, until_ns: int | None) -> dict:
    """Block until the run ends, `until` appears in a marker or console line, simulated time reaches `until_ns`,
    or wait_s elapses. Returns {waited_s, stopped_because}."""
    t0 = time.time()
    while True:
        if load_state(d).get("phase") not in _ACTIVE:
            why = "run ended"
            break
        last_tick_ps, markers, _, _ = _scan_stdout(d / "stdout.log", None)
        if until and (any(until in m["text"] for m in markers)
                      or any(until in l["text"] for l in _read_uart(d / "uart.txt"))):
            why = f"found '{until}'"
            break
        if until_ns is not None and last_tick_ps is not None and last_tick_ps // 1000 >= until_ns:
            why = f"simulated time reached {until_ns} ns"
            break
        if time.time() - t0 >= wait_s:
            why = f"wait_s ({wait_s} s) elapsed, run still going: call again to keep waiting"
            break
        time.sleep(3)
    return {"waited_s": int(time.time() - t0), "stopped_because": why}


def sim_status(run_id: str, since_ns: int | None = None, wait_s: int = 0, until: str | None = None,
               until_ns: int | None = None) -> dict:
    cfg = load()
    d = run_dir(cfg, run_id)
    if d is None:
        known = sorted(p.name for p in cfg.runs.iterdir()) if cfg.runs.exists() else []
        return _err("unknown run_id", f"known runs: {', '.join(known[-10:]) or 'none'}")
    wait_s = max(0, min(int(wait_s or 0), MAX_WAIT_S))
    waited = _wait(d, wait_s, until, None if until_ns is None else int(until_ns)) if wait_s else None
    st = load_state(d)
    phase = st.get("phase")
    last_tick_ps, markers, uart_lines, truncated = _scan_stdout(d / "stdout.log", since_ns)
    wd = _watchdog(d / "watchdog.txt")
    start = _parse_time(st.get("sim_started_at", "")) if st.get("sim_started_at") else None
    end = _parse_time(st.get("sim_finished_at", "")) if st.get("sim_finished_at") else None
    now = datetime.now(start.tzinfo) if start else None
    wall_s = int(((end or now) - start).total_seconds()) if start else None
    state = {"queued": "queued", "elab": "elaborating", "running": "running", "finished": "finished",
             "stalled": "stalled", "timeout": "timeout", "killed": "killed", "elab_failed": "elab_failed"}.get(phase, phase)
    out = {
        "run_id": run_id, "state": state, "elab": st.get("elab"), "snapshot": st.get("snapshot"),
        "binary": os.path.basename(st.get("binary", "")),
        "sim_time_ns": last_tick_ps // 1000 if last_tick_ps is not None else 0,
        "wall_s": wall_s, "rss_mb": wd.get("rss_mb"), "sim_ns_per_wall_min": wd.get("sim_ns_per_wall_min"),
        "markers": markers, "markers_truncated": truncated, "uart_lines": uart_lines,
    }
    if waited:
        out["wait"] = waited
    if uart_lines and (waited or state not in ("queued", "elaborating", "running")):
        # a wait or a finished run: hand back the console too, so one call tells the whole story
        lines = _read_uart(d / "uart.txt")
        out["console"] = [{"n": l["n"], "t_ns": l["t_ns"], "text": l["text"]} for l in lines[-CONSOLE_TAIL:]]
        if len(lines) > CONSOLE_TAIL:
            out["console_note"] = (f"last {CONSOLE_TAIL} of {len(lines)} lines; "
                                   f"sim_uart(run_id='{run_id}', since_line=0) for all of them")
    elif uart_lines:
        out["uart_hint"] = f"sim_uart(run_id='{run_id}') for the {uart_lines} console lines"
    if state == "stalled" or wd.get("stalled_at_ns") is not None:
        out["stall"] = {"at_ns": wd.get("stalled_at_ns"), "memory_growing": wd.get("rss_growing", False),
                        "meaning": "simulated time stopped advancing; the run was killed by the watchdog",
                        "next": f"sim_stall_trace(run_id='{run_id}') to see what the simulator was executing"}
    if state in ("finished", "timeout", "killed", "elab_failed"):
        rc = st.get("rc")
        verdict = "success" if any(m["kind"] == "success" for m in markers) else (
            "fail" if any(m["kind"] in ("fail", "fatal") for m in markers) else state)
        code = 0 if verdict == "success" else None
        for m in markers:
            mc = re.search(r"FAILED: return code (\d+)", m["text"])
            if mc:
                code = int(mc.group(1))
        out["exit"] = {"rc": rc, "verdict": verdict, "program_exit_code": code,
                       "note": "rc is the simulator process's exit status; program_exit_code is what the program "
                               "returned (from the testbench's SUCCESS / 'FAILED: return code N' line)"}
        if state == "timeout":
            out["exit"]["reason"] = f"wall-clock limit {st.get('timeout_s')} s reached; raise timeout_s or check for a hang"
    if state == "elab_failed":
        ef = d / "elab_errors.txt"
        out["elab_errors"] = ef.read_text().splitlines()[:5] if ef.exists() else []
        out["fix"] = "fix the compile error in the file, then rtl_recompile it and sim_run again"
    if state == "elaborating":
        out["note"] = "elaboration in progress (~4 min); the simulation starts afterwards"
    return out


def _read_uart(path: Path):
    lines = []
    if not path.exists():
        return lines
    for n, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        m = _UART_RE.match(raw)
        if not m:
            continue
        host, idx, t_ps, text = m.groups()
        lines.append({"n": n, "host_time": host if host and host != "--:--:--" else None,
                      "t_ns": int(t_ps) // 1000 if t_ps else None, "uart": int(idx), "text": text.rstrip()})
    return lines


def sim_uart(run_id: str, since_line: int = 0, max_lines: int = 100, wait_s: int = 0,
             until: str | None = None) -> dict:
    cfg = load()
    d = run_dir(cfg, run_id)
    if d is None:
        return _err("unknown run_id", "use the run_id returned by sim_run")
    since_line = int(since_line or 0)
    max_lines = max(1, min(int(max_lines or 100), 500))
    wait_s = max(0, min(int(wait_s or 0), MAX_WAIT_S))
    needle = until if until and until != "end" else None
    deadline = time.time() + wait_s
    while True:
        st = load_state(d)
        lines = _read_uart(d / "uart.txt")
        new = [l for l in lines if l["n"] > since_line]
        active = st.get("phase") in _ACTIVE
        if until == "end":
            done = False                                        # only the end of the run (or the deadline) stops it
        elif needle:
            done = any(needle in l["text"] for l in new)
        else:
            done = bool(new)                                    # default: the first new line
        if done or not active or time.time() >= deadline:
            break
        time.sleep(2)
    out = {
        "run_id": run_id, "state": st.get("phase"), "lines": new[:max_lines], "total": len(lines),
        "more": len(new) > max_lines, "file": str(d / "uart.txt"),
    }
    if new:
        out["next_since_line"] = new[:max_lines][-1]["n"]
    if not lines and st.get("phase") in ("queued", "elab"):
        out["note"] = "run has not started simulating yet"
    return out


def _kill_pgid(pgid: int) -> bool:
    try:
        os.killpg(pgid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False


def _kill_run(cfg: Config, run_id: str) -> dict:
    d = run_dir(cfg, run_id)
    if d is None:
        return {"run_id": run_id, "result": "unknown run_id"}
    st = load_state(d)
    if st.get("phase") not in ("queued", "elab", "running"):
        return {"run_id": run_id, "result": f"already {st.get('phase')}"}
    (d / "killed").touch()
    killed = []
    pg = d / "xsim.pgid"
    if pg.exists():
        pgid = int(pg.read_text().strip() or 0)
        if pgid and _kill_pgid(pgid):
            killed.append(f"xsim pgid {pgid}")
    if st.get("phase") in ("queued", "elab") and st.get("runner_pid"):
        if _kill_pgid(int(st["runner_pid"])):
            killed.append(f"runner pgid {st['runner_pid']}")
        st["phase"] = "killed"
        st["updated_at"] = datetime.now().isoformat(timespec="seconds")
        (d / "run.json").write_text(json.dumps(st, indent=1))
        # the snapshot may be half built: make sure it is not marked usable
        snap = st.get("snapshot")
        if snap and not (cfg.work / f"{snap}.ok").exists():
            subprocess.run(["rm", "-rf", str(cfg.work / "xsim.dir" / snap)], check=False)
    else:
        # a running simulation: the runner notices xsim exiting and records phase=killed (plus rc and the final
        # UART sweep). Wait for that so sim_status right after this call already says killed.
        deadline = time.time() + 15
        while time.time() < deadline and load_state(d).get("phase") in ("queued", "elab", "running"):
            time.sleep(0.3)
        st = load_state(d)
        if st.get("phase") in ("queued", "elab", "running"):  # runner gone or stuck: record it ourselves
            if st.get("runner_pid"):
                _kill_pgid(int(st["runner_pid"]))
            st["phase"] = "killed"
            st["updated_at"] = datetime.now().isoformat(timespec="seconds")
            (d / "run.json").write_text(json.dumps(st, indent=1))
    return {"run_id": run_id, "result": "killed", "state": load_state(d).get("phase"), "processes": killed}


def sim_kill(run_id: str | None = None, session_id: str | None = None, all: bool = False) -> dict:
    cfg = load()
    from . import session as sess  # late import: sessions live in their own module
    results = []
    if all:
        for p in sorted(cfg.runs.iterdir()) if cfg.runs.exists() else []:
            if (p / "run.json").exists() and load_state(p).get("phase") in ("queued", "elab", "running"):
                results.append(_kill_run(cfg, p.name))
        results += sess.kill_all()
        return {"killed": results or [], "note": "no active runs or sessions" if not results else None}
    if run_id:
        results.append(_kill_run(cfg, run_id))
    if session_id:
        results.append(sess.kill_session(session_id))
    if not results:
        return _err("nothing to kill", "pass run_id, session_id or all=true")
    return {"killed": results}
