# Program the first xcvu9p found on the local hw_server with $::env(BITSTREAM) (starts hw_server if needed).
open_hw_manager
connect_hw_server -url localhost:3121 -allow_non_jtag
open_hw_target [lindex [get_hw_targets] 0]
set d [lindex [get_hw_devices xcvu9p_0] 0]
current_hw_device $d
set_property PROGRAM.FILE $::env(BITSTREAM) $d
set_property PROBES.FILE {} $d
set_property FULL_PROBES.FILE {} $d
set t0 [clock seconds]
program_hw_devices $d
refresh_hw_device -update_hw_probes false $d
puts "PROGRAMMED in [expr {[clock seconds]-$t0}] s"
close_hw_target
disconnect_hw_server
close_hw_manager
