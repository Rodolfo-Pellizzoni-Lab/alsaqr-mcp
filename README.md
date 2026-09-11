# alsaqr-mcp

MCP server with tools to help AI agents navigate the Alsaqr SoC project

The goal of this project is to have some bare bone tools to help any AI agent navigate, modify and simulate the platform.

## What the simulation is

- The SoC RTL (four CVA6 cores, coherency unit, last-level cache, on-chip SRAM, debug module, interrupt
  controllers, peripherals) simulated with Vivado xsim.
- DRAM is not a memory-chip model: it is a plain byte array attached behind the last-level cache, zero unless
  written. Programs are loaded straight into it at 0x8000_0000.
- The on-chip SRAM at 0x1C00_0000 (32 KB) holds the program's `tohost` word (exit code) and small shared data.
- The console is a mock UART: every line the software prints is captured with its simulated time.
- Core 0 is started by the testbench over JTAG; the other cores start when the software wakes them (mailbox
  interrupt for core 1, APMU counter interrupts for cores 2 and 3). `soc_bootflow` describes this step by step.
- Speed: about 40 s of wall clock per simulated millisecond; a hello-style test finishes in ~3 ms of simulated
  time. The compiled design is reused between runs and rebuilt only after an RTL change (~4 min).

## Using the tools

```
python3 -m alsaqr_mcp serve                          # MCP over stdio
python3 -m alsaqr_mcp list                           # tool schemas
python3 -m alsaqr_mcp sim_run binary=/path/x.riscv   # any tool from the CLI: key=value, JSON for lists
python3 -m alsaqr_mcp sim_session op=probe session_id=s1 'signals=["i_host_domain/i_axi_llc/slv_req_i"]'
```

Claude Code: `claude mcp add alsaqr -- python3 -m alsaqr_mcp serve` (run from this directory, or set `PYTHONPATH`).
Paths to the simulator build tree, the toolchain and the repository are in `config.json`.

Conventions: times are simulated nanoseconds (`*_ns`); outputs are compact JSON and raw logs stay on disk (their
paths are returned); errors are `{error, fix}`. Runs, sessions and traces are background processes with their
state under `~/.cache/he-soc-simexp/{runs,sessions,traces}/<id>/`. The repository is never modified: RTL and
testbench changes are made on override copies.

A typical loop: `soc_bootflow` once → `sw_build` → `sim_run` → `sim_status` / `sim_uart` → if stalled,
`sim_stall_trace` → `sim_session` to probe signals → `rtl_override` + `rtl_recompile` → `sim_run` again.

## Simulation tools

### sim_run
Start a simulation of a program in the background.
```
in:  { binary: path, timeout_s?: int (7200), tag?: str }
out: { run_id, snapshot, elab: "reused"|"rebuilding", elab_note, entry, tohost, binary_size, checks: [str],
       timeout_s, uart: {tool: "sim_uart", args: {run_id}, file, note}, logs: {stdout, watchdog, state}, next: [str] }
err: binary not found | elf unreadable | program linked for the wrong memory | no tohost symbol |
     sections outside DRAM/SRAM | no compiled library
```
The program must be built for DRAM (entry 0x8000_0000, `sw_build` or `make build`). States: queued → elab →
running → finished | stalled | timeout | killed. "stalled" means simulated time stopped advancing for 180 s of
wall clock and the run was killed.

### sim_status
```
in:  { run_id, since_ns?: int }
out: { run_id, state, elab, snapshot, binary, sim_time_ns, wall_s, rss_mb, sim_ns_per_wall_min,
       markers: [{t_ns, approx, kind: success|fail|fatal|finish|jtag|preload|error, text}], markers_truncated,
       uart_lines, uart_hint?, stall?: {at_ns, memory_growing, meaning, next}, exit?: {rc, verdict, reason?},
       elab_errors?, note? }
```
Markers are the testbench's own progress lines (JTAG steps, program load, SUCCESS/FAILED, `$finish`);
`approx: true` means the time is that of the last heartbeat before the line (10 µs resolution).

### sim_uart
```
in:  { run_id, since_line?: int, max_lines?: int (100, max 500), wait_s?: int (0, max 300) }
out: { run_id, state, lines: [{n, host_time, t_ns, uart, text}], total, more, next_since_line?, file, note? }
```
`wait_s` blocks until a new line appears, the run ends or the time is up. Cores that print at the same time
without a lock interleave their characters.

### sim_kill
```
in:  { run_id?: str, session_id?: str, all?: bool }
out: { killed: [{run_id|session_id, result, processes?}] }
```

### sim_session
An interactive simulation that keeps its state between calls, for reading and forcing signals.
```
op=open       { binary, session_id? }                -> { session_id, snapshot, elab, state, note, paths, next }
op=advance    { session_id, to_ns | by_ns, wait_s? } -> { state: ready|running, t_ns, target_ns?, note? }
op=wait       { session_id, wait_s? }                -> { state, t_ns, note? }
op=probe      { session_id, signals: [path] }        -> { t_ns, values: { path: { value, struct?, fields?, elements?, note?, error? } } }
op=scope_list { session_id, scope }                  -> { children: [instance], objects: [signal|param], truncated }
op=force      { session_id, path, value }            -> { result: forced, force_id }
op=release    { session_id, path }                   -> { result: released }
op=close      { session_id }                         -> { result: closed }
op=list       {}                                     -> { sessions: [...] }
```
Signal paths are instance paths below the SoC top, e.g. `i_host_domain/i_axi_llc/slv_req_i` (`soc_lookup`
returns the path of a module; `scope_list` shows what an instance contains); `tb/...` addresses the testbench.
Values are hex. Struct-typed signals come back with named `fields` (arrays of structs as `elements`, in index
order). Opening a session builds a debug-visible copy of the design once (~5 min); it runs about half as fast as
`sim_run`. `advance` returns `running` if it takes longer than `wait_s`; `wait` collects it.

### sim_stall_trace
What the simulator was doing when a run stalled.
```
start: { run_id } | { binary, at_ns }, window_ns? (700), snapshot? -> { trace_id, state, start_ns, snapshot, elab, eta, next }
poll:  { trace_id } -> { state: queued|elab|locate|trace|done|no_stall|failed, stall_ns, kind: busy_loop|idle|unknown,
                         kinds, events_in_window, top_processes: [{instance, count, file, line}],
                         busy_block?: {instance, file, repo_relative, block_line, block_end, variables_written, also_busy, explanation},
                         next?, ptrace_log }
```
It re-runs to the exact simulated time where progress stops, records every RTL process that executes during a
short window and ranks them. `busy_loop`: a few processes execute over and over inside one time step (the
report names the block, the variables it writes and the blocks it keeps waking). `idle`: almost nothing
executes; the design is waiting for a signal that never comes, so open a `sim_session` at `stall_ns` and probe
the request/response signals along the path the program was on.

## RTL editing tools

### rtl_override
The simulator compiles from an override copy of a file when one exists, so the repository stays untouched.
```
op=create { path }  -> { path, override_path, created, note }     make the copy (then edit that file)
op=diff   { path }  -> { path, added, removed, lines, truncated }  copy vs repository
op=show   { path }  -> { path, source: override|repo, file, lines }
op=revert { path }  -> { result }                                  reset the copy to the repository content
op=remove { path }  -> { result }                                  delete the copy
op=list   { filter? } -> { count, overrides: [{path, override_exists, differs}], map }
```
Paths are relative to `hardware/` (e.g. `ip_list/riscv-dbg/src/dm_mem.sv`) or absolute.

### rtl_recompile
```
in:  { files: [path], extra_defines?: [str] }
out: { compiled: [{file, source: override|repo, ok, log}], errors: [{file, line, msg}], note?, fix? }
```
Compiles the files into the simulator library with the include paths and defines the original build used. The
next `sim_run` / `sim_session` / `sim_stall_trace` rebuilds the design.

## Software and navigation tools

### sw_build
```
in:  { test: name|dir, extra_cflags?: str, clean?: bool }
out: { test, elf, entry, tohost, size, sections: [{name, addr, size, region: dram|sram}], checks, problems, next }
err: unknown test (lists the available ones) | build failed {errors: [{file, line, msg}], log}
```
Runs `make build` for a test under `software/` with the RISC-V toolchain and checks the ELF layout. Tests that
also program the APMU core have their own build steps that this tool does not cover yet.

### soc_lookup
```
in:  { query }
out: address   -> { address, matches: [{name, base, end, offset, source}], nearest? }
     subsystem -> { windows: [{name, base, end, source}] }          e.g. UART, PLIC, mailbox, APMU, ISPM, DSPM
     module    -> { module: { instances: [{parent, instance, file, line, generate}], paths: [instance paths] } }
     struct    -> { typedef: { layouts: [[field, ...]] } }
     other     -> { identifier: { hits: [{file, line, text}] } }
```
Address windows come from the SoC packages and the address-rule tables; instance paths follow the same
convention as `sim_session` (`tb/dut/i_host_domain/...`, generate loops as `label[i]`).

### soc_bootflow
No input. What is simulated, the memory map, how a program is loaded and how each core starts, the markers
to expect in order with typical simulated times, and what to do when something goes wrong.
