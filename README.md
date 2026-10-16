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
python3 -m alsaqr_mcp list [prefix]                  # tool schemas (e.g. list accel)
python3 -m alsaqr_mcp sim_run binary=/path/x.riscv   # any tool from the CLI: key=value, JSON for lists
python3 -m alsaqr_mcp sim_session op=probe session_id=s1 'signals=["i_host_domain/i_axi_llc/slv_req_i"]'
```

Claude Code: `claude mcp add alsaqr -- python3 -m alsaqr_mcp serve` (run from this directory, or set `PYTHONPATH`).

Setup: the tools drive the in-tree xsim flow of the he-soc repository's `xsim-port` branch. Check that branch out
and build the simulator library once with `hardware/xsim/build.sh` (~2 min); every tool reports
`{error, fix}` if either is missing. Paths (he-soc checkout, RISC-V toolchain, Vivado) are in `config.json`.

Conventions: times are simulated nanoseconds (`*_ns`); outputs are compact JSON (no indentation) and raw logs stay on disk (their
paths are returned); errors are `{error, fix}`. Runs, sessions and traces are background processes with their
state under `hardware/xsim/work/mcp/{runs,sessions,traces}/<id>/` (git-ignored, next to the compiled library).
RTL and testbench sources are the he-soc files themselves: edit them in place, `rtl_recompile` them, and use
`rtl_changes` (git) to see or undo what was changed.

A typical loop: `soc_bootflow` once → `sw_build` → `sim_run` → `sim_status(wait_s=900)` → if stalled,
`sim_stall_trace` → `sim_session` to probe signals → edit the RTL + `rtl_recompile` → `sim_run` again.

## Simulation tools

### sim_run
Start a simulation of a program in the background.
```
in:  { binary: path, timeout_s?: int (7200), tag?: str }
out: { run_id, snapshot, elab: "reused"|"rebuilding", elab_note, entry, tohost, binary_size, checks: [str],
       timeout_s, uart: {tool: "sim_uart", args: {run_id}, file, note}, logs: {stdout, watchdog, state}, next: [str],
       stale_sources?: [path], warning? }
err: binary not found | elf unreadable | program linked for the wrong memory | no tohost symbol |
     sections outside DRAM/SRAM | xsim flow not found | no compiled library
```
The program must be built for DRAM (entry 0x8000_0000, `sw_build` or `make build`). States: queued → elab →
running → finished | stalled | timeout | killed. "stalled" means simulated time stopped advancing for 180 s of
wall clock and the run was killed. `stale_sources` lists files edited after they were compiled: the run uses
the old version until they are recompiled.

### sim_status
```
in:  { run_id, since_ns?: int, wait_s?: int (0, max 900), until?: str, until_ns?: int, qos?: bool }
out: { run_id, state, elab, snapshot, binary, sim_time_ns, wall_s, rss_mb, sim_ns_per_wall_min,
       markers: [{t_ns, approx, kind: success|fail|fatal|finish|jtag|preload|error, text}], markers_truncated,
       uart_lines, console?: [{n, t_ns, text}], console_note?, uart_hint?, wait?: {waited_s, stopped_because},
       stall?: {at_ns, memory_growing, meaning, next}, exit?: {rc, verdict, program_exit_code, note, reason?}, elab_errors?, note?,
       qos?: ["llc_in src=1 at=0 rcid=6 mcid=11 ar=1033 aw=13", ...], qos_errors?: [str] }
```
`qos=true` adds the simulation's QoS monitor counts (he-soc `hardware/docs/qos.md`): AXI requests per QoS tag
(source core, access type, RCID, MCID) entering the LLC (`llc_in`) and leaving it towards memory (`llc_out`),
the LLC's CBQRI allocations per RCID (`llc_alloc rcid=.. at=.. n=.. ways=.. mask=..`) and the checkers' summaries.
`qos_errors` is always present when the simulation's QoS scoreboard or the LLC allocation check reported an error
(a request with a wrong tag, a refill outside its CBQRI way mask); `sim_regress` then counts the test as failed.
Markers are the testbench's own progress lines (JTAG steps, program load, SUCCESS/FAILED, `$finish`);
`approx: true` means the time is that of the last heartbeat before the line (10 µs resolution).
`wait_s` blocks until the run ends, or until `until` (case-sensitive) appears in a marker or console line, or until simulated time
reaches `until_ns` (useful to tell a hung program from a slow one); `wait.stopped_because` says which. After a wait,
or once the run has ended, `console` carries the last 20 console lines, so one call usually tells the whole story.

### sim_uart
```
in:  { run_id, since_line?: int, max_lines?: int (100, max 500), wait_s?: int (0, max 900), until?: str }
out: { run_id, state, lines: [{n, host_time, t_ns, uart, text}], total, more, next_since_line?, file, note? }
```
`wait_s` blocks until the first new line (default), until the run ends (`until: "end"`) or until a new line
containing `until` appears; it always returns when the run ends or the time is up. Cores that print at the same
time without a lock interleave their characters.

### sim_kill
```
in:  { run_id?: str, session_id?: str, all?: bool }
out: { killed: [{run_id|session_id, result, state?, processes?}] }
```
Returns once the run's state reads `killed`, so a `sim_status` right after it is already final.

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
Values are hex, both ways: `probe` returns hex and `force` takes hex (`deadbeef`, `0x3f`) or a Verilog literal
(`4'b1010`, `8'hff`, `'d12`). Struct-typed signals come back with named `fields` (arrays of structs as `elements`,
in index order). Opening a session builds a debug-visible copy of the design once (~5 min); it runs about half as
fast as `sim_run`. `advance` returns `running` if it takes longer than `wait_s`; `wait` collects it.

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

### rtl_changes
What differs from the git HEAD of the he-soc checkout (the sources are edited in place).
```
op=list   { filter? } -> { count, changes: [{path, status: modified|new|deleted|same as HEAD, in_library}], note }
op=diff   { path }    -> { path, added, removed, lines, truncated }  unified diff against HEAD
op=revert { path }    -> { path, result, note }                      restore the HEAD version (then rtl_recompile it)
```
`in_library: false` means the file was edited after it was compiled; `same as HEAD` marks a file back at its
HEAD content (e.g. after revert) whose edited version is still in the library. Untracked files are listed only
when the build compiles them. Paths are relative to `hardware/` (e.g. `ip_list/riscv-dbg/src/dm_mem.sv`) or absolute.

### rtl_recompile
```
in:  { files: [path], extra_defines?: [str] }
out: { compiled: [{file, ok, log}], errors: [{file, line, msg}], note?, fix? }
```
Compiles the files into the simulator library with the include paths and defines the full build used, and
records the compile (that is what `in_library` and `stale_sources` compare against). The next `sim_run` /
`sim_session` / `sim_stall_trace` rebuilds the design.

## Validation tools

### sim_regress
Build and run several self-checking programs in parallel; one compact table back.
```
in:  { suite?: soc|accel|all (soc), tests?: [name|path], regress_id?: str, wait_s?: int (900, max 900) }
out: { regress_id, passed, failed, running, wall_s, recompiled?, note?, next?,
       tests: [{test, run_id, state, result: pass|fail|running|build_error, exit_code?, sim_ms, summary? | detail?, stall?}] }
```
`soc` = he-soc `software/soc_tests` + hello_culsans + quad_boot:

| Test | What it checks |
|---|---|
| t_mem | core 0 on cached DRAM (L1 -> CCU -> LLC -> DRAM): byte/half/word/dword writes and reads, byte merge, a 64 KB eviction sweep; the same on the uncached on-chip SRAM |
| t_atomics | every AMO (add, swap, and, or, xor, min/max signed and unsigned, 32-bit on the upper word) with its returned old value, LR/SC success and SC-without-reservation failure, on DRAM and on SRAM |
| t_smp | 4 cores woken (mailbox + APMU), contended amoadd and LR/SC counters, per-core cache lines read by core 0, a message-passing ring between cores |
| t_irq | CLINT machine software interrupt (msip), CLINT timer interrupt (mtimecmp), PLIC priority/threshold registers |
| t_socket | accelerator socket: accesses past the last slot return DECERR / 0xdeadbeef (both lanes, 32- and 64-bit) instead of hanging |
| t_qos | Ssqosid `srmcfg` CSR: reset value, WARL widths, access from M/S (ok), U (illegal), VS/VU (virtual instruction); each core's QoS tag read back through the socket's QoS probe (uncached read and write, tag changes); cached traffic with sharing, AMOs, LR/SC and LLC evictions, checked request by request by the simulation's QoS scoreboard |
| t_cbqri | LLC CBQRI capacity controller (0x1040_1800): register interface and STATUS codes; isolation (victim lines of RCID 1 survive 80 aggressor accesses of RCID 2 with disjoint way masks: 0 LLC read misses vs 8 with shared masks); code/data masks; 4 cores with disjoint 4-way masks in one LLC set |
| t_dram | DRAM timing model (0x1A10_6100): real dependent-read latency with the model off, then a fixed latency (every read at least LAT_RD cycles, one counted per hop, none late), late detection when the model is faster than the real memory, the bandwidth limit on a chase and on a stream, writes through the model, pass-through when disabled |
| t_dma | the accelerator socket's DMA against the cores' write-back L1 caches, with slot 0 (`example`): buffers in the uncached SRAM, a DRAM source still dirty in the L1 (does the device read it?), a DRAM destination the core already caches (does the core see the device's write?), the fence.i recipe, DRAM above 256 MB; prints "source coherent, destination coherent" on the design with the CCU port (stale on one without) and the DMA cycles per word |

`accel` = the C test of every registered accelerator. Self-checking programs use `software/soc_tests/include/soc_test.h`
(`CHECK`, a trap guard that turns any exception into a `TRAP mcause=.. mepc=.. mtval=..` line and exit code 127,
`soc_wake_all()` for cores 2-3, `soc_barrier()`); their exit code is the number of failed checks. Accelerator and socket
sources edited since their last compile are recompiled first. If tests are still running when `wait_s` is up, call
again with `regress_id`.

## Accelerator tools

Custom accelerators go into the accelerator socket in the cluster window (he-soc `hardware/accel/README.md`): slot s
has its registers at `0x1000_0000 + s*0x1_0000`, a 64-bit memory master port into the SoC and an interrupt (PLIC ID 9).
Every accelerator has the same ports (a strobe/ack register port, a req/gnt/rvalid memory port, irq_o).

The memory port is DMA through a port of the coherence unit (ACE ReadOnce / WriteUnique): a device reads what a core
wrote even while it is still dirty in that core's write-back L1, and a device's write invalidates the cores' copies.
Buffers may be anywhere (DRAM, on-chip SRAM) and need no cache maintenance; software writes the buffer, `fence`s,
writes START, and reads the result after DONE or the interrupt. Requests are single-beat 64-bit (the socket's
`axi_from_mem` makes them so). The price is one snoop round trip per word (`t_dma` prints the cycles per word). The
same holds for emulated I/O devices plugged into a slot (a virtio-mmio disk or NIC): Linux's stock drivers assume
exactly this coherent DMA.

### A socket device under Linux

`linux_boot` with the bundle's rebuilt Linux (`alsaqr-software/linux/cva6-sdk/install64/fw_payload.elf` and
`alsaqr-socket.dtb`, config.json `linux`): the device tree has one `generic-uio` node for the socket window
(`/dev/uio0`: mmap of the registers, `read()` blocks until PLIC interrupt 9) and a 16 MB `shared-dma-pool` at
0x9F00_0000 nobody else uses; the kernel has UIO, `/proc/self/pagemap` and the virtio-mmio drivers. Verify with
`linux_cmd(cmd='socket_dma')`: it drives slot 0 with buffers in the pool (`/dev/mem`), in the process's own cached
pages (physical address from pagemap), through `/dev/uio0`, and with the interrupt, checking every word without any
cache maintenance (`SOCKET_DMA <mode> ok` per mode, then `SOCKET_DMA PASS`). Source: he-soc
`software/linux/socket_dma`. A kernel driver does the same with `dma_alloc_coherent` (from the pool when its node
names it in `memory-region`) or `dma_map_single`, with no `dma_sync` cost.

### accel_add
```
in:  { name, slot?: 0-7 }
out: { name, slot, base, irq, files: {rtl, unit_test, sw_test}, template, contract, next }
```
Creates `hardware/accel/<name>/<name>.sv` from a working template (dst[i] = compute(src[i], ARG) over LEN 64-bit words,
CTRL/STATUS/ID/CYCLES registers), its unit test `hardware/accel/<name>/accel_test.svh`, and its SoC test
`software/accel/<name>/<name>.c`; registers it in `hardware/accel/slots.json`, regenerates `accel_slots.sv` and the
managed block of `hardware/Bender.yml`, and regenerates `compile.sh`. Every `.sv` in the accelerator's directory is
compiled (packages `*_pkg.sv` first).

### accel_unit_test
```
in:  { name, seed?: int (1), waves?: bool }
out: { name, result: pass|fail|compile_error|elaborate_error|no_verdict, summary, wall_s, log, output?: [the test's $display lines],
       failures?, errors?: [{file,line,msg}], mem_log?, vcd? }
```
The accelerator alone in `hardware/accel/tb/accel_tb.sv` (~5 s): a register driver, a sparse 64-bit memory with random
gnt/rvalid stalls and port-protocol checks, and helpers for the test body (wr, rd, rd_check, mem_write, mem_read,
mem_fill, check, wait_irq, wait_reg, cycles, mem_stall; max_cycles / reg_timeout settable). Functions or tasks for a
reference model go in `accel_test_decls.svh` (included at module scope). Every check/rd_check/wait_* call counts as one
check; port-protocol checks count only when they fail. The SoC library is visible as `work`, so common_cells etc. can
be instantiated. Elaborated with -O0 (xsim 2024.2 mis-evaluates automatic-function calls in loop bodies at -O1/-O2).

### accel_soc_test
```
in:  { name, wait_s?: int (900; 0 = return run_id), extra_cflags? }
out: { name, slot, recompiled?, run_id, elab, state, sim_time_ns, verdict, exit_code,
       console: ["t_ns: line"] (TEST/CHECK FAIL/TRAP lines + the last 6), console_note?, stall? }
     | { result: compile_error|sw_build_error|sim_run_error, ... }
```
Compiles the changed socket/accelerator sources into the SoC library, builds the C test, runs it on the whole SoC and
waits for the verdict (re-elaboration ~4 min after an RTL change, then ~2-3 min).

### accel_list / accel_remove
`accel_list {}` -> slots, bases, RTL files, free slots. `accel_remove {name}` frees the slot (files are kept).

## FPGA bitstreams and the VCU118 board

The same working tree can be built into a VCU118 bitstream and run on the connected board; the tools fold in the
bring-up lessons of that flow (hardware/docs and the memory notes of 2026-10): a timing gate that rejects what the
board would silently mis-execute, the reset / stale-IP / congestion traps, the debugger quirks of this board.

### fpga_build / fpga_status
```
in:  { tag?, opt?, place?, route? (Vivado directives), reuse_synth?: build_id | "last" }
out: { build_id, phase, directives, expect, next }
in:  { build_id?, wait_s? (max 900) }
out: { build_id, phase, elapsed_s, wns_place?, timing: {setup_failing, hold_failing_outside_adv_timer, hold_failing_adv_timer,
       wns, whs, unrouted, soc_clk_mhz (from the MMCM's attributes), clk_constraint_mhz}, hold_failing_endpoints?,
       sources (where each number comes from), verdict: OK | FAIL, bitstream, errors?, logs }   (no build_id: the last builds)
```
A build is a detached `scripts/fpga_build.sh`: the Vivado file list regenerated from Bender.yml (`fpga.bender_make`), the in-tree `run.tcl` project setup and synthesis (`make constr`, the
SoC clock period substituted into `timing_constr.xdc`, ~40 min), one linked checkpoint with the IP contents and
constraints, then `scripts/fpga_impl.tcl` (opt / place / route / gate / bitstream, ~2-3 h, 50 MHz, 4 cores, DDR4, LLC).
State in `<state>/fpga/<build_id>/build.json`, Vivado logs and reports next to it. The gate: 0 failing setup endpoints,
no hold failures outside the adv_timer clock-as-data class, every net routed, MMCM output = the SoC clock. Post-route
`phys_opt_design` is always followed by a re-route (alone it leaves real hold violations). Default directives are the
ones that closed timing on this design; `SSI_SpreadLogic_high` + `AlternateCLBRouting` is the congestion fallback.

### board_program / board_run / board_regress
```
in:  { bitstream: .bit path | build_id }                      -> { programmed, bitstream, seconds, ddr_wait_s }
in:  { binary: test name | ELF built with fpga=1, program?, sleep_ms? (8000), peeks?: ["addr:count"], tag? }
out: { run_id, result: pass | fail | no_result | error, tohost, test_line, console_tail, failed_checks?, peeks?, logs }
in:  { suite?: soc | accel | all, tests?, program?, sleep_ms? }  -> { bitstream, passed, failed, wall_s, tests: [...] }
```
`board_program` goes through Vivado's hw_server and waits for the DDR4 calibration; it stops any debugger / console
session. `board_run` builds a soc_tests or accel test for the board layout (`make build fpga=1`, ELF copied as
`<name>_fpga.riscv`) and drives it over JTAG with the alsaqr-software OpenOCD + GDB (`scripts/board_run.sh`): OpenOCD
manages harts 0-1 (harts 2-3 of this board never halt once running, they are released through debug-module writes),
`.bss` is zeroed from GDB (the startup code does not), the console (38400) is captured, and after `sleep_ms` the
program is halted and `tohost` read over the system bus. Results are the self-check line and `tohost`; the raw
console and GDB logs stay under `<state>/board/runs/<run_id>/`. Every run starts a fresh OpenOCD and ends with `reset halt`; when the harts cannot be halted (a program left them hung, which also happens after a Linux session) the board is reprogrammed with the current bitstream and the run retried once (`note` says so).

### linux_boot / linux_status / linux_cmd / board_stop
```
in:  { bitstream?, program? (true), srmcfg?: {hart: value}, payload?, dtb?, wait_s? }   -> { boot_id, phase, next }
in:  { boot_id?, wait_s? }  -> { phase: openocd | loading | released | opensbi | kernel | shell | failed, milestones,
                                 srmcfg_written, kernel, console_tail, logs }
in:  { cmd, timeout_s? }    -> { cmd, output: [lines] }
```
`linux_boot` reprograms the board (a Linux boot must start clean), loads the DTB and the OpenSBI + Linux payload
over JTAG (~3 min at ~110 KB/s; config.json `linux` names the bundle's rebuilt payload and the socket device tree,
`payload` / `dtb` override them), sets `a1` of every hart to the DTB's address (OpenSBI hands on the device tree it
finds there; the boot ROM leaves its own built-in one, which has no bootargs and no socket), optionally writes
`srmcfg` (CSR 0x181, exposed to GDB as `$csr385`) on the given harts before releasing them, and records the console
milestones. Harts 2-3 of this board cannot be halted once
running, so the CSR is written before release rather than at run time. `linux_cmd` types a command one character at
a time (the SoC UART overruns on bursts) and returns what the console captured up to the next prompt; busybox has
`devmem` and `hexdump` (`-s`/`-n` in decimal), `/dev/mem` reads of the OpenSBI region are PMP-protected. Only one
process may read the console: the tools kill a previous capture before starting their own.

## Software and navigation tools

### sw_build
```
in:  { test: name|dir, target?: str (build), extra_cflags?: str, clean?: bool }
out: { test, target, toolchain, elf, entry, tohost, size, checks, sections? (only with problems): [{name, addr, size, region}],
       problems, next }
err: unknown test (lists the available ones) | build failed {errors: [{file, line, msg}], log} | no ELF produced
```
Runs `make <target>` for a test (a name under he-soc `software/`, or any directory with a Makefile) with the
RISC-V toolchain and checks the ELF layout. A directory inside an alsaqr-software checkout is built with that
bundle's own toolchains (its rv64 gcc plus the rv32 `riscv-none-elf-*` shim the APMU firmware Makefiles expect),
so `target=pmu_bench` of `tests/pmu_mempol_synth` builds the Ibex firmware and embeds it. The ELF is
`<target>.riscv` (or `<test>.riscv`) and is copied to the tool's `bin/` directory as `<test>_<target>.riscv`.
`extra_cflags` is appended to every compiler call, e.g. `-DXSIM` for simulation-only code paths. A test directory
outside he-soc `software/` that has no bundled toolchains (e.g. an apmu-software checkout) is built with the
alsaqr-software checkout named by `toolchain_bundle` in `config.json`.

Running alsaqr-software's `pmu_mempol_synth` in the simulator: use `tests/pmu_mempol_synth_sim`
(`sw_build test=~/alsaqr-software/tests/pmu_mempol_synth_sim target=pmu_bench`). On the FPGA all four cores
are resumed by OpenOCD; in the simulation only core 0 is (core 1 is woken by the startup code, cores 2 and 3
sleep in the boot ROM until a PLIC interrupt reaches them), and the FPGA version's wall-clock delays and table
sizes would take hours of simulated time. The `_sim` variant builds the same sources with `-DXSIM` (wakes cores
2 and 3 the way he-soc's `quad_boot` does) and smaller workload knobs; the FPGA build is unchanged.

### soc_lookup
```
in:  { query }
out: address   -> { address, matches: [{name, base, end, offset, source}], nearest? }
     subsystem -> { windows: [{name, base, end, source}] }          e.g. UART, PLIC, mailbox, APMU, ISPM, DSPM
     module    -> { module: { instances: [{parent, instance, file, line, generate}], paths: [instance paths] } }
     struct    -> { typedef: { layouts: [[field, ...]], note? } }                  name or pkg::name
     other     -> { identifier: { hits: [{file, line, text, kind: declaration|conditional|use}], total, files, note },
                    compile_define? }
```
Address windows come from the SoC packages and the address-rule tables; instance paths follow the same
convention as `sim_session` (`tb/dut/i_host_domain/...`, generate loops as `label[i]`). Identifiers (signals,
parameters, `` `define `` macros) are whole-word matches over the compiled RTL, testbench and include headers,
declarations first; `compile_define` says when a macro is set on the compiler command line instead. The module
and typedef indexes are cached under the state directory and rebuilt when a compiled source changes.

### soc_bootflow
No input. What is simulated, the memory map, how a program is loaded and how each core starts, the markers
to expect in order with typical simulated times, and what to do when something goes wrong.
