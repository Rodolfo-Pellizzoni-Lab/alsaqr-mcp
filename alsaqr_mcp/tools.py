"""Tool registry: name, description, JSON schema, implementation.

A: sim_run, sim_status, sim_uart, sim_kill, sim_session, sim_stall_trace
B: rtl_override, rtl_recompile, rtl_fix_ring
C: sw_build, soc_lookup, soc_bootflow
"""
from . import rtl, session, sim, soc, stall, status


class UnknownTool(Exception):
    pass


_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}
_LS = {"type": "array", "items": _S}

TOOLS = {
    # ------------------------------------------------------------------ A: simulation
    "sim_run": {
        "description": (
            "Launch an AlSaqr SoC simulation (Vivado xsim) of a RISC-V ELF in the background and return a run_id. "
            "The binary must be an L3 build (entry 0x80000000, `make build` / sw_build): code and data live in DRAM behind "
            "the LLC, secondary cores are booted by the software itself (mailbox for core 1, APMU interrupts for cores 2/3). "
            "The elaborated design is reused across runs; it is rebuilt only when an RTL/tb override changed (~4 min). "
            "Follow progress with sim_status(run_id); read the mock-UART console with sim_uart(run_id)."
        ),
        "schema": {"type": "object", "properties": {
            "binary": {**_S, "description": "path to the test ELF (.riscv)"},
            "timeout_s": {**_I, "description": "wall-clock limit for the simulation (default 7200)"},
            "tag": {**_S, "description": "optional run name; becomes the run_id and the log prefix"}},
            "required": ["binary"]},
        "fn": lambda a: sim.sim_run(a["binary"], a.get("timeout_s"), a.get("tag")),
    },
    "sim_status": {
        "description": (
            "Progress of a run started by sim_run: state (queued/elaborating/running/finished/stalled/timeout/killed), "
            "simulated time (ns), wall time, memory, JTAG/preload/finish markers with approximate sim time, a stall hint "
            "and the exit verdict. Compact; raw logs stay on disk. since_ns returns only markers after that simulated time."
        ),
        "schema": {"type": "object", "properties": {
            "run_id": _S, "since_ns": {**_I, "description": "only markers with t_ns > since_ns"}},
            "required": ["run_id"]},
        "fn": lambda a: status.sim_status(a["run_id"], a.get("since_ns")),
    },
    "sim_uart": {
        "description": (
            "Mock-UART console lines of a run (what the cores printf). Each line has n (line number), t_ns (simulated "
            "time), uart index and text. Use since_line=<last n seen> for deltas; wait_s blocks up to that long for new "
            "lines (0 = return immediately). Cores that print without a lock interleave characters, as in Questa."
        ),
        "schema": {"type": "object", "properties": {
            "run_id": _S, "since_line": {**_I, "description": "return lines with n > since_line"},
            "max_lines": {**_I, "description": "cap (default 100, max 500)"},
            "wait_s": {**_I, "description": "block up to this many seconds for new lines (max 300)"}},
            "required": ["run_id"]},
        "fn": lambda a: status.sim_uart(a["run_id"], a.get("since_line", 0), a.get("max_lines", 100), a.get("wait_s", 0)),
    },
    "sim_kill": {
        "description": "Stop a run (run_id) or an interactive session (session_id), or everything (all=true). Kills by process group, never by name.",
        "schema": {"type": "object", "properties": {"run_id": _S, "session_id": _S, "all": _B}},
        "fn": lambda a: status.sim_kill(a.get("run_id"), a.get("session_id"), bool(a.get("all", False))),
    },
    "sim_session": {
        "description": (
            "Persistent interactive xsim on a debug-visible snapshot: simulation state is kept between calls, so probing "
            "at 3.0 ms and then at 3.6 ms costs seconds, not two runs from zero. ops: open(binary) -> session_id; "
            "advance(to_ns | by_ns) runs the simulator forward (blocks up to wait_s, else returns 'running': use wait); "
            "wait; probe(signals=[paths]) returns hex values and, for structs, verified named fields; "
            "scope_list(scope) lists child instances and signals of a scope; force(path, value) / release(path); close; list. "
            "Paths are relative to dut (e.g. 'i_host_domain/i_axi_llc/slv_req_i'); 'tb/...' for testbench objects; "
            "'/...' absolute. A debug snapshot is ~2x slower than sim_run and is elaborated once (~5 min)."
        ),
        "schema": {"type": "object", "properties": {
            "op": {**_S, "enum": ["open", "advance", "wait", "probe", "scope_list", "force", "release", "close", "list"]},
            "session_id": _S, "binary": {**_S, "description": "open: the L3 ELF to load"},
            "to_ns": {**_I, "description": "advance: absolute simulated time in ns"},
            "by_ns": {**_I, "description": "advance: relative step in ns"},
            "signals": {**_LS, "description": "probe: up to 32 signal paths"},
            "scope": {**_S, "description": "scope_list: instance path (default dut)"},
            "path": {**_S, "description": "force/release: signal path"},
            "value": {**_S, "description": "force: value, e.g. 0, 1, 8'hff"},
            "wait_s": {**_I, "description": "advance/wait: max seconds to block (default 600)"}},
            "required": ["op"]},
        "fn": lambda a: session.sim_session(a["op"], a.get("session_id"), a.get("binary"), a.get("to_ns"), a.get("by_ns"),
                                            a.get("signals"), a.get("scope"), a.get("path"), a.get("value"), a.get("wait_s")),
    },
    "sim_stall_trace": {
        "description": (
            "Find what a stalled run is doing. Start with run_id (uses the watchdog's stall time) or binary+at_ns; it "
            "elaborates a debug snapshot if needed, steps to the exact time the simulator stops advancing, then captures an "
            "xsim ptrace window there and ranks the executing processes with their source lines. Result kinds: comb_ring "
            "(an always_comb ready/valid ring; the suggestion names the block, its partners and the variables to assign "
            "once -> rtl_fix_ring), deadlock_or_idle, no_stall. Asynchronous: poll with trace_id."
        ),
        "schema": {"type": "object", "properties": {
            "run_id": {**_S, "description": "a stalled run"}, "trace_id": {**_S, "description": "poll an existing trace"},
            "at_ns": {**_I, "description": "override the stall search start (ns)"},
            "window_ns": {**_I, "description": "ptrace window length (default 700)"},
            "binary": {**_S, "description": "with at_ns when no run_id is given"},
            "snapshot": {**_S, "description": "use this already-elaborated debug snapshot instead of the current design's"}}},
        "fn": lambda a: stall.sim_stall_trace(a.get("run_id"), a.get("trace_id"), a.get("at_ns"), a.get("window_ns", 700),
                                              a.get("binary"), a.get("snapshot")),
    },
    # ------------------------------------------------------------------ B: RTL overrides
    "rtl_override": {
        "description": (
            "Manage scratch overrides of repo RTL/tb files (the repo itself is never modified). ops: create(path) copies "
            "the repo file into the override tree and registers it; diff(path) shows override vs repo; show(path); "
            "revert(path) resets the override to the repo content; remove(path) deletes it; list(filter?) lists all. "
            "Paths are relative to hardware/ (e.g. 'ip_list/riscv-dbg/src/dm_mem.sv') or absolute."
        ),
        "schema": {"type": "object", "properties": {
            "op": {**_S, "enum": ["create", "diff", "show", "revert", "remove", "list"]},
            "path": _S, "filter": {**_S, "description": "list: substring filter"}},
            "required": ["op"]},
        "fn": lambda a: rtl.rtl_override(a["op"], a.get("path"), a.get("filter")),
    },
    "rtl_recompile": {
        "description": (
            "Recompile RTL/tb files into the xsim library with the exact include/define options the original flow used "
            "(looked up in compile.sh); the override copy is used when one exists, else the repo file. Returns compiler "
            "errors as {file, line, msg}. The next sim_run/sim_session/sim_stall_trace elaborates a new snapshot."
        ),
        "schema": {"type": "object", "properties": {
            "files": {**_LS, "description": "repo-relative (below hardware/) or absolute paths"},
            "extra_defines": {**_LS, "description": "additional +define names for this compile"}},
            "required": ["files"]},
        "fn": lambda a: rtl.rtl_recompile(a["files"], a.get("extra_defines")),
    },
    "rtl_fix_ring": {
        "description": (
            "Apply the assign-once rewrite to the always_comb blocks of a file: every listed variable written inside an "
            "always_comb is renamed to a shadow and assigned once at the block's end, which removes xsim's transient "
            "re-triggering on ready/valid rings without changing RTL semantics. Creates the override if needed, audits the "
            "result (tails on unwritten variables, doubly driven structs) and returns the diff. dry_run previews only. "
            "Use the written_vars from sim_stall_trace's suggestion, then rtl_recompile the file."
        ),
        "schema": {"type": "object", "properties": {
            "file": _S, "vars": {**_LS, "description": "variables the block writes (outputs and cross-block signals)"},
            "dry_run": _B}, "required": ["file", "vars"]},
        "fn": lambda a: rtl.rtl_fix_ring(a["file"], a["vars"], bool(a.get("dry_run", False))),
    },
    # ------------------------------------------------------------------ C: software and navigation
    "sw_build": {
        "description": (
            "Build a bare-metal test from software/<test> for the L3 flow (`make build` with the RISC-V GCC 16 toolchain and "
            "the compatibility flags baked in), copy the ELF next to the other binaries and check its layout (entry "
            "0x80000000, tohost in L2). Returns compiler errors as {file, line, msg}."
        ),
        "schema": {"type": "object", "properties": {
            "test": {**_S, "description": "test name under software/ (e.g. hello_culsans, quad_boot) or a directory path"},
            "extra_cflags": {**_S, "description": "appended to the compiler invocation"},
            "clean": {**_B, "description": "make clean first"}},
            "required": ["test"]},
        "fn": lambda a: soc.sw_build(a["test"], a.get("extra_cflags"), bool(a.get("clean", False))),
    },
    "soc_lookup": {
        "description": (
            "Navigate the SoC without grepping: an address (0x...) -> which slave/window it hits (from the SoC packages and "
            "the address-rule tables) with the offset; a module name -> where it is instantiated and its static hierarchical "
            "paths (for sim_session probes); a typedef name -> its member layout(s); any other identifier -> grep hits."
        ),
        "schema": {"type": "object", "properties": {"query": _S}, "required": ["query"]},
        "fn": lambda a: soc.soc_lookup(a["query"]),
    },
    "soc_bootflow": {
        "description": (
            "Static explainer of how a simulation boots on this SoC: memory windows, the marker sequence with typical "
            "simulated times, how each core is woken (JTAG, mailbox, APMU), failure signatures and what to call next. "
            "Read once before interpreting sim_status/sim_uart."
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
