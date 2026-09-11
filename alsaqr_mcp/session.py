"""sim_session: a persistent interactive xsim (debug snapshot) that keeps simulation state between calls.

ops: open, advance, wait, probe, scope_list, force, release, close, list
"""
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .config import Config, load
from .elf import ElfError, check_layout, elf_info
from .sim import snapshot_stamp
from . import typeinfo

MAX_SIGNALS = 32
MAX_SCOPE_ITEMS = 200


def _err(error, fix, **extra):
    d = {"error": error, "fix": fix}
    d.update(extra)
    return d


def sessions_root(cfg: Config) -> Path:
    return cfg.simexp / "sessions"


def debug_snapshot_name(cfg: Config) -> str:
    return f"tb_{snapshot_stamp(cfg)}_dbg"


def debug_snapshot_ready(cfg: Config, snap: str) -> bool:
    return (cfg.work / "xsim.dir" / snap / "xsimk").exists() and (cfg.work / f"{snap}.ok").exists()


def _sdir(cfg: Config, sid: str) -> Path | None:
    d = sessions_root(cfg) / sid
    return d if (d / "state.json").exists() else None


def _state(d: Path) -> dict:
    return json.loads((d / "state.json").read_text())


def _save(d: Path, st: dict):
    st["updated_at"] = datetime.now().isoformat(timespec="seconds")
    (d / "state.json").write_text(json.dumps(st, indent=1))


def _plusargs(cfg: Config, binary: Path) -> str:
    pa = dict(cfg.plusargs)
    pa["CVA6_STRING"] = str(binary)
    return " ".join(f"-testplusarg {k}={v}" if v != "" else f"-testplusarg {k}" for k, v in pa.items())


def _generics(cfg: Config) -> str:
    return " ".join(f"-generic_top {k}={v}" for k, v in cfg.xelab_generics.items())


def _tb_opts(cfg: Config) -> str:
    from .sim import _tb_xvlog_opts
    return _tb_xvlog_opts(cfg)


def parse_time_ns(s: str) -> int | None:
    m = re.search(r"([\d.]+)\s*(ps|ns|us|ms|s)\b", s)
    if not m:
        return None
    v, u = float(m.group(1)), m.group(2)
    return int(v * {"ps": 1e-3, "ns": 1, "us": 1e3, "ms": 1e6, "s": 1e9}[u])


# ---------------------------------------------------------------- command channel
def _alive(st: dict) -> bool:
    pgid = st.get("xsim_pgid")
    if not pgid:
        return False
    try:
        os.killpg(int(pgid), 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _send(d: Path, st: dict, tcl: str, wait_s: float) -> tuple[str, list[str]]:
    """Queue Tcl into the session, wait for its end marker. Returns (status, output lines).
    status: 'done' | 'busy' (marker not seen within wait_s; the command stays queued)."""
    seq = int(st.get("cmd_seq", 0)) + 1
    st["cmd_seq"] = seq
    st["pending_seq"] = seq
    _save(d, st)
    marker = f"@@DONE {seq}"
    with open(d / "cmd.fifo", "w") as f:
        f.write(f"{tcl}\nputs \"{marker}\"\nflush stdout\n")
        f.flush()
    return _wait_marker(d, st, seq, wait_s)


def _wait_marker(d: Path, st: dict, seq: int, wait_s: float) -> tuple[str, list[str]]:
    marker = f"@@DONE {seq}"
    start_marker = f"@@DONE {seq - 1}"
    deadline = time.time() + wait_s
    out_path = d / "out.log"
    while True:
        txt = out_path.read_text(errors="replace") if out_path.exists() else ""
        if marker in txt:
            seg = txt.split(marker, 1)[0]
            if start_marker in seg:
                seg = seg.rsplit(start_marker, 1)[1]
            st["pending_seq"] = None
            _save(d, st)
            return "done", [l for l in seg.splitlines() if l.strip()]
        if time.time() >= deadline or not _alive(st):
            return ("busy" if _alive(st) else "dead"), []
        time.sleep(0.5)


def _resolve(st: dict, path: str) -> str:
    """Accept: 'dut/...', 'i_host_domain/...' (relative to dut), 'tb/...' (relative to the testbench) or an
    absolute xsim path starting with '/'."""
    top = st.get("top") or ""
    p = path.strip()
    if p.startswith("/"):
        return p
    if p.startswith("tb/"):
        return f"{top}/{p[3:]}"
    if p.startswith("dut/"):
        return f"{top}/{p}"
    return f"{top}/dut/{p}"


def _norm(s: str) -> str:
    """xsim prints nested packed structs with '{...}' inside a parent but bare when read directly."""
    return s.replace("'{", "").replace("}", "").replace(" ", "")


def _tcl_str(s: str) -> str:
    return "{" + s + "}"


# ---------------------------------------------------------------- ops
def op_open(cfg: Config, binary: str, session_id: str | None) -> dict:
    b = Path(os.path.expanduser(binary)).resolve()
    if not b.is_file():
        return _err("binary not found", f"no file at {b}")
    try:
        info = elf_info(cfg, b)
    except ElfError as e:
        return _err("elf unreadable", str(e))
    checks, problems = check_layout(cfg, info)
    if problems:
        return _err(problems[0]["error"], problems[0]["fix"], checks=checks)
    snap = debug_snapshot_name(cfg)
    need_elab = not debug_snapshot_ready(cfg, snap)
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id) if session_id else "s_" + hashlib.sha1(
        f"{b}{time.time()}".encode()).hexdigest()[:6]
    root = sessions_root(cfg)
    d = root / sid
    if d.exists():
        return _err("session exists", f"session {sid} already exists; choose another session_id or close it")
    d.mkdir(parents=True)
    st = {"session_id": sid, "binary": str(b), "snapshot": snap, "phase": "elab" if need_elab else "starting",
          "elab": "rebuilding" if need_elab else "reused", "t_ns": 0, "cmd_seq": 0, "pending_seq": None,
          "created_at": datetime.now().isoformat(timespec="seconds")}
    _save(d, st)
    env = {
        "SESSION_DIR": str(d), "WORK": str(cfg.work), "SNAP": snap, "NEED_ELAB": "1" if need_elab else "0",
        "TB_FILE": str(cfg.tb_file), "TB_OPTS": _tb_opts(cfg), "DPI_DIR": str(cfg.dpi_dir),
        "GENERICS": _generics(cfg), "PLUSARGS": _plusargs(cfg, b), "VIVADO_SETTINGS": cfg.vivado_settings,
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
    }
    runner = d / "runner.sh"
    runner.write_text((cfg.runner.parent / "session_runner.sh").read_text())  # private copy
    with open(d / "runner.out", "w") as out:
        proc = subprocess.Popen(["setsid", "bash", str(runner)], cwd=str(cfg.work), env=env, stdout=out,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    st["runner_pid"] = proc.pid
    _save(d, st)
    return {
        "session_id": sid, "snapshot": snap, "elab": st["elab"], "state": st["phase"],
        "note": ("debug elaboration in progress (~5 min); the session becomes ready afterwards"
                 if need_elab else "starting xsim (~30 s)"),
        "t_ns": 0, "paths": "signal paths are relative to dut (e.g. 'i_host_domain/i_axi_llc/slv_req_i'); "
                             "use 'tb/...' for testbench objects or an absolute '/…' path",
        "next": [f"sim_session(op='advance', session_id='{sid}', to_ns=...) then op='probe'",
                 f"sim_session(op='scope_list', session_id='{sid}', scope='i_host_domain') to discover names"],
    }


def _ready(cfg: Config, d: Path, st: dict, wait_s: float) -> dict | None:
    """Make sure the interpreter answered its readiness marker; returns an error dict if not."""
    deadline = time.time() + wait_s
    while True:
        st = _state(d)
        if st.get("phase") in ("elab_failed", "closed", "dead"):
            errs = (d / "elab_errors.txt").read_text().splitlines()[:5] if (d / "elab_errors.txt").exists() else []
            return _err(f"session {st.get('phase')}", "open a new session", errors=errs)
        if st.get("phase") == "ready":
            return None
        if st.get("phase") == "starting" and (d / "out.log").exists() and "@@DONE 0" in (d / "out.log").read_text(errors="replace"):
            m = re.search(r"@@TOP (.+)", (d / "out.log").read_text(errors="replace"))
            top = m.group(1).rstrip("\r\n") if m else ""
            if top.startswith("/\\") and not top.endswith(" "):
                top += " "  # escaped identifier: the terminating space is part of the name
            st["top"] = top
            st["phase"] = "ready"
            _save(d, st)
            return None
        if time.time() >= deadline:
            return _err(f"session not ready ({st.get('phase')})",
                        "elaboration/start still in progress; call again in a minute (op='wait' blocks up to wait_s)")
        time.sleep(2)


def _finish_advance(d: Path, st: dict, lines: list[str]) -> dict:
    t = None
    for l in lines:
        m = re.match(r"@@T (.+)", l)
        if m:
            t = parse_time_ns(m.group(1))
    if t is not None:
        st["t_ns"] = t
        _save(d, st)
    finished = any("$finish" in l for l in lines)
    out = {"session_id": st["session_id"], "state": "ready", "t_ns": st.get("t_ns")}
    if finished:
        out["note"] = "the testbench called $finish; the session can still be probed but not advanced"
    return out


def op_advance(cfg: Config, sid: str, to_ns=None, by_ns=None, wait_s=600) -> dict:
    d = _sdir(cfg, sid)
    if d is None:
        return _err("unknown session_id", "open a session first")
    st = _state(d)
    e = _ready(cfg, d, st, min(wait_s, 60))
    if e:
        return e
    st = _state(d)
    if st.get("pending_seq"):
        return _err("session busy", "a previous command has not completed; use op='wait'")
    cur = int(st.get("t_ns", 0))
    if to_ns is not None:
        delta = int(to_ns) - cur
    elif by_ns is not None:
        delta = int(by_ns)
    else:
        return _err("missing target", "pass to_ns (absolute) or by_ns (relative), in ns")
    if delta <= 0:
        return {"session_id": sid, "state": "ready", "t_ns": cur, "note": "already at or past that time"}
    status, lines = _send(d, st, f"run {delta}ns\nputs \"@@T [current_time]\"", float(wait_s))
    if status == "busy":
        return {"session_id": sid, "state": "running", "t_ns": cur, "target_ns": cur + delta,
                "note": f"still running after {wait_s}s; call op='wait' (it returns when the run completes)"}
    if status == "dead":
        st["phase"] = "dead"; _save(d, st)
        return _err("session died", "xsim exited; open a new session", tail=lines[-5:])
    return _finish_advance(d, _state(d), lines)


def op_wait(cfg: Config, sid: str, wait_s=600) -> dict:
    d = _sdir(cfg, sid)
    if d is None:
        return _err("unknown session_id", "open a session first")
    st = _state(d)
    e = _ready(cfg, d, st, float(wait_s))
    if e:
        return e
    st = _state(d)
    seq = st.get("pending_seq")
    if not seq:
        return {"session_id": sid, "state": "ready", "t_ns": st.get("t_ns")}
    status, lines = _wait_marker(d, st, int(seq), float(wait_s))
    if status != "done":
        return {"session_id": sid, "state": "running" if status == "busy" else "dead", "t_ns": st.get("t_ns")}
    return _finish_advance(d, _state(d), lines)


def op_probe(cfg: Config, sid: str, signals: list[str], wait_s=60) -> dict:
    d = _sdir(cfg, sid)
    if d is None:
        return _err("unknown session_id", "open a session first")
    if not signals:
        return _err("no signals", "pass signals: [path, ...] (max 32)")
    signals = list(signals)[:MAX_SIGNALS]
    st = _state(d)
    e = _ready(cfg, d, st, min(wait_s, 60))
    if e:
        return e
    st = _state(d)
    if st.get("pending_seq"):
        return _err("session busy", "a run is in progress; use op='wait' first")
    tcl = []
    for i, s in enumerate(signals):
        p = _tcl_str(_resolve(st, s))
        tcl.append(f"if {{[catch {{set v [get_value -radix hex {p}]}} e]}} {{puts \"@@E {i} $e\"}} else {{puts \"@@V {i} $v\"}}")
        tcl.append(f"if {{[catch {{set dsc [describe {p}]}} e]}} {{puts \"@@D {i}\"}} else {{puts \"@@D {i} $dsc\"}}")
    status, lines = _send(d, st, "\n".join(tcl), float(wait_s))
    if status != "done":
        return _err("probe timed out" if status == "busy" else "session died", "retry; if it persists close and reopen the session")
    values, descs, errors = {}, {}, {}
    for l in lines:
        if l.startswith("@@V "):
            i, v = l[4:].split(" ", 1)
            values[int(i)] = v.strip()
        elif l.startswith("@@E "):
            i, v = l[4:].split(" ", 1)
            errors[int(i)] = v.strip()[:160]
        elif l.startswith("@@D "):
            parts = l[4:].split(" ", 1)
            descs[int(parts[0])] = [parts[1].strip()] if len(parts) > 1 else []
    out = {"session_id": sid, "t_ns": st.get("t_ns"), "values": {}}
    structs = {}  # i -> (parts, candidates)
    for i, s in enumerate(signals):
        if i in errors:
            out["values"][s] = {"error": errors[i]}
            continue
        v = values.get(i)
        entry = {"value": v}
        desc = " ".join(descs.get(i, []))[:200]
        if desc:
            entry["type"] = desc
        if v and ("'{" in v or "," in v):
            parts = typeinfo._split_top(v)
            if parts:
                structs[i] = (parts, typeinfo.candidates_by_arity(cfg, len(parts)))
        out["values"][s] = entry
    # name struct members: xsim resolves dotted member paths, so verify each typedef candidate of the right
    # arity by reading its members and comparing them positionally with the tuple xsim printed
    if structs:
        st = _state(d)
        tcl = []
        arrays = {}
        for i, (parts, cands) in structs.items():
            base = _resolve(st, signals[i])
            if len(parts) >= 2 and all(p.startswith("'{") for p in parts):
                # maybe an unpacked array of structs: element 0 decides
                sub = typeinfo._split_top(parts[0])
                if sub:
                    arrays[i] = (sub, typeinfo.candidates_by_arity(cfg, len(sub)))
                    tcl.append(f"if {{![catch {{get_value {_tcl_str(base + '[0]')}}}]}} {{puts \"@@A {i}\"}}")
                    for c, (tname, members) in enumerate(arrays[i][1]):
                        vals = "|".join(f"[get_value -radix hex {_tcl_str(base + '[0].' + m)}]" for m in members)
                        tcl.append(f"if {{![catch {{set mv \"{vals}\"}}]}} {{puts \"@@MV {i} {c} $mv\"}}")
                    continue
            for c, (tname, members) in enumerate(cands):
                vals = "|".join(f"[get_value -radix hex {_tcl_str(base + '.' + m)}]" for m in members)
                tcl.append(f"if {{![catch {{set mv \"{vals}\"}}]}} {{puts \"@@MV {i} {c} $mv\"}}")
        status, lines = _send(d, st, "\n".join(tcl), float(wait_s))
        if status == "done":
            is_array = set()
            got = {}
            for l in lines:
                if l.startswith("@@A "):
                    is_array.add(int(l[4:]))
                elif l.startswith("@@MV "):
                    i, c, mv = l[5:].split(" ", 2)
                    got.setdefault(int(i), {})[int(c)] = mv.strip()
            for i, cs in got.items():
                if i in arrays and i in is_array:
                    sub, cands = arrays[i]
                    parts = structs[i][0]
                    norms = [_norm("|".join(typeinfo._split_top(p) or [])) for p in parts]
                    for c, mv in cs.items():
                        if _norm(mv) in norms:  # mv is element [0]; xsim prints [N-1] first for [N-1:0] arrays
                            k = norms.index(_norm(mv))
                            ordered = list(reversed(parts)) if k == len(parts) - 1 and len(parts) > 1 else parts
                            tname, members = cands[c]
                            e = out["values"][signals[i]]
                            e["struct"] = f"{tname}[{len(parts)}]"
                            e["elements"] = [dict(zip(members, typeinfo._split_top(p) or [])) for p in ordered]
                            e["note"] = "elements listed in index order [0], [1], ..."
                            break
                elif i not in arrays:
                    parts, cands = structs[i]
                    for c, mv in cs.items():
                        if _norm(mv) == _norm("|".join(parts)):
                            tname, members = cands[c]
                            e = out["values"][signals[i]]
                            e["struct"] = tname
                            e["fields"] = dict(zip(members, parts))
                            break
    return out


def op_scope_list(cfg: Config, sid: str, scope: str, wait_s=60) -> dict:
    d = _sdir(cfg, sid)
    if d is None:
        return _err("unknown session_id", "open a session first")
    st = _state(d)
    e = _ready(cfg, d, st, min(wait_s, 60))
    if e:
        return e
    st = _state(d)
    if st.get("pending_seq"):
        return _err("session busy", "a run is in progress; use op='wait' first")
    p = _tcl_str(_resolve(st, scope or "dut"))
    tcl = (f"if {{[catch {{current_scope {p}}} e]}} {{puts \"@@E $e\"}} else {{\n"
           f"foreach s [get_scopes] {{puts \"@@S [file tail $s]\"}}\n"
           f"foreach o [get_objects] {{puts \"@@O [file tail $o]\"}} }}")
    status, lines = _send(d, st, tcl, float(wait_s))
    if status != "done":
        return _err("scope_list timed out", "retry")
    scopes = [l[4:] for l in lines if l.startswith("@@S ")]
    objs = [l[4:] for l in lines if l.startswith("@@O ")]
    errs = [l[4:] for l in lines if l.startswith("@@E ")]
    if errs:
        return _err("no such scope", errs[0][:200])
    return {"session_id": sid, "scope": scope, "children": scopes[:MAX_SCOPE_ITEMS], "objects": objs[:MAX_SCOPE_ITEMS],
            "truncated": len(scopes) > MAX_SCOPE_ITEMS or len(objs) > MAX_SCOPE_ITEMS}


def op_force(cfg: Config, sid: str, path: str, value: str | None, release: bool = False, wait_s=30) -> dict:
    d = _sdir(cfg, sid)
    if d is None:
        return _err("unknown session_id", "open a session first")
    st = _state(d)
    e = _ready(cfg, d, st, 30)
    if e:
        return e
    st = _state(d)
    p = _tcl_str(_resolve(st, path))
    forces = st.setdefault("forces", {})
    if release:
        fid = forces.get(path)
        cmd = f"remove_force {fid}" if fid else "remove_forces -all"
    else:
        cmd = f"add_force {p} {value}"
    status, lines = _send(d, st, f"if {{[catch {{set fid [{cmd}]}} e]}} {{puts \"@@E $e\"}} else {{puts \"@@OK $fid\"}}", float(wait_s))
    errs = [l[4:] for l in lines if l.startswith("@@E ")]
    if errs:
        return _err("force failed", errs[0][:200])
    ok = [l[5:].strip() for l in lines if l.startswith("@@OK")]
    st = _state(d)
    forces = st.setdefault("forces", {})
    if release:
        forces.pop(path, None)
        _save(d, st)
        return {"session_id": sid, "result": "released", "path": path,
                "note": None if fid else "no force id was recorded for this path: all forces in the session were removed"}
    forces[path] = ok[0] if ok and ok[0] else None
    _save(d, st)
    return {"session_id": sid, "result": "forced", "path": path, "force_id": forces[path],
            "note": "port forces collapse onto the whole net; structs cannot be forced"}


def op_close(cfg: Config, sid: str) -> dict:
    return kill_session(sid, graceful=True)


def op_list(cfg: Config) -> dict:
    root = sessions_root(cfg)
    out = []
    for p in sorted(root.iterdir()) if root.exists() else []:
        if (p / "state.json").exists():
            st = _state(p)
            out.append({"session_id": p.name, "state": st.get("phase"), "t_ns": st.get("t_ns"),
                        "binary": os.path.basename(st.get("binary", "")), "alive": _alive(st)})
    return {"sessions": out}


def kill_session(sid: str, graceful: bool = False) -> dict:
    cfg = load()
    d = _sdir(cfg, sid)
    if d is None:
        return {"session_id": sid, "result": "unknown session_id"}
    st = _state(d)
    if graceful and st.get("phase") == "ready" and _alive(st):
        try:
            with open(d / "cmd.fifo", "w") as f:
                f.write("quit\n")
        except OSError:
            pass
        for _ in range(10):
            if not _alive(st):
                break
            time.sleep(0.5)
    for key in ("xsim_pgid", "runner_pid"):
        pg = st.get(key)
        if pg:
            try:
                os.killpg(int(pg), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    st["phase"] = "closed"
    _save(d, st)
    return {"session_id": sid, "result": "closed"}


def kill_all() -> list[dict]:
    cfg = load()
    root = sessions_root(cfg)
    out = []
    for p in sorted(root.iterdir()) if root.exists() else []:
        if (p / "state.json").exists() and _state(p).get("phase") not in ("closed", "dead", "elab_failed"):
            out.append(kill_session(p.name))
    return out


def sim_session(op: str, session_id: str | None = None, binary: str | None = None, to_ns=None, by_ns=None,
                signals=None, scope: str | None = None, path: str | None = None, value=None, wait_s=None) -> dict:
    cfg = load()
    op = (op or "").lower()
    if op == "open":
        if not binary:
            return _err("missing binary", "op='open' needs binary=<program ELF>")
        return op_open(cfg, binary, session_id)
    if op == "list":
        return op_list(cfg)
    if not session_id:
        return _err("missing session_id", "every op except open/list needs session_id")
    if op == "advance":
        return op_advance(cfg, session_id, to_ns, by_ns, int(wait_s or 600))
    if op == "wait":
        return op_wait(cfg, session_id, int(wait_s or 600))
    if op == "probe":
        return op_probe(cfg, session_id, signals or [], int(wait_s or 60))
    if op == "scope_list":
        return op_scope_list(cfg, session_id, scope or "dut", int(wait_s or 60))
    if op == "force":
        if not path or value is None:
            return _err("missing path/value", "op='force' needs path and value")
        return op_force(cfg, session_id, path, str(value))
    if op == "release":
        if not path:
            return _err("missing path", "op='release' needs path")
        return op_force(cfg, session_id, path, None, release=True)
    if op == "close":
        return op_close(cfg, session_id)
    return _err("unknown op", "ops: open, advance, wait, probe, scope_list, force, release, close, list")
