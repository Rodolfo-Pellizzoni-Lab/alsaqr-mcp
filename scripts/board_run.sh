#!/bin/bash
# Run one bare-metal ELF on the programmed VCU118 over JTAG and capture its console (synchronous, ~15-40 s).
# Environment (set by board.py): RUN_DIR ELF BUNDLE (alsaqr-software checkout: openocd + toolchain + source.sh)
#   UART BAUD SLEEP_MS (time the program gets) PEEKS ("addr:count ..." 64-bit words read over the system bus
#   after the halt) RELEASE_HARTS (1: release harts 2 and 3 through the debug module, as the board flow needs)
#   OPENOCD_CORES (haltable harts for OpenOCD's smp group; 2 on this board) SCRIPTS
# Results: run.json (phase + tohost + test line), uart.log, gdb.log
set -u
export SET_PHASE=$SCRIPTS/set_phase.sh PHASE_FILE=$RUN_DIR/run.json
phase() { bash $SET_PHASE $PHASE_FILE "$@"; }
fail() { phase failed stage="$1" error="$2"; exit 1; }
source "$BUNDLE/source.sh" > /dev/null 2>&1
export OPENOCD_SCRIPTS=$BUNDLE/openocd/tcl
GDB=$BUNDLE/toolchain/rv64/bin/riscv64-unknown-elf-gdb; [ -x "$GDB" ] || GDB=riscv64-unknown-elf-gdb
READELF=$BUNDLE/toolchain/rv64/bin/riscv64-unknown-elf-readelf; [ -x "$READELF" ] || READELF=riscv64-unknown-elf-readelf

phase openocd
# a fresh OpenOCD for every run: a previous session can leave the debug module in a state a new one cannot examine
start_openocd() {
  pkill -9 -x openocd 2>/dev/null; sleep 1
  nohup "$BUNDLE/openocd/bin/openocd" -s "$BUNDLE/openocd" -c "adapter speed 2000" -c "set _INTERFACE ftdi" \
    -c "set _DEVICE olimex-arm-usb-ocd-h" -c "gdb_port 6699" -c "tcl_port disabled" -c "telnet_port disabled" \
    -c "set _NUM_CORES $OPENOCD_CORES" -c "set _TARGET smp" -f openocd.cfg -c "init" > "$RUN_DIR/openocd.log" 2>&1 &
  for i in $(seq 1 40); do sleep 1; grep -qE "Listening on port 6699|Fatal|failed to halt during examine|IR capture error" "$RUN_DIR/openocd.log" 2>/dev/null && break; done
}
start_openocd
if grep -q "IR capture error" "$RUN_DIR/openocd.log"; then   # transient right after programming: the chain needs a moment
  sleep 4; start_openocd
fi
if grep -qE "failed to halt during examine|Fatal" "$RUN_DIR/openocd.log"; then
  pkill -9 -x openocd 2>/dev/null
  fail unhaltable "harts cannot be halted (left hung by an earlier run): the board must be reprogrammed"
fi
grep -q "Listening on port 6699" "$RUN_DIR/openocd.log" || { pkill -9 -x openocd 2>/dev/null; fail openocd "OpenOCD did not come up (openocd.log)"; }

# .sbss/.bss are not cleared by the startup code: zero them from GDB (DDR holds garbage after programming)
ZS=0; ZE=0
while read -r a z; do a=$((16#$a)); z=$((16#$z)); [ $z -eq 0 ] && continue
  if [ $ZS -eq 0 ] || [ $a -lt $ZS ]; then ZS=$a; fi; if [ $((a+z)) -gt $ZE ]; then ZE=$((a+z)); fi
done < <($READELF -S -W "$ELF" | awk '$2 ~ /^\.s?bss$/ {print $4, $6}')
[ $ZE -gt $ZS ] && head -c $((ZE-ZS)) /dev/zero > "$RUN_DIR/bss_zero.bin"

phase run
pkill -f "scripts/uart_ca[p].py" 2>/dev/null; sleep 0.3   # a second reader would split the console bytes
stty -F "$UART" "$BAUD" raw -echo -crtscts 2>/dev/null
python3 "$SCRIPTS/uart_cap.py" "$UART" "$BAUD" $((SLEEP_MS/1000 + 30)) "$RUN_DIR/uart.log" > /dev/null 2>&1 &
CAP=$!
{
  cat <<G
set pagination off
set confirm off
set remotetimeout 60
target extended-remote :6699
monitor reset halt
load
G
  [ $ZE -gt $ZS ] && echo "restore $RUN_DIR/bss_zero.bin binary $(printf '0x%x' $ZS)"
  if [ "${RELEASE_HARTS:-1}" = 1 ]; then cat <<G
monitor riscv dmi_write 0x10 0x40020001
monitor riscv dmi_write 0x10 0x00020001
monitor riscv dmi_write 0x10 0x40030001
monitor riscv dmi_write 0x10 0x00030001
monitor riscv dmi_write 0x10 0x00000001
monitor sleep 200
G
  fi
  cat <<G
monitor resume
monitor sleep $SLEEP_MS
monitor halt
monitor targets riscv.cpu0
monitor reg pc
monitor riscv set_mem_access sysbus
echo TOHOST\\n
monitor read_memory 0x1C000000 64 1
G
  for ac in ${PEEKS:-}; do echo "echo PEEK ${ac%%:*}\\n"; echo "monitor read_memory ${ac%%:*} 64 ${ac##*:}"; done
  cat <<G
monitor riscv set_mem_access progbuf sysbus abstract
monitor reset halt
quit
G
} > "$RUN_DIR/run.gdb"
"$GDB" -batch -x "$RUN_DIR/run.gdb" "$ELF" > "$RUN_DIR/gdb.log" 2>&1
sleep 1; kill $CAP 2>/dev/null; wait $CAP 2>/dev/null
tohost=$(awk '/^TOHOST/{f=1;next} f && /^0x/{print $1; exit}' "$RUN_DIR/gdb.log")
line=$(tr -d '\r' < "$RUN_DIR/uart.log" | grep -E 'TEST (PASS|FAIL)|TRAP|SUCCESS|FAILED' | tail -1)
phase done tohost="${tohost:-?}" test_line="$line"
