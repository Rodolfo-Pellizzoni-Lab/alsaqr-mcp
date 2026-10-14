"""Tool registry: name, description, JSON schema, implementation.

Simulation: sim_run, sim_status, sim_uart, sim_kill, sim_session, sim_stall_trace
RTL edits:  rtl_changes, rtl_recompile
Software and navigation: sw_build, soc_lookup, soc_bootflow
Validation: sim_regress
Accelerators: accel_add, accel_list, accel_unit_test, accel_soc_test, accel_remove
"""
from . import accel, regress, rtl, session, sim, soc, stall, status


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
            "To wait for it, call sim_status(run_id, wait_s=900): it blocks until the run ends and then returns the verdict "
            "and the console, so there is no need to poll or sleep."
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
            "logs stay on disk. since_ns returns only markers after that simulated time. "
            "Waiting: wait_s (max 900) blocks until the run ends, or until the text `until` appears in a marker or console "
            "line, or until simulated time reaches until_ns (e.g. well past the time SUCCESS is expected, to tell a hung "
            "program from a slow one); `wait` says why it returned. After a wait, or once the run has ended, the result "
            "includes the last console lines, so a separate sim_uart call is rarely needed."
        ),
        "schema": {"type": "object", "properties": {
            "run_id": _S, "since_ns": {**_I, "description": "only markers with t_ns > since_ns"},
            "wait_s": {**_I, "description": "block up to this many seconds (max 900) for the run to end or a condition below"},
            "until": {**_S, "description": "with wait_s: also return as soon as this text (case-sensitive) appears in a marker or console line"},
            "until_ns": {**_I, "description": "with wait_s: also return once simulated time reaches this many ns"}},
            "required": ["run_id"]},
        "fn": lambda a: status.sim_status(a["run_id"], a.get("since_ns"), a.get("wait_s", 0), a.get("until"),
                                          a.get("until_ns")),
    },
    "sim_uart": {
        "description": (
            "Console output of a run: every line the software printed through the UART, with n (line number), t_ns "
            "(simulated time), uart index and text. Use since_line=<last n seen> for deltas. wait_s blocks up to that long "
            "(max 900): by default until the first new line; with until='end' until the run ends; with until=<text> until a "
            "new line containing that text appears. When several cores print at the same time without a lock their "
            "characters interleave."
        ),
        "schema": {"type": "object", "properties": {
            "run_id": _S, "since_line": {**_I, "description": "return lines with n > since_line"},
            "max_lines": {**_I, "description": "cap (default 100, max 500)"},
            "wait_s": {**_I, "description": "block up to this many seconds (max 900); see until"},
            "until": {**_S, "description": "with wait_s: 'end' = wait for the run to end; any other text = wait for a "
                                           "new line containing it (default: the first new line)"}},
            "required": ["run_id"]},
        "fn": lambda a: status.sim_uart(a["run_id"], a.get("since_line", 0), a.get("max_lines", 100), a.get("wait_s", 0),
                                        a.get("until")),
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
    # ------------------------------------------------------------------ validation
    "sim_regress": {
        "description": (
            "Build and run a set of self-checking test programs in parallel and return one compact pass/fail table "
            "(per test: result, exit code, simulated ms, and the CHECK FAIL / TRAP lines of failing tests). "
            "suite='soc' (default) validates the SoC: t_mem (DRAM through L1/CCU/LLC with every access size and an "
            "eviction sweep, on-chip SRAM), t_atomics (every AMO + LR/SC on DRAM and SRAM), t_smp (4 cores: wake-up, "
            "contended AMO and LR/SC counters, coherence, message passing), t_irq (CLINT software and timer interrupts, "
            "PLIC registers), t_socket (accelerator socket error responses), hello_culsans, quad_boot. suite='accel' runs "
            "every registered accelerator's C test, 'all' both; or tests=[names under he-soc software/ or paths]. "
            "Blocks up to wait_s (max 900); a typical suite needs ~3 min (plus ~4 min if the design was changed). If "
            "tests are still running it returns regress_id: call again with it to keep waiting."
        ),
        "schema": {"type": "object", "properties": {
            "suite": {**_S, "description": "soc | accel | all (default soc)"},
            "tests": {**_LS, "description": "explicit test list instead of a suite (max 10)"},
            "regress_id": {**_S, "description": "continue waiting for an earlier regression"},
            "wait_s": {**_I, "description": "block up to this many seconds (default and max 900)"}}},
        "fn": lambda a: regress.sim_regress(a.get("suite"), a.get("tests"), a.get("regress_id"), a.get("wait_s", 900)),
    },
    # ------------------------------------------------------------------ accelerators
    "accel_add": {
        "description": (
            "Add a custom hardware accelerator to the SoC. The accelerator socket sits in the cluster window: slot s has "
            "its registers at 0x10000000 + s*0x10000 (64 KB), a 64-bit memory master port into the SoC (DRAM, on-chip "
            "SRAM), and an interrupt (PLIC ID 9). This creates, from a working template (a DMA-style engine computing "
            "dst[i] = f(src[i]) with CTRL/STATUS/ID registers): hardware/accel/<name>/<name>.sv (edit the RTL), "
            "hardware/accel/<name>/accel_test.svh (unit test body), software/accel/<name>/<name>.c (SoC test), and wires "
            "the module into the socket and the build. The port contract is in the RTL header and hardware/accel/README.md. "
            "Then: accel_unit_test (~5 s) and accel_soc_test (whole SoC, ~6-8 min). Extra .sv files dropped in "
            "hardware/accel/<name>/ are picked up automatically."
        ),
        "schema": {"type": "object", "properties": {
            "name": {**_S, "description": "lower-case identifier; becomes the module name"},
            "slot": {**_I, "description": "slot 0-7 (default: first free)"}}, "required": ["name"]},
        "fn": lambda a: accel.accel_add(a["name"], a.get("slot")),
    },
    "accel_list": {
        "description": "Registered accelerators: slot, register base, RTL files, C test directory; and the free slots.",
        "schema": {"type": "object", "properties": {}},
        "fn": lambda a: accel.accel_list(),
    },
    "accel_unit_test": {
        "description": (
            "Compile and simulate one accelerator alone in the generic unit testbench (no SoC; ~5 s). The test body is "
            "hardware/accel/<name>/accel_test.svh (inside an initial block; functions/tasks for a reference model go "
            "in accel_test_decls.svh, included at module scope), with helpers wr/rd/rd_check (registers), mem_write/mem_read/mem_fill "
            "(a sparse 64-bit memory behind the accelerator's memory port, with random stalls and protocol checks), "
            "check, wait_irq, wait_reg, cycles; max_cycles / reg_timeout can be set (full list in "
            "hardware/accel/tb/accel_tb.sv). Every check/rd_check/wait_* call counts as one check. Returns pass/fail, the "
            "test's own $display lines (output), the log path and the failing checks, or compile/elaboration errors with file:line. The SoC's IP library is visible, so the "
            "accelerator may instantiate common_cells etc. waves=true writes a VCD of the accelerator's signals."
        ),
        "schema": {"type": "object", "properties": {
            "name": _S, "seed": {**_I, "description": "random seed for the memory stalls (default 1)"},
            "waves": {**_B, "description": "dump a VCD of every signal inside the accelerator"}}, "required": ["name"]},
        "fn": lambda a: accel.accel_unit_test(a["name"], a.get("seed", 1), bool(a.get("waves", False))),
    },
    "accel_soc_test": {
        "description": (
            "Run an accelerator inside the whole SoC: compiles the accelerator and socket sources that changed into the "
            "simulator library, builds software/accel/<name>/<name>.c, simulates it (the design is re-elaborated after "
            "an RTL change, ~4 min, then ~2-3 min of simulation) and waits up to wait_s (max 900) for the verdict. "
            "Returns the result (exit code = number of failed checks), the TEST / CHECK FAIL / TRAP console lines plus the "
            "last console lines, or "
            "compile errors. A register read from an empty slot, or one the accelerator does not acknowledge within 4096 "
            "cycles, returns 0xdeadbeef instead of hanging (CVA6 ignores the AXI error response)."
        ),
        "schema": {"type": "object", "properties": {
            "name": _S, "wait_s": {**_I, "description": "block up to this many seconds (default/max 900; 0 = return the run_id)"},
            "extra_cflags": {**_S, "description": "appended to the C compiler flags"}}, "required": ["name"]},
        "fn": lambda a: accel.accel_soc_test(a["name"], a.get("wait_s", 900), a.get("extra_cflags")),
    },
    "accel_remove": {
        "description": "Unregister an accelerator (frees its slot; its files are kept).",
        "schema": {"type": "object", "properties": {"name": _S}, "required": ["name"]},
        "fn": lambda a: accel.accel_remove(a["name"]),
    },
}


def list_tools():
    return [{"name": n, "description": t["description"], "inputSchema": t["schema"]} for n, t in TOOLS.items()]


def call(name: str, args: dict):
    if name not in TOOLS:
        raise UnknownTool(name)
    return TOOLS[name]["fn"](args)
