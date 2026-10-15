# Implement a linked synthesized checkpoint of the AlSaqr VCU118 design (non-project mode) and gate the result.
# env: BUILD_DIR (outputs), SYNTH_DCP, OPT_DIR / PLACE_DIR / ROUTE_DIR (Vivado directives), SOC_MHZ (expected SoC clock),
#      PHASE_FILE (json updated by set_phase in fpga_build.sh; written here through `exec`)
# Lessons folded in (he-soc VCU118 flow, 2026-10): never run post-route phys_opt without a re-route (it leaves real hold
# violations behind); verify the MMCM really generates the SoC clock (a stale IP output once gave 20 MHz under a 50 MHz
# constraint); the only accepted hold failures are the adv_timer clock-as-data endpoints.
set out $::env(BUILD_DIR)
proc phase {p args} { exec bash $::env(SET_PHASE) $::env(PHASE_FILE) $p {*}$args }

open_checkpoint $::env(SYNTH_DCP)
phase opt
opt_design -directive $::env(OPT_DIR)
report_utilization -file $out/post_opt_util.rpt

phase place
place_design -directive $::env(PLACE_DIR)
report_utilization -file $out/post_place_util.rpt
report_timing_summary -file $out/post_place_timing_summary.rpt
write_checkpoint -force $out/post_place.dcp
set wns_place [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
phase route wns_place=$wns_place

route_design -directive $::env(ROUTE_DIR)
report_timing_summary -file $out/post_route_preopt_timing_summary.rpt
if {[get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]] < 0} {
  phase route_physopt
  phys_opt_design
  route_design -directive $::env(ROUTE_DIR)
}
write_checkpoint -force $out/post_route.dcp
report_route_status -file $out/post_route_status.rpt
report_timing_summary -file $out/post_route_timing_summary.rpt
report_drc -file $out/post_imp_drc.rpt

phase gate
set ns [llength [get_timing_paths -setup -max_paths 100000 -nworst 1 -slack_lesser_than 0]]
set bad {}
set nadv 0
foreach p [get_timing_paths -hold -max_paths 10000 -nworst 1 -slack_lesser_than 0] {
  set e [get_property ENDPOINT_PIN $p]
  if {[string match "*apb_adv_timer*u_in_stage/r_ls_clk_sync_reg*" $e]} { incr nadv } else { lappend bad "[get_property SLACK $p] $e" }
}
set wns [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
set whs [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -hold]]
set unrouted [llength [get_nets -hierarchical -filter {ROUTE_STATUS != ROUTED && ROUTE_STATUS != INTRASITE && ROUTE_STATUS != NOLOADS && ROUTE_STATUS != NODRIVER}]]
# soc_clk_mhz is what the MMCM really generates, computed from the cell's own attributes in the routed design (clk_out1
# is CLKOUT0); clk_constraint_mhz is the generated clock the timing analysis used. Both must equal the SoC clock.
set clk_constraint_mhz ""
foreach c [get_clocks -quiet clk_out1_xilinx_clk_mngr] { set clk_constraint_mhz [format %.3f [expr {1000.0 / [get_property PERIOD $c]}]] }
set clk_mhz ""
set mmcm_desc ""
foreach m [get_cells -hierarchical -quiet -filter {REF_NAME =~ MMCME4_ADV* && NAME =~ *alsaqr_clk_manager*}] {
  set in_ns [get_property CLKIN1_PERIOD $m]; set mult [get_property CLKFBOUT_MULT_F $m]
  set div [get_property DIVCLK_DIVIDE $m];   set odiv [get_property CLKOUT0_DIVIDE_F $m]
  set clk_mhz [format %.3f [expr {1000.0 / $in_ns * $mult / $div / $odiv}]]
  set mmcm_desc "[get_property NAME $m] CLKIN1_PERIOD=$in_ns CLKFBOUT_MULT_F=$mult DIVCLK_DIVIDE=$div CLKOUT0_DIVIDE_F=$odiv"
}
if {$clk_mhz eq ""} { set clk_mhz $clk_constraint_mhz; set mmcm_desc "no MMCME4_ADV cell found: soc_clk_mhz is the generated clock's period" }
set line "setup_failing=$ns hold_failing_outside_adv_timer=[llength $bad] hold_failing_adv_timer=$nadv wns=$wns whs=$whs unrouted=$unrouted soc_clk_mhz=$clk_mhz clk_constraint_mhz=$clk_constraint_mhz"
set f [open $out/gate.txt w]
puts $f $line
puts $f "mmcm $mmcm_desc"
foreach b [lrange $bad 0 20] { puts $f "  $b" }
close $f
phase bitstream setup_failing=$ns hold_failing_outside_adv_timer=[llength $bad] hold_failing_adv_timer=$nadv wns=$wns whs=$whs unrouted=$unrouted soc_clk_mhz=$clk_mhz clk_constraint_mhz=$clk_constraint_mhz "mmcm=$mmcm_desc"
write_bitstream -force $out/alsaqr_xilinx.bit
