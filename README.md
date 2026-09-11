# alsaqr-mcp

MCP server with tools to help AI agents navigate the Alsaqr SoC project

The goal of this project is to have some bare bone tools to help any AI agent navigate, modify and simulate the platform.

This version ships twelve small tools that let an agent build a test, simulate the AlSaqr SoC (4× CVA6, CCU, SPU/APMU, LLC, L2,
debug module, PLIC, uDMA, …), watch the console, probe signals interactively, find out why a run stalls, patch RTL
as scratch overrides and recompile them. Dependency-free Python (stdio JSON-RPC MCP server + a CLI).

```
python3 -m alsaqr_mcp serve                          # MCP over stdio
python3 -m alsaqr_mcp list                           # tool schemas
python3 -m alsaqr_mcp sim_run binary=/path/x.riscv   # any tool from the CLI: key=value, JSON for lists
python3 -m alsaqr_mcp sim_session op=probe session_id=s1 'signals=["i_host_domain/i_axi_llc/slv_req_i"]'
```

Claude Code: `claude mcp add alsaqr -- python3 -m alsaqr_mcp serve` (run from this directory, or set `PYTHONPATH`).

## Backend and conventions

- Backend: the xsim build tree in `~/.cache/he-soc-simexp` (compiled library `xsim/w_I`, overrides
  `xsim/overrides` + `overrides_pad.map`, DPI library, RISC-V GCC 16). Paths live in `config.json`
  (`ALSAQR_MCP_CONFIG` overrides its location).
- Only the L3 boot flow: the ELF is linked with `test.ld` (`make build` / `sw_build`), entry 0x8000_0000 in the
  simulation DRAM behind the LLC, `.tohost` in L2. Core 0 is halted/resumed over JTAG; cores 1–3 are booted by
  the software (SCMI mailbox, APMU interrupts). L2-linked binaries are rejected with the build command to use.
- Times are simulated nanoseconds (`*_ns`). Outputs are compact JSON; raw logs stay on disk (paths returned).
  Errors are `{error, fix}`.
- One elaborated design per stamp (overrides + tb defines + xelab generics + DPI library + compiled RTL units,
  minus the units the testbench compile rewrites, recorded in `w_I/tb_units.txt`). Runs reuse it; a changed
  design is elaborated once (~4 min fast snapshot, ~5 min debug-visible snapshot for sessions/traces).
  Elaborations are serialised (`elab.lock`); launches on one snapshot are serialised while xsim rewrites the
  snapshot's `xsim_script.tcl` (`<snap>.start.lock`), otherwise simultaneous runs swap plusargs.
- Runs, sessions and traces are detached process groups executing a private copy of their runner script
  (bash reads scripts incrementally, so live runs must never see edits). Kills are by group, never by name.
- State on disk: `~/.cache/he-soc-simexp/{runs,sessions,traces}/<id>/`.
- The repository itself is never modified: every RTL/tb change is an override.

## A — simulation

### sim_run
```
in:  { binary: path, timeout_s?: int (7200), tag?: str }
out: { run_id, snapshot, elab: "reused"|"rebuilding", elab_note, entry, tohost, binary_size, checks: [str],
       timeout_s, runner_pid, uart: {tool: "sim_uart", args: {run_id}, file, note}, logs: {stdout, watchdog, state},
       next: [str] }
err: binary not found | elf unreadable | l2-linked binary | no tohost symbol | sections outside L2/L3 | no compiled library
```
Phases (`run.json`): queued → elab → running → finished | stalled | timeout | elab_failed | killed. The watchdog
kills a run after 180 s without a TICK heartbeat (one every 10 µs of simulated time).

### sim_status
```
in:  { run_id, since_ns?: int }
out: { run_id, state, elab, snapshot, binary, sim_time_ns, wall_s, rss_mb, sim_ns_per_wall_min,
       markers: [{t_ns, approx, kind: success|fail|fatal|finish|jtag|preload|error, text}], markers_truncated,
       uart_lines, uart_hint?, stall?: {at_ns, rss_growing, kind_guess, next}, exit?: {rc, verdict, reason?},
       elab_errors?, note? }
```
`approx: true` = time of the last heartbeat before the line. `kind_guess` is a hint (a ring can keep memory
flat); `sim_stall_trace` gives the verdict.

### sim_uart
```
in:  { run_id, since_line?: int, max_lines?: int (100, max 500), wait_s?: int (0, max 300) }
out: { run_id, state, lines: [{n, host_time, t_ns, uart, text}], total, more, next_since_line?, file, note? }
```
`t_ns` comes from a `$time` stamp in the mock-UART override. `wait_s` blocks until a new line, the run ends or
the time is up. Cores printing without a lock interleave characters exactly as in Questa.

### sim_kill
```
in:  { run_id?: str, session_id?: str, all?: bool }
out: { killed: [{run_id|session_id, result, processes?}] }
```

### sim_session
Persistent interactive xsim on the debug-visible snapshot; state is kept between calls.
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
Paths are relative to `dut` (`i_host_domain/i_axi_llc/slv_req_i`), `tb/...` for testbench objects, or absolute.
Values are hex. Struct values get named fields: the tuple's arity is matched against every `typedef struct
packed` in the RTL tree and each candidate is verified by reading its members (`get_value path.member`) and
comparing values positionally, so `fields` is exact whenever present. Unpacked arrays of structs come back as
`elements` in index order. `$finish` during an advance is reported; the session stays open for probing.

### sim_stall_trace
```
start: { run_id } | { binary, at_ns }, window_ns? (700), snapshot? -> { trace_id, state, start_ns, snapshot, elab, eta, next }
poll:  { trace_id } -> { state: queued|elab|locate|trace|done|no_stall|failed, stall_ns,
                         kind: comb_ring|deadlock_or_idle|unknown, events_in_window,
                         top_processes: [{instance, count, file, line}],
                         suggestion: {instance, file, repo_relative, override_path, block_line, block_end,
                                      written_vars, partner_blocks, fix} | {fix}, ptrace_log }
```
locate = step from `start_ns` in 500 ns increments until the simulator stops advancing; trace = re-run to
200 ns before that point with `ptrace on` for `window_ns` and attribute every process execution to its instance
and source line. A comb ring shows one `always_comb` executing thousands of times in 700 ns (normal activity:
a few hundred). The suggestion is the direct input for `rtl_fix_ring`.

## B — RTL overrides

### rtl_override
```
op=create { path }  -> { path, override_path, created, note }     copy repo file into the override tree + map
op=diff   { path }  -> { path, added, removed, lines, truncated }  override vs repo
op=show   { path }  -> { path, source: override|repo, file, lines }
op=revert { path }  -> { result }                                  override reset to the repo content
op=remove { path }  -> { result }                                  delete override + map entry
op=list   { filter? } -> { count, overrides: [{path, override_exists, differs}], map }
```
Paths are relative to `hardware/` or absolute (repo or override).

### rtl_recompile
```
in:  { files: [path], extra_defines?: [str] }
out: { compiled: [{file, source: override|repo, ok, log}], errors: [{file, line, msg}], note?, fix? }
```
Uses the exact include/define options of the original compile block (looked up in `compile.sh`), plus per-file
defines from `config.json` (`recompile_defines`, e.g. `host/host_domain.sv` → XSIM_SIM_DRAM). The next
run/session/trace elaborates a new snapshot.

### rtl_fix_ring
```
in:  { file, vars: [str], dry_run?: bool }
out: { file, override_path, override_created, dry_run, blocks_rewritten, shadows, warnings, audit, diff, diff_truncated, warning?, next? }
```
Assign-once rewrite: inside every `always_comb` that writes a listed variable, the writes go to a shadow
`<var>_xc` and the variable is assigned once at the block's end. Handles `always_comb (* attr *)`, ports and
internal variables (shadow declared after the original declaration), multi-line declarations, struct members
(`x.member` tails) and word boundaries (`go` vs `going`). The audit flags tails on variables the original never
writes and doubly driven structs; generate-scoped blocks get a warning.

## C — software and navigation

### sw_build
```
in:  { test: name|dir, extra_cflags?: str, clean?: bool }
out: { test, elf, entry, tohost, size, sections: [{name, addr, size, region}], checks, problems, next }
err: unknown test (lists the available ones) | build failed {errors: [{file, line, msg}], log}
```
`make build` with SW_HOME/HW_HOME and the GCC 16 compatibility flags (`uint` typedef header,
int-conversion / implicit-declaration downgraded), ELF copied to the binaries directory as `<test>_l3.riscv`.

### soc_lookup
```
in:  { query }  -- "0x..." address | module name | typedef name | any identifier
out: address -> { address, matches: [{name, base, end, offset, source}], nearest? }
     module  -> { module: { instances: [{parent, instance, file, line, generate}], paths: [static hier paths] } }
     typedef -> { typedef: { layouts: [[member, ...]] } }
     other   -> { identifier: { hits: [{file, line, text}] } }
```
Address windows come from the `*Base`/`*Length` constants in `include/*pkg*.sv` and the rule tables in
`host/*.sv`; hierarchy from a one-time scan of every file the flow compiles (cached). Static paths use the
same convention as sessions (`tb/dut/i_host_domain/...`, generate loops as `label[i]`).

### soc_bootflow
No input. Memory windows, the marker sequence with typical simulated times, how each core is woken, failure
signatures and what to call next. Read once before interpreting `sim_status` / `sim_uart`.

## Verification (2026-09-10, real xsim backend)

- A: rejections; elaboration reuse, serialisation and stamp stability; two simultaneous runs keeping their
  binaries; 40 s timeout leaving no processes; live status/uart during a run and after `$finish`; session
  open/advance/probe (named fields for AXI/ACE structs, dmcontrol, scoreboard-entry arrays), scope_list,
  force/release, wait, close; stall trace on a deliberately re-introduced dm_mem ring → comb_ring at 2016 µs,
  block dm_mem.sv:227, partner :137, the written variables.
- B: revert dm_mem to the ring version → `rtl_fix_ring` (2 blocks, 9 shadows, clean audit) → `rtl_recompile`
  → run to `[JTAG] SUCCESS`; injected syntax error reported as `{file, line, msg}`; create/diff/revert/remove/list.
- C: `sw_build` hello_culsans and quad_boot (layout checks, sections by region), unknown test lists candidates;
  `soc_lookup` for 0x10404000 (mailbox rule), 0x0C203004 (PLIC), 0x1A100000 (FLL/APB), dm_mem, cva6 (generate
  label), axi_llc_top, ccu_fsm, pmu_top, dmcontrol_t, irq_mbox_i; `soc_bootflow`.
- MCP stdio round trip: initialize, tools/list (12), tools/call, unknown-tool error.

Known limits: `kind_guess` in sim_status is a hint only; struct naming needs the typedef in the RTL tree;
declared scalar types are not reported (`describe` is silent in batch xsim); static hierarchy paths are
approximate inside generate blocks (a session's scope_list is exact); the first elaboration after a
`tb_units.txt` update can happen one extra time.
