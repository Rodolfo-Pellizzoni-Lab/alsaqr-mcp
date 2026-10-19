"""The connected VCU118: board_program, board_run (one bare-metal ELF), board_regress (a suite), linux_boot /
linux_status (prebuilt OpenSBI + Linux over JTAG, detached) and linux_cmd (a shell command on the console).

Board facts the tools encode (he-soc VCU118 bring-up notes): the FPGA is programmed through Vivado's hw_server and
needs ~30 s of DDR4 calibration afterwards; CVA6 is debugged with the alsaqr-software OpenOCD + GDB bundle over the
Olimex probe; harts 2 and 3 cannot be halted once running, so OpenOCD manages harts 0-1 and the other two are released
through debug-module writes; the console is a CP2105 UART fixed at 38400 baud whose receiver overruns on bursts; the
startup code does not clear .bss, so the runner zeroes it from GDB; a Linux boot must start from a freshly programmed
bitstream (bare-metal tests leave dirty cache lines).
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import soc
from .config import ROOT, load

MAX_WAIT_S = 900
SUITES = {"soc": ["t_mem", "t_atomics", "t_smp", "t_irq", "t_socket", "t_qos", "t_cbqri", "t_dram", "t_dma", "t_iotag", "t_part", "t_eth"],
          "accel": ["example", "crc32"]}
RUN_MS = {"t_eth": 90000}   # tests that need more than the default console window (t_eth waits for the PHY and listens on the LAN)


def _err(error, fix, **extra):
    d = {"error": error, "fix": fix}
    d.update(extra)
    return d


def _board_dir(cfg):
    d = cfg.state / "board"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _board_state(cfg):
    f = _board_dir(cfg) / "board.json"
    return json.loads(f.read_text()) if f.exists() else {}


def _save_board(cfg, st):
    (_board_dir(cfg) / "board.json").write_text(json.dumps(st, indent=1))


def _problem(cfg):
    if not cfg.board_bundle or not (cfg.board_bundle / "openocd" / "bin" / "openocd").exists():
        return _err("no board bundle", f"config board.bundle ({cfg.board_bundle}) must be the alsaqr-software checkout (openocd/, toolchain/, source.sh)")
    if not Path(cfg.board_uart).exists():
        return _err("console not found", f"{cfg.board_uart} is missing: is the board's USB UART connected?")
    return None


def _bitstream_of(cfg, bitstream: str | None):
    """A bitstream path, or a build_id of a finished fpga_build, or None = the one currently programmed."""
    if not bitstream:
        return None, None
    p = Path(os.path.expanduser(bitstream))
    if p.exists():
        return p, None
    bj = cfg.fpga_builds / bitstream / "build.json"
    if bj.exists():
        st = json.loads(bj.read_text())
        if st.get("phase") != "done" or not st.get("bitstream"):
            return None, _err("build not finished", f"fpga_status(build_id='{bitstream}')")
        if st.get("verdict") != "OK":
            return None, _err("build failed its timing gate", f"fpga_status(build_id='{bitstream}') shows why; pass the .bit path to program it anyway")
        return Path(st["bitstream"]), None
    return None, _err("bitstream not found", "a .bit path or the build_id of a finished fpga_build")


def _stop_debug(cfg):
    subprocess.run(["pkill", "-9", "-x", "openocd"], capture_output=True)
    subprocess.run(["pkill", "-f", "scripts/uart_ca[p].py"], capture_output=True)   # every console reader (a second one splits the bytes)
    bs = _board_state(cfg)
    pid = bs.get("uart_cap_pid")
    if pid:
        subprocess.run(["kill", str(pid)], capture_output=True)
        bs.pop("uart_cap_pid", None)
    bs.pop("linux_boot", None)
    _save_board(cfg, bs)
    time.sleep(1)


def board_program(bitstream: str) -> dict:
    cfg = load()
    pb = _problem(cfg)
    if pb:
        return pb
    bit, e = _bitstream_of(cfg, bitstream)
    if e:
        return e
    if bit is None:
        return _err("no bitstream", "give a .bit path or a finished build_id")
    _stop_debug(cfg)
    d = _board_dir(cfg)
    env = dict(os.environ, BITSTREAM=str(bit))
    cmd = f"source {cfg.vivado_settings} > /dev/null 2>&1 && vivado -mode batch -source {ROOT / 'scripts' / 'fpga_program.tcl'} -nojournal -log {d / 'program.log'}"
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, timeout=600)
    m = re.search(r"PROGRAMMED in (\d+) s", r.stdout)
    if not m:
        errs = [l for l in (r.stdout + r.stderr).splitlines() if l.startswith("ERROR")][:3]
        return _err("programming failed", "is the board powered and the Xilinx JTAG cable connected? see program.log",
                    errors=errs, log=str(d / "program.log"))
    time.sleep(cfg.board_ddr_wait_s)   # DDR4 calibration
    st = {"bitstream": str(bit), "programmed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "program_s": int(m.group(1))}
    _save_board(cfg, st)
    return {"programmed": True, "bitstream": str(bit), "seconds": int(m.group(1)), "ddr_wait_s": cfg.board_ddr_wait_s,
            "next": "board_run / board_regress / linux_boot"}


def _test_dir(cfg, name: str) -> str:
    """A test name as sim_regress lists them: software/<name>, software/soc_tests/<name> or software/accel/<name>."""
    for d in (cfg.software / name, cfg.software / "soc_tests" / name, cfg.software / "accel" / name):
        if (d / "Makefile").exists():
            return str(d)
    return name


def _elf_of(cfg, binary: str) -> tuple[Path | None, dict | None]:
    p = Path(os.path.expanduser(binary))
    if p.exists() and p.suffix == ".riscv":
        return p, None
    b = soc.sw_build(_test_dir(cfg, binary), make_args=["fpga=1"], suffix="_fpga")   # board layout: code and data in DDR
    if "error" in b:
        return None, b
    return Path(b["elf"]), None


def board_run(binary: str, program: str | None = None, sleep_ms: int | None = None, peeks: list[str] | None = None,
              tag: str | None = None) -> dict:
    cfg = load()
    pb = _problem(cfg)
    if pb:
        return pb
    if program:
        r = board_program(program)
        if "error" in r:
            return r
    bs = _board_state(cfg)
    unknown = not bs.get("bitstream")   # whatever is on the FPGA now; the harts' state decides whether a run is possible
    if bs.get("linux_boot"):
        _stop_debug(cfg)   # a Linux session holds OpenOCD with 4 harts; bare-metal runs use the 2-hart configuration
    elf, e = _elf_of(cfg, binary)
    if e:
        return e
    runs = _board_dir(cfg) / "runs"
    runs.mkdir(exist_ok=True)
    run_id = (tag or f"br{time.strftime('%m%d_%H%M%S')}_{elf.stem}").replace("/", "_")
    rdir = runs / run_id
    rdir.mkdir()
    sleep_ms = int(sleep_ms or cfg.board_sleep_ms)
    state = {"run_id": run_id, "phase": "queued", "elf": str(elf), "bitstream": bs.get("bitstream") or "unknown",
             "started": time.time()}
    (rdir / "run.json").write_text(json.dumps(state, indent=1))
    env = dict(os.environ)
    env.update({"RUN_DIR": str(rdir), "ELF": str(elf), "BUNDLE": str(cfg.board_bundle), "UART": cfg.board_uart,
                "BAUD": str(cfg.board_baud), "SLEEP_MS": str(sleep_ms), "PEEKS": " ".join(peeks or []),
                "RELEASE_HARTS": "1", "OPENOCD_CORES": str(cfg.board_openocd_cores), "SCRIPTS": str(ROOT / "scripts")})
    subprocess.run(["bash", str(ROOT / "scripts" / "board_run.sh")], env=env, capture_output=True, text=True,
                   timeout=sleep_ms / 1000 + 180)
    st = json.loads((rdir / "run.json").read_text())
    reprogrammed = False
    if st.get("phase") == "failed" and st.get("stage") == "unhaltable" and unknown:
        return _err("the harts cannot be halted and no bitstream is known to reprogram",
                    "board_program(bitstream=...) or board_run(..., program=...)", run_id=run_id, logs=str(rdir))
    if st.get("phase") == "failed" and st.get("stage") == "unhaltable":
        # the harts hang (a known way this board ends up after a crashed or misbehaving program): reprogram, retry once
        r = board_program(bs["bitstream"])
        if "error" in r:
            return _err("harts cannot be halted and reprogramming failed", r.get("fix", ""), run_id=run_id, logs=str(rdir))
        reprogrammed = True
        bs = _board_state(cfg)
        (rdir / "run.json").write_text(json.dumps(state, indent=1))
        subprocess.run(["bash", str(ROOT / "scripts" / "board_run.sh")], env=env, capture_output=True, text=True,
                       timeout=sleep_ms / 1000 + 180)
        st = json.loads((rdir / "run.json").read_text())
    uart = (rdir / "uart.log").read_bytes().decode("utf-8", "replace").replace("\r", "") if (rdir / "uart.log").exists() else ""
    lines = [l for l in uart.splitlines() if l.strip()]
    line = st.get("test_line") or ""
    if "TEST PASS" in line or line.strip() == "SUCCESS":
        result = "pass"
    elif st.get("phase") == "failed":
        result = "error"
    elif "TEST FAIL" in line or "TRAP" in line or line.strip() == "FAILED":
        result = "fail"
    else:
        result = "no_result"   # no self-check line: the program may still be running or printed nothing
    out = {"run_id": run_id, "result": result, "tohost": st.get("tohost"), "test_line": line or None,
           "elf": str(elf), "console_lines": len(lines), "console_tail": lines[-8:], "logs": str(rdir)}
    if result in ("fail", "error", "no_result"):
        out["failed_checks"] = [l for l in lines if "CHECK FAIL" in l or "FAIL" in l][:10]
    if result == "no_result" and not lines:
        out["note"] = ("nothing captured from the console: the program printed nothing within sleep_ms, or the UART is read "
                       "by another process (board_stop, then retry); tohost 0x1 alone does not prove a pass (the startup "
                       "code writes 1 on exit)")
    if st.get("error"):
        out["error"] = st["error"]
    if reprogrammed:
        out["note"] = "the harts could not be halted (left hung by an earlier run): the board was reprogrammed first"
    if unknown:
        out["bitstream"] = "unknown: the board was not programmed by these tools (board_program records it)"
    if peeks:
        g = (rdir / "gdb.log").read_text() if (rdir / "gdb.log").exists() else ""
        vals = {}
        cur = None
        for l in g.splitlines():
            if l.startswith("PEEK "):
                cur = l.split()[1]
                vals[cur] = []
            elif cur and l.strip().startswith("0x"):
                vals[cur] += l.split()
        out["peeks"] = vals
    bs["last_run"] = run_id
    _save_board(cfg, bs)
    return out


def board_regress(suite: str | None = None, tests: list[str] | None = None, program: str | None = None,
                  sleep_ms: int | None = None) -> dict:
    cfg = load()
    pb = _problem(cfg)
    if pb:
        return pb
    if tests is None:
        if suite == "all":
            tests = SUITES["soc"] + SUITES["accel"]
        elif (suite or "soc") in SUITES:
            tests = SUITES[suite or "soc"]
        else:
            return _err("unknown suite", "suites: soc, accel, all (or pass tests=[...])")
    if program:
        r = board_program(program)
        if "error" in r:
            return r
    t0 = time.time()
    rows = []
    for t in tests:
        r = board_run(t, sleep_ms=sleep_ms or RUN_MS.get(t))
        if "error" in r and "run_id" not in r:
            rows.append({"test": t, "result": "build_error", "detail": r.get("error"), "errors": r.get("errors")})
            continue
        row = {"test": t, "run_id": r["run_id"], "result": r["result"], "tohost": r.get("tohost"), "test_line": r.get("test_line")}
        if r["result"] != "pass":
            row["failed_checks"] = r.get("failed_checks")
        rows.append(row)
    passed = sum(r["result"] == "pass" for r in rows)
    return {"bitstream": _board_state(cfg).get("bitstream"), "passed": passed, "failed": len(rows) - passed,
            "wall_s": int(time.time() - t0), "tests": rows}


def _host_has_ip(ip: str) -> bool:
    r = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True)
    return f" {ip}/" in r.stdout


def linux_boot(bitstream: str | None = None, program: bool = True, srmcfg: dict | None = None, payload: str | None = None,
               dtb: str | None = None, wait_s: int = 0, netboot: bool = False) -> dict:
    cfg = load()
    pb = _problem(cfg)
    if pb:
        return pb
    lx = cfg.board_linux
    nb = dict(lx.get("netboot", {}))
    if netboot:                                   # Ethernet: the rebuilt Linux (axienet) and the socket DTB by default
        payload = payload or nb.get("payload") or lx["payload"]
        dtb = dtb or nb.get("dtb") or lx["dtb"]
    payload = Path(os.path.expanduser(payload or lx["payload"]))
    dtb_p = Path(os.path.expanduser(dtb or lx["dtb"]))
    for f in (payload, dtb_p):
        if not f.exists():
            return _err("missing Linux artifact", f"{f} (config board.linux)")
    nb_env = {}
    if netboot:
        if srmcfg:
            return _err("srmcfg needs the JTAG boot", "over Ethernet the harts are released by the loader, not by GDB")
        elf = Path(os.path.expanduser(nb.get("elf", str(cfg.repo / "software" / "netboot" / "netboot.riscv"))))
        if not elf.exists():
            return _err("no network boot loader", f"{elf}: build it with make -C software/netboot fpga=1 SERVER_IP=... "
                        "STATIC_IP=... (he-soc branch ethernet), or set board.linux.netboot.elf")
        binp = payload if payload.suffix == ".bin" else payload.with_suffix(".bin")
        if not binp.exists():
            return _err("no payload binary", f"{binp}: the loader fetches fw_payload.bin (next to the .elf)")
        host_ip = nb.get("host_ip", "10.42.0.1")
        if not _host_has_ip(host_ip):
            return _err("no address on the private link", f"this machine has no {host_ip}: give the adapter that faces "
                        f"the board its address (e.g. sudo nmcli con mod <connection> ipv4.method manual ipv4.addresses "
                        f"{host_ip}/24 && sudo nmcli con up <connection>), and allow it in the firewall")
        nb_env = {"NETBOOT_ELF": str(elf), "NETBOOT_BIN": str(binp), "NETBOOT_PORT": str(nb.get("port", 6969))}
    if program:
        bit, e = _bitstream_of(cfg, bitstream)
        if e:
            return e
        if bit is None:
            bit = _board_state(cfg).get("bitstream")
            if not bit:
                return _err("nothing to program", "give bitstream=<.bit or build_id>")
        r = board_program(str(bit))
        if "error" in r:
            return r
    else:
        _stop_debug(cfg)
    bs = _board_state(cfg)
    boots = _board_dir(cfg) / "linux"
    boots.mkdir(exist_ok=True)
    boot_id = f"lx{time.strftime('%m%d_%H%M%S')}"
    bdir = boots / boot_id
    bdir.mkdir()
    tags = []
    for h, v in (srmcfg or {}).items():
        try:
            tags.append(f"{int(h)}:{int(str(v), 0):#x}")
        except ValueError:
            return _err("bad srmcfg", "srmcfg = {hart: value}, e.g. {'0': '0x30005'} (RCID 5, MCID 3)")
    state = {"boot_id": boot_id, "phase": "queued", "bitstream": bs.get("bitstream"), "payload": str(payload),
             "dtb": str(dtb_p), "srmcfg": tags, "via": "ethernet" if netboot else "jtag", "started": time.time()}
    (bdir / "boot.json").write_text(json.dumps(state, indent=1))
    env = dict(os.environ)
    env.update({"BOOT_DIR": str(bdir), "BUNDLE": str(cfg.board_bundle), "PAYLOAD": str(payload), "DTB": str(dtb_p),
                "DTB_ADDR": lx.get("dtb_addr", "0x81800000"), "UART": cfg.board_uart, "BAUD": str(cfg.board_baud),
                "UART_SECS": str(lx.get("console_capture_s", 7200)),
                # over Ethernet only the loader is loaded, on the harts OpenOCD can reach (the rest sit in the boot ROM and
                # the loader wakes them itself); a halt request left pending on an unreachable hart would catch it later
                "NUM_HARTS": str(cfg.board_openocd_cores if netboot else lx.get("num_harts", 4)),
                "SRMCFG_LIST": " ".join(tags), "SCRIPTS": str(ROOT / "scripts"), **nb_env})
    with open(bdir / "runner.log", "w") as out:
        subprocess.Popen(["setsid", "bash", str(ROOT / "scripts" / "linux_boot.sh")], env=env, stdout=out,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    bs["linux_boot"] = boot_id
    _save_board(cfg, bs)
    res = {"boot_id": boot_id, "phase": "queued", "via": "ethernet" if netboot else "jtag",
           "expect": ("loader over JTAG ~1 min, payload over Ethernet ~30 s, shell ~1 min later" if netboot
                      else "payload load ~3 min, shell ~1 min later"),
           "next": f"linux_status(boot_id='{boot_id}', wait_s=600)"}
    if wait_s:
        res = linux_status(boot_id, wait_s)
    return res


def _boot_state(cfg, boot_id):
    f = _board_dir(cfg) / "linux" / boot_id / "boot.json"
    return json.loads(f.read_text()) if f.exists() else None


def linux_status(boot_id: str | None = None, wait_s: int = 0) -> dict:
    cfg = load()
    boot_id = boot_id or _board_state(cfg).get("linux_boot")
    if not boot_id:
        return _err("no Linux boot", "linux_boot(...) first")
    st = _boot_state(cfg, boot_id)
    if st is None:
        return _err("unknown boot_id", "linux_boot returns it")
    wait_s = max(0, min(int(wait_s or 0), MAX_WAIT_S))
    t_end = time.time() + wait_s
    while st["phase"] not in ("shell", "failed") and time.time() < t_end:
        time.sleep(5)
        st = _boot_state(cfg, boot_id)
    bdir = _board_dir(cfg) / "linux" / boot_id
    uart = bdir / "uart.log"
    lines = [l for l in uart.read_bytes().decode("utf-8", "replace").replace("\r", "").splitlines() if l.strip()] if uart.exists() else []
    out = {"boot_id": boot_id, "phase": st["phase"], "elapsed_s": int(time.time() - st["started"]),
           "milestones": {k: st[k] for k in ("t_link_s", "t_transfer_s", "transfer_ms", "t_opensbi_s", "t_kernel_s", "t_shell_s") if k in st},
           "via": st.get("via", "jtag"),
           "console_lines": len(lines), "console_tail": lines[-6:], "logs": str(bdir)}
    if st.get("srmcfg"):
        g = (bdir / "gdb.log").read_text() if (bdir / "gdb.log").exists() else ""
        out["srmcfg_written"] = re.findall(r"SRMCFG hart (\d+): \n\$\d+ = (0x[0-9a-f]+)", g)
    for l in lines:
        if l.startswith("Linux version"):
            out["kernel"] = l[:60]
            break
    if st["phase"] == "failed":
        out["error"] = st.get("error")
    elif st["phase"] == "shell":
        out["next"] = "linux_cmd(cmd='...')"
    else:
        out["next"] = f"linux_status(boot_id='{boot_id}', wait_s=600)"
    return out


SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5"]


def _board_ip(cfg) -> str:
    return str(cfg.board_linux.get("netboot", {}).get("board_ip", "10.42.0.2"))


def _ssh_up(ip: str) -> bool:
    """True when dropbear on the board accepts a root login without a password (the rebuilt Linux after 2026-10-19)."""
    r = subprocess.run(["ssh", *SSH_OPTS, f"root@{ip}", "true"], capture_output=True, text=True, timeout=15)
    return r.returncode == 0


def _shell_state(cfg):
    boot_id = _board_state(cfg).get("linux_boot")
    if not boot_id:
        return (None, None), _err("no Linux session", "linux_boot(...) and wait for phase 'shell'")
    st = _boot_state(cfg, boot_id)
    if not st or st["phase"] != "shell":
        return (None, None), _err("Linux shell not up", f"linux_status(boot_id='{boot_id}', wait_s=600)")
    return (boot_id, st), None


def linux_cmd(cmd: str, timeout_s: int = 30, via: str = "auto") -> dict:
    """Run cmd on the booted Linux: over SSH when the board is on the Ethernet link (fast, output intact), else typed
    on the console. via: auto | ssh | uart."""
    cfg = load()
    (boot_id, st), e = _shell_state(cfg)
    if e:
        return e
    if via not in ("auto", "ssh", "uart"):
        return _err("via must be auto, ssh or uart", "linux_cmd(cmd=..., via='auto')")
    ip = _board_ip(cfg)
    if via in ("auto", "ssh") and st.get("via") == "ethernet":
        try:
            r = subprocess.run(["ssh", *SSH_OPTS, f"root@{ip}", cmd], capture_output=True, text=True, timeout=int(timeout_s) + 10)
            if r.returncode != 255:                       # 255 = ssh itself failed (no link, no server)
                lines = (r.stdout + r.stderr).splitlines()
                return {"cmd": cmd, "via": "ssh", "rc": r.returncode, "output": lines[:200], "truncated": len(lines) > 200}
            fail = r.stderr.strip()
        except subprocess.TimeoutExpired:
            return _err(f"ssh root@{ip} timed out after {timeout_s} s", "linux_cmd(cmd=..., timeout_s=<more>)", cmd=cmd, via="ssh")
        if via == "ssh":
            return _err(f"ssh root@{ip} failed: {fail}", "the board's Linux must run dropbear (rebuilt bundle, 2026-10-19) and this "
                        "machine must be 10.42.0.1; linux_cmd(cmd='ifconfig eth0', via='uart') to look from the console")
    elif via == "ssh":
        return _err("SSH needs the Ethernet boot", "linux_boot(netboot=true) puts the board on the link (10.42.0.2)")
    log = _board_dir(cfg) / "linux" / boot_id / "uart.log"
    r = subprocess.run(["bash", str(ROOT / "scripts" / "lx_cmd.sh"), str(log), cfg.board_uart, cmd, str(int(timeout_s))],
                       capture_output=True, text=True, timeout=int(timeout_s) + 30)
    lines = r.stdout.splitlines()
    return {"cmd": cmd, "via": "uart", "output": lines[:200], "truncated": len(lines) > 200}


def linux_copy(src: str, dst: str, to_board: bool = True, timeout_s: int = 300) -> dict:
    """scp a file to the board (to_board=True: src is a host path, dst a board path) or from it, over the Ethernet link."""
    cfg = load()
    _, e = _shell_state(cfg)
    if e:
        return e
    ip = _board_ip(cfg)
    if to_board and not Path(os.path.expanduser(src)).is_file():
        return _err(f"{src}: no such file on this machine", "give the host path of the file to send")
    a, b = (os.path.expanduser(src), f"root@{ip}:{dst}") if to_board else (f"root@{ip}:{src}", os.path.expanduser(dst))
    t0 = time.time()
    try:
        r = subprocess.run(["scp", "-O", "-q", *SSH_OPTS, a, b], capture_output=True, text=True, timeout=int(timeout_s))
    except subprocess.TimeoutExpired:
        return _err(f"scp timed out after {timeout_s} s", "linux_copy(..., timeout_s=<more>); the link moves ~0.5 MB/s")
    ms = int((time.time() - t0) * 1000)
    if r.returncode != 0:
        return _err(f"scp failed: {r.stderr.strip()}", "the board must have booted with linux_boot(netboot=true) and run dropbear "
                    "(rebuilt bundle, 2026-10-19); linux_cmd(cmd='ls -l <dir>') to check the destination")
    size = Path(os.path.expanduser(src if to_board else dst)).stat().st_size
    return {"copied": "to board" if to_board else "from board", "src": src, "dst": dst, "bytes": size, "ms": ms,
            "mb_per_s": round(size / 1048576 / max(ms, 1) * 1000, 2)}


def board_stop() -> dict:
    cfg = load()
    _stop_debug(cfg)
    return {"stopped": True, "note": "OpenOCD and the console capture are stopped; the FPGA keeps running"}
