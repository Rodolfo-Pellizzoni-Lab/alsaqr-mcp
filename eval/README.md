# alsaqr-mcp benchmark

Measures what the alsaqr MCP server saves an agent (tokens, cost, calls, wall time) and whether it changes the
quality of the result, by running the same tasks with and without it, and explains every run that loses points.

## Method

- Each run is a headless Claude Code session (`claude -p --model sonnet --effort high`, auto permission mode) in its
  own sandbox: a lean copy of the he-soc checkout (`sandbox.py`) whose every path points inside the sandbox
  (compile.sh, vsim.tcl, the MCP's compile records), with the current xsim snapshots symlinked under the sandbox's
  snapshot stamp. Runs never touch `~/he-soc`; `run_eval.py` checks the real checkouts before and after.
- Two conditions with identical prompts and environment notes: `mcp` (only the alsaqr server attached, and the
  prompt says it is connected) and `base` (no MCP server; the agent has the he-soc docs and scripts, e.g.
  `hardware/xsim/README.md` and `run.sh`). Both get the same tools otherwise (Bash, Read, Edit, ...).
- Metrics come from the stream-json transcript (`analyze.py`): tokens = input + cache write + cache read summed over
  API calls (deduplicated by message id), output tokens, peak context, tool calls, tool-result volume, the share of
  tokens spent on polling calls, cost (Claude Code's estimate) and wall time.
- Grading (`grade.py`): a separate Opus call per run scores the final answer and the recorded diff against the task
  rubric, checks claimed verification runs against the trace, and for every lost point names the cause
  (categories below) and any MCP-tool problem it saw.

## Tasks

Ground truth for every rubric was established with the tools on the same snapshots and checked against the RTL and
the boot ROM. Fixtures are in `fixtures/` (planted bugs are described in the task table, not in the fixture
names the agent sees).

| Task | Kind | What the agent must do | MCP tools it exercises |
|---|---|---|---|
| N1_mailbox | navigation | trace core 1's wake-up writes in the startup code to the SCMI mailbox window, its RTL module and instance path | soc_lookup (address, module), soc_bootflow |
| N2_llc_struct | navigation | type, field order and per-field widths of the LLC slave port (macro-generated AXI structs, a `define that crosses files) | soc_lookup (module, struct, identifier) |
| N3_cluster | navigation | the switch that removes the PULP cluster (set in two places), its window, and what answers in its place (error slave, response) | soc_lookup (identifier, compile_define, address, module) |
| N4_decode | navigation | decode five physical addresses to window, offset and register (SoC control, PLIC claim, APMU counter, CLINT, tohost area) | soc_lookup (address) |
| S1_quad_run | build + simulate | build quad_boot for DRAM, run it, report every console line with time and core, and the verdict | sw_build, sim_run, sim_status (wait) |
| S2_exit_code | build + simulate | run a self-test that fails; report verdict, the program's exit code and which checks failed | sw_build, sim_run, sim_status |
| R1_evu_run | external build + simulate | build apmu-software's evu_test (two-pass: rv32 Ibex firmware + rv64 program, from the alsaqr-software toolchain bundle) and report its counters and result | sw_build (bundle toolchains), sim_run, sim_status |
| P1_probe | simulate + probe | stop at 2.65 ms and read four cores' commit PCs and the LLC port's AR/AW channel | sim_session (open, advance, probe, scope_list) |
| P2_find_when | simulate + search | find to 4 µs when cores 2 and 3 leave the boot ROM and which interrupt line woke them | sim_session (stepping + probe) |
| D1_quad_sync | debug: software | a four-core rendezvous never finishes: core 3 is enabled on the wrong PLIC context (machine instead of supervisor; only the supervisor line wakes a core parked in wfi with mie = 0); fix and verify | sim_run, sim_status, soc_bootflow, sw_build |
| D2_rtl_stall | debug: RTL | the latest commit replaced riscv-dbg `dm_mem.sv` with the upstream version without the xsim ring rewrite; the simulation stalls at ~2.017 ms; find, fix (restore + recompile), verify | sim_status (stall), sim_stall_trace, rtl_changes, rtl_recompile |
| D3_fw_load | debug: firmware/hardware | evu_test's APMU firmware is loaded with memcpy; the ISPM indexes by byte address and ignores byte strobes, so the image's sub-word tail corrupts the last instruction and the firmware logs nothing; find, fix (word copies), verify | sw_build, sim_run, sim_session, soc_lookup |

Not covered yet: `sim_session force/release`, `rtl_changes op=revert`, and long workloads (pmu_mempol_synth, ~48 min
of wall clock per run).

## Failure categories (grader)

| Category | Meaning |
|---|---|
| wrong_tool_info | a tool (MCP or a repo script/doc) returned wrong or misleading information the agent followed |
| tool_failure | a tool errored, timed out or could not do what was needed, and the agent could not work around it |
| environment | sandbox/setup problem outside the agent's control (missing file, permission denial, quota) |
| agent_error | the agent misread data, reasoned wrongly or made a wrong change despite correct tool output |
| incomplete | rubric items left unaddressed or unverified although the agent had the means |
| timeout_or_budget | the run hit its wall-clock or budget limit |

`analyze.py` adds mechanical signals per run: tool errors and MCP `{error}` results per tool, permission denials,
polling share, `sleep` calls, simulations started, and commands that look like writes outside the sandbox.

## Results (2026-10-06, Sonnet 5.5, effort high, 3 runs per task and condition)

Tag `v2` (all tasks) with R1/D3 re-run as `v2b`: in `v2` a "15 minutes per simulation" rule plus 10 parallel agents
made evu_test (6.4 ms simulated, ~20 min on the loaded host) time out in both conditions; `v2b` allows 45 minutes and
runs 6 at a time, and its MCP runs include the fixes made after `v2` (case-sensitive `until`, 900 s wait cap,
sw_build error parsing, scope_list default scope).

| Task | Tokens MCP / base | Saved | Cost MCP / base | Wall s MCP / base | Score MCP / base |
|---|---|---|---|---|---|
| N1_mailbox | 217k / 311k | 30% | $0.16 / $0.23 | 39 / 54 | 4.00 / 4.00 of 4 |
| N2_llc_struct | 327k / 370k | 12% | $0.22 / $0.27 | 51 / 55 | 3.92 / 4.00 of 4 |
| N3_cluster | 875k / 2,509k | 65% | $0.42 / $1.00 | 683 / 1488 | 4.00 / 2.33 of 4 |
| N4_decode | 510k / 1,229k | 59% | $0.29 / $0.62 | 64 / 138 | 4.33 / 4.67 of 5 |
| S1_quad_run | 245k / 366k | 33% | $0.18 / $0.22 | 449 / 586 | 3.00 / 3.00 of 3 |
| S2_exit_code | 199k / 353k | 44% | $0.14 / $0.20 | 368 / 591 | 3.00 / 3.00 of 3 |
| R1_evu_run (v2b) | 283k / 1,022k | 72% | $0.19 / $0.45 | 840 / 942 | 3.00 / 3.00 of 3 |
| P1_probe | 310k / 564k | 45% | $0.19 / $0.31 | 423 / 773 | 3.00 / 3.00 of 3 |
| P2_find_when | 1,735k / 1,039k | -67% | $0.62 / $0.49 | 1060 / 802 | 3.00 / 3.00 of 3 |
| D1_quad_sync | 412k / 973k | 58% | $0.23 / $0.45 | 983 / 1101 | 3.50 / 3.00 of 3.5 |
| D2_rtl_stall | 876k / 495k | -77% | $0.38 / $0.26 | 816 / 1054 | 3.67 / 4.00 of 4 |
| D3_fw_load (v2b) | 4,011k / 8,666k | 54% | $1.53 / $2.90 | 2287 / 3958 | 4.00 / 4.00 of 4 |
| **All 12 (36 runs each)** | **30.0M / 53.7M** | **44%** | **$13.65 / $22.19** | **403 / 577 min** | **127.25 / 123.0 of 130.5** |

Tokens include cache reads (~93% of the total), so cost is the better weighted measure. Where the MCP loses:

- P2_find_when: `sim_session` can only step forward and probe; agents spend 35-50 calls bracketing an event (and
  reopen sessions when they overshoot), while the baseline writes one xsim Tcl loop. Needs an "advance until a
  signal condition" op that runs the loop inside xsim.
- D2_rtl_stall: the median is close to the baseline; the mean is driven by the one run that used `sim_stall_trace`,
  which classifies the dm_mem combinational-ring stall as "idle" (the spinning ring produces almost no ptrace
  lines) and points at handshakes: 1.8M tokens and a lost point.
- N4_decode: `soc_lookup` gives window and offset but no register names, so agents still grep the register packages.

Before the round-2 changes (round 1, 5 tasks), build+run cost the MCP 25% *more* tokens than the baseline because
`sim_uart` returned after every console line (polling was 55-73% of the tokens), and every MCP run on the quad_sync
bug repeated `soc_bootflow`'s wrong "context 2*hart+1 = machine mode". The blocking `sim_status` wait and the
corrected PLIC text turned both around (build+run -33%, quad_sync 1.07M -> 412k tokens and full marks).

## Running

```
python3 eval/run_eval.py --tag v2 --jobs 10 --reps 3          # all tasks, both conditions (~3-4 h, ~70 runs)
python3 eval/run_eval.py --tag v2 --tasks D2_rtl_stall --reps 1 --conds mcp
python3 eval/grade.py v2                                        # one Opus grading call per run
python3 eval/report.py v2                                       # tables, lost points by cause, MCP issues
```

Results, transcripts and per-run diffs go to `$ALSAQR_EVAL_DIR/results/<tag>/` (default `~/.cache/alsaqr-mcp-eval`).
The runner stops launching runs when the subscription's five-hour window is 70 % used; rerun the same command later
to fill in the missing runs. Data the tasks need outside this repository:

- `$ALSAQR_EVAL_DIR/quad_boot.riscv`: quad_boot built by `sw_build test=quad_boot` (P1/P2 ground truth is for this
  layout: .text at 0x80000108, 200392 bytes).
- `$ALSAQR_EVAL_DIR/templates/d2_stall`: a sandbox with the planted `dm_mem.sv` committed, recompiled and both
  snapshots elaborated. Recreate: `python3 eval/sandbox.py <dir>`, write
  `git -C ~/he-soc show 3d9e5289:hardware/ip_list/riscv-dbg/src/dm_mem.sv` over the file, commit it, `rtl_recompile`
  it, then `sim_run` (normal snapshot) and `sim_session op=open` (debug snapshot) once.
- `~/alsaqr-software-github` (apmu-software) and the toolchain bundle `~/alsaqr-software`.
