"""Benchmark tasks: what an agent is asked, what its sandbox contains, and how its answer is graded.

Every task runs twice per repetition: with the alsaqr MCP server attached ("mcp") and without ("base"). Both get
the same prompt and environment notes; the MCP run is told the server is connected. {SB} is the run's sandbox.
Rubric items are (points, criterion); ground truth was established with the tools and checked against the RTL.
"""

ENV = """Environment
- The AlSaqr SoC repository (he-soc, branch xsim-port) is checked out at {SB}/he-soc. Its RTL is simulated with \
Vivado xsim; the flow and its documentation are in {SB}/he-soc/hardware/xsim (README.md). The simulator library is \
already compiled.
- RISC-V GCC: /home/mhassen/.cache/he-soc-simexp/gcc2/riscv/bin (riscv64-unknown-elf-*). Vivado 2024.2: \
/tools/Xilinx/Vivado/2024.2/settings64.sh.
{EXTRA}{MCP_LINE}- Rules: only create or modify files inside {SB} (use {SB}/scratch for scratch files). Do not \
rebuild the whole simulator library (build.sh). Stop only processes you started yourself, by PID; never use pkill or \
killall. Keep every single simulation under {SIM_LIMIT} minutes of wall clock. Do not ask questions: work autonomously and end \
with a concise final answer.
"""
MCP_LINE = "- The alsaqr MCP server (tools mcp__alsaqr__*) is connected and operates on this checkout.\n"
APMU_LINE = ("- apmu-software (tests for the APMU, whose firmware runs on an Ibex core) is checked out at "
             "{SB}/apmu-software. Its tests are built with the toolchains of the alsaqr-software bundle at "
             "/home/mhassen/alsaqr-software (read only: `source.sh` there puts its rv64 GCC, its rv32 GCC and the "
             "riscv-none-elf-* shim the firmware Makefiles call on PATH).\n")

# fixtures: (kind, source, destination in the sandbox)
#   "file"/"dir": copy from eval/fixtures (or an absolute path); "quad_support": the support files quad_boot.c uses;
#   "patch": apply a patch (-p1) inside a directory of the sandbox
QUAD_SUPPORT = ["Makefile", "LICENSE", "pmu_defines.h", "pmu_test_func.c", "pmu_test_func.h",
                "riscv_encoder_instr.c", "riscv_encoder_instr.h"]

TASKS = {
    # ------------------------------------------------------------------ static navigation (no simulation)
    "N1_mailbox": {
        "kind": "navigation", "timeout_s": 1800, "tools": ["soc_lookup", "soc_bootflow"],
        "prompt": """Task: the startup code that every test program links (he-soc software/common) wakes core 1 \
through a mailbox. Find:
1. the address(es) it writes for this and what each write does;
2. which window of the SoC address map those addresses fall in (name, base and end address) and where in the RTL \
that window is defined;
3. the RTL module that implements that mailbox and its full hierarchical instance path in the simulated design \
(below the testbench top, e.g. dut/...).""",
        "rubric": [
            (1, "writes 0x10404000 <- 0x80000000 (word 0 / jump address for core 1) and 0x10404024 <- 1 (raises "
                "the completion/doorbell interrupt that wakes core 1); the PLIC setup writes are optional"),
            (1, "window: SCMI mailbox 0x1040_4000 - 0x1040_5000 (inside the AXI-Lite window 0x1040_0000 - "
                "0x1070_0000); naming only the AXI-Lite window without the 4 KiB SCMI slot earns 0.5"),
            (1, "defined as rule idx 4 of the AXI-Lite crossbar address map in hardware/host/axi_lite_subsystem.sv "
                "(AXILite base/length in include/ariane_soc_pkg.sv)"),
            (1, "module axi_lite_scmi_mailbox, path dut/i_host_domain/i_axi_lite_subsystem/i_scmi_ot_mailbox"),
        ],
    },
    "N2_llc_struct": {
        "kind": "navigation", "timeout_s": 1800, "tools": ["soc_lookup"],
        "prompt": """Task: in the simulated design the last-level cache is the instance dut/i_host_domain/i_axi_llc. \
For its slave-port input slv_req_i, report:
1. its SystemVerilog type (package::name) and the file that declares it;
2. its top-level fields in declaration order;
3. the fields of its AW-channel struct in declaration order;
4. the bit widths of aw.id, aw.addr, aw.user and w.data on this port, and which parameters/defines determine \
each of them in this build.""",
        "rubric": [
            (1, "type ariane_axi_soc::req_slv_t declared in hardware/include/ariane_axi_soc_pkg.sv"),
            (1, "top-level fields: aw, aw_valid, w, w_valid, b_ready, ar, ar_valid, r_ready"),
            (1, "AW (aw_chan_slv_t): id, addr, len, size, burst, lock, cache, prot, qos, region, atop, user"),
            (1, "widths 0.25 each: aw.id 8 (IdWidthSlave = 4 + clog2(NrSlaves = 10)), aw.addr 64, aw.user 2 "
                "(QUAD_CORE `define in ariane_soc_pkg.sv, visible in the same xvlog call), w.data 64"),
        ],
    },
    "N3_cluster": {
        "kind": "navigation", "timeout_s": 1800, "tools": ["soc_lookup"],
        "prompt": """Task: the PULP cluster is not part of the simulated SoC.
1. Which compile-time switch removes it, and where is that switch set for this xsim build (every place)?
2. What is the cluster's address window (base and end)?
3. What answers a CPU load from that window in the simulated design: the module, its instance name and full \
hierarchical path, and what the load gets back (data and AXI response)?""",
        "rubric": [
            (1, "EXCLUDE_CLUSTER, set by `define at hardware/host/al_saqr.sv line 15 AND by -d on the compile "
                "command line from SOC_DEFINES in hardware/xsim/env.sh (0.5 for only one of the two)"),
            (1, "window 0x1000_0000 - 0x1040_0000 (ClusterBase, ClusterLength 0x40_0000 in ariane_soc_pkg.sv)"),
            (1, "axi_err_slv instance clusternotimplemented in al_saqr, path dut/clusternotimplemented"),
            (1, "returns DECERR (axi_err_slv default Resp) with read data 0xdeadbeefdeadbeef (RespData)"),
        ],
    },
    "N4_decode": {
        "kind": "navigation", "timeout_s": 1800, "tools": ["soc_lookup"],
        "prompt": """Task: for each of these physical addresses, name the device or address window it falls in \
(with its base address), the offset inside it, and the register or variable at that offset:
0x1A10601C, 0x0C207004, 0x10407008, 0x0200BFF8, 0x1C000040.""",
        "rubric": [
            (1, "0x1A10601C: SoC control (SOCCTRL, base 0x1A10_6000), offset 0x1C = LLC_CACHE_ADDR_END register "
                "(end of the LLC-cached range)"),
            (1, "0x0C207004: PLIC (base 0x0C00_0000), offset 0x20_7004 = claim/complete register of context 7 "
                "(hart 3, supervisor context); calling context 7 hart 3's machine-mode context earns 0.5"),
            (1, "0x10407008: APMU (window base 0x1040_5000), counter bundle 0 (COUNTER_B_BASE 0x1040_7000) "
                "register 2 = EVENT_INFO of counter 0"),
            (1, "0x0200BFF8: CLINT (base 0x0200_0000), offset 0xBFF8 = mtime"),
            (1, "0x1C000040: on-chip SRAM / L2SPM (base 0x1C00_0000), offset 0x40 = fromhost (tohost is at 0x0)"),
        ],
    },
    # ------------------------------------------------------------------ build + simulate
    "S1_quad_run": {
        "kind": "build+simulate", "timeout_s": 3600, "tools": ["sw_build", "sim_run", "sim_status", "sim_uart"],
        "prompt": """Task: build the test he-soc/software/quad_boot so that it runs from the simulated DRAM \
(entry 0x80000000), simulate it in xsim, and report:
1. the ELF's entry point and the address of its tohost symbol;
2. every console (UART) line the program printed, with its simulated time, and which core printed it;
3. the simulated time of the [JTAG] SUCCESS (or FAILED) marker and the final verdict.""",
        "rubric": [
            (1, "entry 0x80000000, tohost 0x1c000000"),
            (1, "7 UART lines from 2.581 to 2.765 ms: core0 'Hello from Core0', core1 'Hello from Core1', core0 "
                "'PLIC Configured', core0 'PMU Interrupt Raised - Counter0!', then core0's '...Counter1!' "
                "interleaved with core2's 'Hello from Core2', and core3's 'Hello from Core3' (~2.74-2.77 ms)"),
            (1, "[JTAG] SUCCESS at ~2.84 ms ($finish at 2,844,500 ns), verdict success"),
        ],
    },
    "S2_exit_code": {
        "kind": "build+simulate", "timeout_s": 3600, "tools": ["sw_build", "sim_run", "sim_status"],
        "fixtures": [("dir", "exit_check", "he-soc/software/exit_check")],
        "prompt": """Task: build the test he-soc/software/exit_check for the simulated DRAM, run it in xsim and \
report the verdict, the exit code the program returned, and which of its checks failed.""",
        "rubric": [
            (1, "verdict FAILED ([JTAG] FAILED at ~2.84 ms)"),
            (1, "exit code 2 (the testbench prints 'FAILED: return code 2'; tohost = 5)"),
            (1, "failing checks: check 2 fib(20) and check 5 fib(10)*3; checks 1, 3, 4 ok"),
        ],
    },
    "R1_evu_run": {
        "kind": "external build+simulate", "timeout_s": 5400, "sim_limit_min": 45, "tools": ["sw_build", "sim_run", "sim_status"],
        "extra_env": APMU_LINE,
        "fixtures": [("dir", "~/alsaqr-software-github", "apmu-software")],
        "prompt": """Task: build the test evu_test of the apmu-software checkout for the simulator (it embeds \
firmware for the APMU's Ibex core; see its Makefile) and run it in xsim. Report the test's final result line, the \
EVU counter values for its entry and exit milestones, how many calls the APMU firmware logged, and the simulated \
time at which the testbench reported the verdict.""",
        "rubric": [
            (1, "built (two-pass: the Ibex firmware with the rv32 toolchain, then the rv64 program) and simulated; "
                "result line '=== EVU TEST PASS (0 failures) ==='"),
            (1, "counter 20 (entry milestone) 0 before / 16 after; counter 21 (exit milestone) 0 / 16"),
            (1, "firmware logged 16 calls (9908 cycles inside evu_probe); [JTAG] SUCCESS at ~6.43 ms "
                "(accept 6.2-6.7 ms)"),
        ],
    },
    # ------------------------------------------------------------------ interactive signal access
    "P1_probe": {
        "kind": "simulate+probe", "timeout_s": 3600, "tools": ["sim_session"],
        "fixtures": [("file", "data:quad_boot.riscv", "quad_boot.riscv")],
        "prompt": """Task: simulate the prebuilt program {SB}/quad_boot.riscv (built for the simulated DRAM, entry \
0x80000000) and stop it at simulated time 2,650,000 ns (2.65 ms). At that instant, report:
1. the commit-stage PC (signal pc_commit) of each of the four CVA6 cores, and for each whether it is executing from \
the boot ROM or from DRAM;
2. on the last-level cache's slave port (input slv_req_i of the instance dut/i_host_domain/i_axi_llc): the values \
of ar_valid, ar.addr and aw_valid.""",
        "rubric": [
            (1, "pc_commit core0 0x8000a3e8, core1 0x80000766, core2 0x10790, core3 0x10790"),
            (1, "cores 0 and 1 in DRAM, cores 2 and 3 in the boot ROM"),
            (1, "LLC slv_req_i: ar_valid 0, ar.addr 0x8000a3e0, aw_valid 0"),
        ],
    },
    "P2_find_when": {
        "kind": "simulate+search", "timeout_s": 3600, "tools": ["sim_session"],
        "fixtures": [("file", "data:quad_boot.riscv", "quad_boot.riscv")],
        "prompt": """Task: simulate the prebuilt program {SB}/quad_boot.riscv (built for the simulated DRAM). Cores 2 \
and 3 start parked in the boot ROM and are woken by interrupts during the run. For each of them find, to within \
4 microseconds of simulated time, when it first commits an instruction from DRAM (pc_commit >= 0x80000000), and \
which interrupt input of the core woke it (signal name and bit).""",
        "rubric": [
            (1, "core 2 first commits from DRAM between 2.654 and 2.656 ms (accept 2.650-2.660 ms)"),
            (1, "core 3 first commits from DRAM between 2.688 and 2.690 ms (accept 2.684-2.694 ms)"),
            (1, "woken by irq_i[1] (bit 1 of irq_i, the supervisor external interrupt line from PLIC context "
                "2*hart+1), seen high at ~2.652 ms (core 2) and ~2.682 ms (core 3); naming irq_i[0]/machine "
                "external interrupt earns 0"),
        ],
    },
    # ------------------------------------------------------------------ debugging
    "D1_quad_sync": {
        "kind": "debug: software", "timeout_s": 3600, "tools": ["sw_build", "sim_run", "sim_status", "soc_bootflow"],
        "fixtures": [("quad_support", "", "he-soc/software/quad_sync"),
                     ("file", "quad_sync/quad_sync.c", "he-soc/software/quad_sync/quad_sync.c"),
                     ("file", "quad_sync/Makefile", "he-soc/software/quad_sync/Makefile")],
        "prompt": """Task: he-soc/software/quad_sync is a four-core test: core 0 wakes cores 2 and 3, every core \
checks in, and core 0 should then exit with code 0 so that the testbench prints [JTAG] SUCCESS. In the xsim \
simulation it never finishes. Find the root cause, fix it in the test's source, and show that the fixed program \
reaches [JTAG] SUCCESS in simulation. Report the root cause and the evidence for it, the change you made, and the \
simulated time of [JTAG] SUCCESS after the fix.""",
        "rubric": [
            (1, "root cause: m_ctx[3] = 6 must be 7. Context 2h is machine mode (irq_i[0]), 2h+1 supervisor "
                "(irq_i[1]); the boot ROM parks cores in wfi with mie = 0 and only irq_i[1] wakes wfi, so source 157 "
                "enabled in context 6 never wakes core 3. 0.5 if the wrong entry is found but the mechanism is "
                "stated wrongly (e.g. '2h+1 is the machine-mode context')"),
            (0.5, "evidence: cores 0-2 check in, core 3 never does; core 0 spins"),
            (1, "fix m_ctx = {1, 3, 5, 7}"),
            (1, "verified: [JTAG] SUCCESS at ~3.00 ms ($finish 3,000,500 ns)"),
        ],
    },
    "D2_rtl_stall": {
        "kind": "debug: RTL stall", "timeout_s": 5400,
        "tools": ["sim_run", "sim_status", "sim_stall_trace", "rtl_changes", "rtl_recompile"],
        "template": "d2_stall",
        "prompt": """Task: after the most recent change to the RTL, the simulation of he-soc/software/quad_boot no \
longer finishes: it stops making progress partway through. Find the cause, fix the RTL, and show that quad_boot \
reaches [JTAG] SUCCESS again. Report what stalls and why, the fix, and the evidence.""",
        "rubric": [
            (1, "the stall: simulated time stops at ~2.01 ms, while the testbench halts hart 0 over JTAG "
                "(after 'haltreq written, polling DMStatus'); the simulator spins inside one time step"),
            (1, "cause: the last commit replaced riscv-dbg dm_mem.sv with the version without the xsim rewrite: its "
                "always_comb p_hart_ctrl_queue (default-then-override writes of cmdbusy_o, go, resume, "
                "cmderror_*) forms a combinational ring with dm_csrs that xsim re-evaluates forever (README section 2)"),
            (1, "fix: restore the xsim version of dm_mem.sv (git revert / checkout of the previous version, or "
                "tools/comb_fix.py) and recompile it into the library"),
            (1, "verified: quad_boot reaches [JTAG] SUCCESS (~2.84 ms) after the fix"),
        ],
    },
    "D3_fw_load": {
        "kind": "debug: firmware/hardware", "timeout_s": 7200, "sim_limit_min": 45,
        "tools": ["sw_build", "sim_run", "sim_status", "sim_session", "soc_lookup"],
        "extra_env": APMU_LINE,
        "fixtures": [("dir", "~/alsaqr-software-github", "apmu-software"),
                     ("patch", "evu_test_memcpy.patch", "apmu-software")],
        "prompt": """Task: {SB}/apmu-software/evu_test is a smoke test of the APMU's EVU PC-milestone counters, with \
firmware running on the APMU's Ibex core that logs every call it sees. In the xsim simulation the test reports \
FAIL. Find the root cause, fix it, and show that the test passes in simulation. Report the root cause and the \
evidence for it, the change you made, and the final result line.""",
        "rubric": [
            (1, "symptom and evidence: EVU counters 20/21 count 16/16 (the hardware milestones work) but the "
                "firmware logs 0 calls and its breadcrumb stays at 2: the firmware never sees an entry; "
                "FAILED: return code 1"),
            (1.5, "root cause: the firmware image is copied into the APMU ISPM with memcpy; the ISPM "
                  "(ip_list/apmu/src/pmu_ispm.sv) indexes its SRAM by byte address and does not merge sub-word "
                  "writes by strobe, so memcpy's byte/half-word stores for the tail of the 2292-byte image (not a "
                  "multiple of 8) land in the wrong entries and corrupt the last instruction. 0.5 for 'memcpy / "
                  "sub-word stores corrupt the firmware' without the ISPM mechanism"),
            (1, "fix: copy the firmware (ISPM, and DSPM data) with 32-bit word stores"),
            (0.5, "verified: '=== EVU TEST PASS (0 failures) ===' and [JTAG] SUCCESS (~6.4 ms)"),
        ],
    },
}

def prompt(task: str, cond: str, sb: str) -> str:
    t = TASKS[task]
    env = (ENV.replace("{MCP_LINE}", MCP_LINE if cond == "mcp" else "").replace("{EXTRA}", t.get("extra_env", ""))
           .replace("{SIM_LIMIT}", str(t.get("sim_limit_min", 15))))
    return (t["prompt"] + "\n\n" + env).replace("{SB}", sb)
