#!/bin/bash
# Detached runner for one VCU118 bitstream build of the he-soc working tree (50 MHz, 4 cores, DDR4, LLC).
# Environment (set by fpga.py): BUILD_DIR FPGA_DIR SYNTH_DCP (reuse: skip synthesis) OPT_DIR PLACE_DIR ROUTE_DIR
#   SOC_PERIOD_NS VIVADO_SETTINGS FPGA_ENV (key=value lines of the in-tree flow: BOARD, XILINX_PART, MAIN_MEM, ...)
#   SCRIPTS (this directory)
# Phases (build.json): queued -> constr -> synth -> link -> opt -> place -> route [-> route_physopt] -> gate ->
#   bitstream -> done | failed
set -u
export SET_PHASE=$SCRIPTS/set_phase.sh PHASE_FILE=$BUILD_DIR/build.json
phase() { bash $SET_PHASE $PHASE_FILE "$@"; }
fail() { phase failed stage="$1" error="$2"; exit 1; }
source "$VIVADO_SETTINGS" > /dev/null 2>&1
while IFS='=' read -r k v; do [ -n "$k" ] && export "$k=$v"; done <<< "$FPGA_ENV"
export PROJECT_HOME=$FPGA_DIR/alsaqr
cd "$FPGA_DIR" || fail constr "no fpga dir $FPGA_DIR"

if [ -z "${SYNTH_DCP:-}" ]; then
  # ---------- constraints + project + synthesis (the in-tree run.tcl up to open_run), then one linked checkpoint ----------
  phase constr
  # the Vivado file list (fpga/alsaqr/tcl/generated/compile.tcl) is generated from Bender.yml: refresh it so a file
  # added to Bender.yml is synthesized (the Makefile only needs the tool variables to exist)
  ( cd "$HW_DIR" && RISCV=${RISCV:-/x} SW_HOME=${SW_HOME:-/x} HW_HOME=${HW_HOME:-/x} QUESTASIM_HOME=${QUESTASIM_HOME:-/x} \
    make $BENDER_MAKE ) > "$BUILD_DIR/bender.log" 2>&1 || fail constr "bender file list failed (bender.log)"
  make clean > "$BUILD_DIR/make_clean.log" 2>&1
  make constr > "$BUILD_DIR/make_constr.log" 2>&1 || fail constr "make constr failed (see make_constr.log)"
  # launch_runs synthesis does not see the Tcl variable used by constraints.xdc's create_clock: substitute the literal
  sed -i "s/\${SRC_CLK_PERIOD}/$SOC_PERIOD_NS/" timing_constr.xdc
  grep -q "create_clock -period $SOC_PERIOD_NS" timing_constr.xdc || fail constr "SoC clock period not in timing_constr.xdc"
  # a stale IP output next to the project (xilinx_clk_mngr.gen, 20 MHz) silently replaces the xci: never keep one
  rm -rf alsaqr/xilinx_clk_mngr.gen
  sed -n '1,/^open_run synth_1/p' run.tcl > "$BUILD_DIR/synth.tcl"
  cat >> "$BUILD_DIR/synth.tcl" <<TCL
update_compile_order -fileset sources_1
puts "CLOCKS: [join [get_clocks] { }]"
write_checkpoint -force $BUILD_DIR/synth_linked.dcp
close_project
TCL
  phase synth
  vivado -mode batch -source "$BUILD_DIR/synth.tcl" -nojournal -log "$BUILD_DIR/synth.log" > "$BUILD_DIR/synth.stdout" 2>&1 \
    || { grep -E "^ERROR" "$BUILD_DIR/synth.log" | head -5 > "$BUILD_DIR/errors.txt"; fail synth "vivado synthesis failed (errors.txt)"; }
  [ -f "$BUILD_DIR/synth_linked.dcp" ] || fail link "no linked checkpoint written"
  cp -f alsaqr/alsaqr.runs/synth_1/alsaqr_xilinx_utilization_synth.rpt "$BUILD_DIR/" 2>/dev/null
  SYNTH_DCP=$BUILD_DIR/synth_linked.dcp
  phase link synth_dcp="$SYNTH_DCP"
fi
export SYNTH_DCP

# ---------- implementation + gate + bitstream ----------
vivado -mode batch -source "$SCRIPTS/fpga_impl.tcl" -nojournal -log "$BUILD_DIR/impl.log" > "$BUILD_DIR/impl.stdout" 2>&1
rc=$?
grep -E "^ERROR|signals failed to route|Design is not legally routed" "$BUILD_DIR/impl.log" | head -5 > "$BUILD_DIR/errors.txt"
if [ $rc -ne 0 ] || [ ! -f "$BUILD_DIR/alsaqr_xilinx.bit" ]; then
  fail "$(python3 -c "import json;print(json.load(open('$PHASE_FILE')).get('phase','impl'))")" "vivado implementation failed (errors.txt)"
fi
g=$(head -1 "$BUILD_DIR/gate.txt")
want_mhz=$(python3 -c "print('%.3f' % (1000.0/$SOC_PERIOD_NS))")
verdict=FAIL
case "$g" in
  *"setup_failing=0 hold_failing_outside_adv_timer=0"*"unrouted=0"*"soc_clk_mhz=$want_mhz"*"clk_constraint_mhz=$want_mhz"*) verdict=OK ;;
esac
phase done verdict=$verdict bitstream="$BUILD_DIR/alsaqr_xilinx.bit"
