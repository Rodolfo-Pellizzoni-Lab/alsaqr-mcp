"""Tool registry: name, description, JSON schema, implementation.

Simulation: sim_run, sim_status, sim_uart, sim_kill, sim_session, sim_stall_trace
RTL edits:  rtl_changes, rtl_recompile
Software and navigation: sw_build, soc_lookup, soc_bootflow
"""
from . import rtl, session, sim, soc, stall, status


class UnknownTool(Exception):
    pass


_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}
_LS = {"type": "array", "items": _S}

TOOLS = {
    # ------------------------------------------------------------------ simulation
    "sim_run": {
        "description": (
            "Start a simulation of the AlSaqr SoC (RTL in Vivado xsim) running the given RISC-V program, in the background. "
            "The program's code and data are loaded into the simulated DRAM (a plain byte array behind the last-level cache) at "
            "0x80000000 and its tohost word into the on-chip SRAM; core 0 is started over JTAG and the other cores are woken by "
            "the software (see soc_bootflow). Returns a run_id. Build programs with sw_build (or `make build`). "
            "The compiled design is reused across runs and only rebuilt after an RTL change (~4 min). "
            "Follow progress with sim_status(run_id); read the console with sim_uart(run_id)."
        ),
        "schema": {"type": "object", "properties": {
            "binary": {**_S, "description": "path to the program ELF (.riscv)"},
            "timeout_s": {**_I, "description": "wall-clock limit for the simulation (default 7200)"},
            "tag": {**_S, "description": "optional run name; becomes the run_id and the log prefix"}},
            "required": ["binary"]},
        "fn": lambda a: sim.sim_run(a["binary"], a.get("timeout_s"), a.get("tag")),
    },
    "sim_status": {
        "description": (
            "Progress of a run started by sim_run: state (queued / elaborating / running / finished / stalled / timeout / "
            "killed), simulated time in ns, wall time, memory, the testbench markers seen so far (JTAG steps, program load, "
            "SUCCESS/FAILED, $finish) with their simulated time, and the exit verdict. 'stalled' means simulated time stopped "
            "advancing and the watchdog killed the run; sim_stall_trace then shows what was executing. Compact output; raw "
            "logs stay on disk. since_ns returns only markers after that simulated time."
        ),
        "schema": {"type": "object", "properties": {
            "run_id": _S, "since_ns": {**_I, "description": "only markers with t_ns > since_ns"}},
            "required": ["run_id"]},
        "fn": lambda a: status.sim_status(a["run_id"], a.get("since_ns")),
    },
    "sim_uart": {
        "description": (
            "Console output of a run: every line the software printed through the UART, with n (line number), t_ns "
            "(simulated time), uart index and text. Use since_line=<last n seen> for deltas; wait_s blocks up to that long for "
            "new lines (0 = return immediately). When several cores print at the same time without a lock their characters "
            "interleave."
        ),
        "schema": {"type": "object", "properties": {
            "run_id": _S, "since_line": {**_I, "description": "return lines with n > since_line"},
            "max_lines": {**_I, "description": "cap (default 100, max 500)"},
            "wait_s": {**_I, "description": "block up to this many seconds for new lines (max 300)"}},
            "required": ["run_id"]},
        "fn": lambda a: status.sim_uart(a["run_id"], a.get("since_line", 0), a.get("max_lines", 100), a.get("wait_s", 0)),
    },
    "sim_kill": {
        "description": "Stop a run (run_id) or an interactive session (session_id), or everything (all=true).",
        "schema": {"type": "object", "properties": {"run_id": _S, "session_id": _S, "all": _B}},
        "fn": lambda a: status.sim_kill(a.get("run_id"), a.get("session_id"), bool(a.get("all", False))),
    },
    "sim_session": {
        "description": (
            "Interactive simulation that keeps its state between calls, for looking at signals: open(binary) starts one and "
            "returns a session_id; advance(to_ns | by_ns) runs the simulator forward (blocks up to wait_s, otherwise returns "
            "'running' and wait collects it); probe(signals=[paths]) reads current values (hex; struct-typed signals come back "
            "with named fields); scope_list(scope) lists the sub-instances and signals of an instance to discover names; "
            "force(path, value) / release(path) override a signal; close; list. Signal paths are instance paths below the SoC "
            "top, e.g. 'i_host_domain/i_axi_llc/slv_req_i' (soc_lookup gives the path of a module); 'tb/...' addresses the "
            "testbench. Runs about twice as slowly as sim_run and needs a one-time ~5 min build."
        ),
        "schema": {"type": "object", "properties": {
            "op": {**_S, "enum": ["open", "advance", "wait", "probe", "scope_list", "force", "release", "close", "list"]},
            "session_id": _S, "binary": {**_S, "description": "open: the program ELF to load"},
            "to_ns": {**_I, "description": "advance: absolute simulated time in ns"},
            "by_ns": {**_I, "description": "advance: relative step in ns"},
            "signals": {**_LS, "description": "probe: up to 32 signal paths"},
            "scope": {**_S, "description": "scope_list: instance path (default: the SoC top)"},
            "path": {**_S, "description": "force/release: signal path"},
            "value": {**_S, "description": "force: hex like probe output (1, 0, deadbeef, 0x3f) or a Verilog literal (4'b1010, 'd12)"},
            "wait_s": {**_I, "description": "advance/wait: max seconds to block (default 600)"}},
            "required": ["op"]},
        "fn": lambda a: session.sim_session(a["op"], a.get("session_id"), a.get("binary"), a.get("to_ns"), a.get("by_ns"),
                                            a.get("signals"), a.get("scope"), a.get("path"), a.get("value"), a.get("wait_s")),
    },
    "sim_stall_trace": {
        "description": (
            "When a run is 'stalled' (simulated time stopped advancing), find out what the simulator was doing at that "
            "moment: it re-runs to the exact time, records every RTL process that executes during a short window, and "
            "reports the busiest instances with their source file and line. kind 'busy_loop' = a few processes execute "
            "over and over inside one time step (the report names the block and the variables it writes); kind 'idle' = "
            "almost nothing executes, the design is waiting for a signal that never comes (then use sim_session to probe). "
            "Runs in the background: start with run_id, poll with trace_id."
        ),
        "schema": {"type": "object", "properties": {
            "run_id": {**_S, "description": "a stalled run"}, "trace_id": {**_S, "description": "poll an existing trace"},
            "at_ns": {**_I, "description": "start searching from this simulated time instead of the watchdog's"},
            "window_ns": {**_I, "description": "recording window length (default 700)"},
            "binary": {**_S, "description": "with at_ns when no run_id is given"},
            "snapshot": {**_S, "description": "use this already-built debug design instead of the current one"}}},
        "fn": lambda a: stall.sim_stall_trace(a.get("run_id"), a.get("trace_id"), a.get("at_ns"), a.get("window_ns", 700),
                                              a.get("binary"), a.get("snapshot")),
    },
    # ------------------------------------------------------------------ RTL edits
    "rtl_changes": {
        "description": (
            "RTL and testbench sources are the files of the he-soc checkout (branch xsim-port) under hardware/: edit them "
            "directly, then rtl_recompile. This tool shows what differs from the git HEAD: list(filter?) -> changed HDL "
            "files with in_library (false = edited after it was compiled); diff(path) -> the change as a unified diff; "
            "revert(path) -> restore the HEAD version (then rtl_recompile it). Paths are relative to hardware/ (e.g. "
            "'ip_list/riscv-dbg/src/dm_mem.sv') or absolute."
        ),
        "schema": {"type": "object", "properties": {
            "op": {**_S, "enum": ["list", "diff", "revert"]},
            "path": _S, "filter": {**_S, "description": "list: substring filter"}},
            "required": ["op"]},
        "fn": lambda a: rtl.rtl_changes(a["op"], a.get("path"), a.get("filter")),
    },
    "rtl_recompile": {
        "description": (
            "Compile edited RTL/testbench files into the simulator's library with the same include paths and defines the "
            "full build used. Returns compiler errors as {file, line, msg}. The next sim_run / sim_session / "
            "sim_stall_trace rebuilds the design (~4-5 min). sim_run warns when an edited file was not recompiled."
        ),
        "schema": {"type": "object", "properties": {
            "files": {**_LS, "description": "repo-relative (below hardware/) or absolute paths"},
            "extra_defines": {**_LS, "description": "additional +define names for this compile"}},
            "required": ["files"]},
        "fn": lambda a: rtl.rtl_recompile(a["files"], a.get("extra_defines")),
    },
    # ------------------------------------------------------------------ software and navigation
    "sw_build": {
        "description": (
            "Build a bare-metal test program (`make <target>` with the RISC-V toolchain), copy the ELF next to the other "
            "binaries and check that it is laid out the way the simulator loads it (code/data in DRAM at 0x80000000, "
            "tohost in the on-chip SRAM). Returns compiler errors as {file, line, msg}. test is a name under he-soc "
            "software/ or a directory path; a directory inside an alsaqr-software checkout is built with that bundle's "
            "own toolchains (rv64 + the rv32 shim the APMU firmware Makefiles need), so targets such as pmu_bench that "
            "embed the PMU firmware work. extra_cflags is appended to every compiler call (e.g. '-DXSIM')."
        ),
        "schema": {"type": "object", "properties": {
            "test": {**_S, "description": "test name under software/ (e.g. hello_culsans, quad_boot) or a directory path"},
            "target": {**_S, "description": "make target (default build); the ELF is <target>.riscv or <test>.riscv"},
            "extra_cflags": {**_S, "description": "appended to the compiler invocation"},
            "clean": {**_B, "description": "make clean first"}},
            "required": ["test"]},
        "fn": lambda a: soc.sw_build(a["test"], a.get("extra_cflags"), bool(a.get("clean", False)), a.get("target", "build")),
    },
    "soc_lookup": {
        "description": (
            "Find your way around the SoC: an address (0x...) -> which memory window / peripheral it belongs to, with the "
            "offset; a subsystem name (UART, PLIC, mailbox, APMU, ISPM, DSPM, LLC, ...) -> its address windows; a module "
            "name -> where it is instantiated and its instance paths (usable in sim_session); a struct type name (also "
            "pkg::name) -> its fields; any other identifier (signal, parameter, `define macro) -> where it appears in the "
            "compiled RTL and testbench, definitions first."
        ),
        "schema": {"type": "object", "properties": {"query": _S}, "required": ["query"]},
        "fn": lambda a: soc.soc_lookup(a["query"]),
    },
    "soc_bootflow": {
        "description": (
            "Read this first. What the simulation actually is (RTL in xsim, DRAM as a byte array, mock UART console), the "
            "memory map, how a program is loaded and how each core starts, the markers to expect in order with typical "
            "simulated times, and what to do when something goes wrong."
        ),
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: soc.soc_bootflow(),
    },
}


def list_tools():
    return [{"name": n, "description": t["description"], "inputSchema": t["schema"]} for n, t in TOOLS.items()]


def call(name: str, args: dict):
    if name not in TOOLS:
        raise UnknownTool(name)
    return TOOLS[name]["fn"](args)
