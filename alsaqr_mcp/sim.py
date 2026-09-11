"""sim_run: launch an AlSaqr xsim run in the background."""
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .config import Config, load
from .elf import ElfError, check_layout, elf_info

SNAPSHOT_PREFIX = "tb_"


def _err(error: str, fix: str, **extra) -> dict:
    d = {"error": error, "fix": fix}
    d.update(extra)
    return d


def snapshot_stamp(cfg: Config) -> str:
    """Identity of the elaborated design: every override file (path, size, mtime), the tb defines,
    the xelab generics and the DPI library. Any change means a new snapshot."""
    h = hashlib.sha1()
    for p in sorted(cfg.overrides.rglob("*")):
        if p.is_file() and not p.name.endswith((".bak", ".wonly", ".orig")):
            st = p.stat()
            h.update(f"{p.relative_to(cfg.overrides)}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    h.update(("defines:" + ",".join(cfg.tb_defines) + "\n").encode())
    h.update(("generics:" + json.dumps(cfg.xelab_generics, sort_keys=True) + "\n").encode())
    dpi = cfg.dpi_dir / "libdpi.so"
    if dpi.exists():
        h.update(f"dpi|{dpi.stat().st_size}|{dpi.stat().st_mtime_ns}\n".encode())
    # the compiled library itself (a recompiled RTL file changes the library's timestamp)
    lib = cfg.work / "xsim.dir" / "work"
    if lib.exists():  # a recompiled unit rewrites its .sdb in place: fingerprint newest mtime + count
        newest, count = 0, 0
        tb_units = set()
        tu = cfg.work / "tb_units.txt"
        if tu.exists():
            tb_units = set(tu.read_text().split())
        with os.scandir(lib) as it:
            for e in it:
                # units recompiled by every elaboration (the testbench and what it includes) must not feed the stamp
                if e.is_file() and "ariane_tb" not in e.name and e.name not in tb_units:
                    count += 1
                    newest = max(newest, e.stat().st_mtime_ns)
        h.update(f"lib|{count}|{newest}\n".encode())
    return h.hexdigest()[:10]


def snapshot_name(stamp: str) -> str:
    return f"{SNAPSHOT_PREFIX}{stamp}"


def snapshot_ready(cfg: Config, snap: str) -> bool:
    return (cfg.work / "xsim.dir" / snap / "xsimk").exists() and (cfg.work / f"{snap}.ok").exists()


def _tb_xvlog_opts(cfg: Config) -> str:
    """The include/define options bender used for the tb block, minus the ones we replace."""
    compile_sh = cfg.work / "compile.sh"
    line = ""
    for l in compile_sh.read_text().splitlines():
        if re.match(r"^xvlog .*[ /]ariane_tb\.sv( |$)", l):
            line = l
            break
    opts = re.findall(r"(-i|-d) ([^ ]+)", line)
    keep = []
    for k, v in opts:
        if k == "-d" and (v.startswith("XSIM_FORCE_RESET") or v in ("DUAL_BOOT",)):
            continue
        keep.append(f"{k} {v}")
    keep += [f"-d {d}" for d in cfg.tb_defines]
    return " ".join(keep)


def _make_run_id(cfg: Config, tag: str | None, binary: Path) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", tag) if tag else re.sub(r"\.riscv$|\.elf$", "", binary.name)
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", base)[:40] or "run"
    rid = base
    n = 1
    while (cfg.runs / rid).exists():
        n += 1
        rid = f"{base}-{n}"
    return rid


def sim_run(binary: str, timeout_s: int | None = None, tag: str | None = None) -> dict:
    cfg = load()
    timeout_s = int(timeout_s or cfg.default_timeout_s)
    b = Path(os.path.expanduser(binary))
    if not b.is_absolute():
        b = Path.cwd() / b
    b = b.resolve()
    if not b.is_file():
        return _err("binary not found", f"no file at {b}; build the test and pass the .riscv ELF path")
    try:
        info = elf_info(cfg, b)
    except ElfError as e:
        return _err("elf unreadable", f"{b}: {e}")
    checks, problems = check_layout(cfg, info)
    if problems:
        p = problems[0]
        return _err(p["error"], p["fix"], checks=checks,
                    problems=problems, entry=f"0x{info['entry']:08x}")
    if not (cfg.work / "compile.sh").exists():
        return _err("no compiled library", f"{cfg.work} has no compile.sh; run the xsim compile flow first")

    stamp = snapshot_stamp(cfg)
    snap = snapshot_name(stamp)
    need_elab = not snapshot_ready(cfg, snap)

    run_id = _make_run_id(cfg, tag, b)
    run_dir = cfg.runs / run_id
    run_dir.mkdir(parents=True)
    plusargs = dict(cfg.plusargs)
    plusargs["CVA6_STRING"] = str(b)
    pa = " ".join(f"-testplusarg {k}={v}" if v != "" else f"-testplusarg {k}" for k, v in plusargs.items())
    gt = " ".join(f"-generic_top {k}={v}" for k, v in cfg.xelab_generics.items())
    started = datetime.now().isoformat(timespec="seconds")
    state = {
        "run_id": run_id, "binary": str(b), "snapshot": snap, "phase": "queued",
        "elab": "rebuilding" if need_elab else "reused", "started_at": started,
        "timeout_s": timeout_s, "entry": f"0x{info['entry']:08x}",
        "tohost": f"0x{info['symbols']['tohost']:08x}",
    }
    (run_dir / "run.json").write_text(json.dumps(state, indent=1))
    env = {
        "RUN_DIR": str(run_dir), "WORK": str(cfg.work), "SNAP": snap, "NEED_ELAB": "1" if need_elab else "0",
        "TB_FILE": str(cfg.tb_file), "TB_OPTS": _tb_xvlog_opts(cfg), "DPI_DIR": str(cfg.dpi_dir),
        "GENERICS": gt, "PLUSARGS": pa, "TIMEOUT_S": str(timeout_s), "STALL_IDLE_S": str(cfg.stall_idle_s),
        "VIVADO_SETTINGS": cfg.vivado_settings, "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
    }
    runner_copy = run_dir / "runner.sh"
    runner_copy.write_text(cfg.runner.read_text())  # private copy: editing scripts must not affect live runs
    with open(run_dir / "runner.out", "w") as out:
        proc = subprocess.Popen(
            ["setsid", "bash", str(runner_copy)], cwd=str(cfg.work), env=env,
            stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
        )
    time.sleep(0.5)
    state["runner_pid"] = proc.pid
    try:  # the runner may already have rewritten run.json (phase change): merge, don't clobber
        cur = json.loads((run_dir / "run.json").read_text())
        cur["runner_pid"] = proc.pid
        (run_dir / "run.json").write_text(json.dumps(cur, indent=1))
    except Exception:
        (run_dir / "run.json").write_text(json.dumps(state, indent=1))
    return {
        "run_id": run_id, "snapshot": snap, "elab": state["elab"],
        "elab_note": "first run on this design: elaboration takes ~4 min before simulation starts"
        if need_elab else "snapshot reused: simulation starts immediately",
        "entry": state["entry"], "tohost": state["tohost"], "binary_size": info["size"],
        "checks": checks, "timeout_s": timeout_s, "runner_pid": proc.pid,
        "uart": {"tool": "sim_uart", "args": {"run_id": run_id}, "file": str(run_dir / "uart.txt"),
                 "note": "console output of the mock UART; lines interleave when cores print without the lock"},
        "logs": {"stdout": str(run_dir / "stdout.log"), "watchdog": str(run_dir / "watchdog.txt"),
                 "state": str(run_dir / "run.json")},
        "next": [f"sim_status(run_id='{run_id}') to follow progress (phase, sim time, markers, stalls)",
                 f"sim_uart(run_id='{run_id}') to read console output"],
    }
