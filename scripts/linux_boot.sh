#!/bin/bash
# Detached runner: boot the prebuilt Linux (OpenSBI fw_payload + DTB) on the programmed VCU118 over JTAG, keep the
# console captured, and record boot milestones. Environment (set by board.py): BOOT_DIR BUNDLE PAYLOAD DTB DTB_ADDR
#   UART BAUD UART_SECS NUM_HARTS SRMCFG_LIST ("hart:value ..." QoS tags written through GDB before release) SCRIPTS
# Phases (boot.json): openocd -> loading -> released -> opensbi -> kernel -> shell | failed
set -u
export SET_PHASE=$SCRIPTS/set_phase.sh PHASE_FILE=$BOOT_DIR/boot.json
phase() { bash $SET_PHASE $PHASE_FILE "$@"; }
fail() { phase failed stage="$1" error="$2"; exit 1; }
source "$BUNDLE/source.sh" > /dev/null 2>&1
export OPENOCD_SCRIPTS=$BUNDLE/openocd/tcl
GDB=$BUNDLE/toolchain/rv64/bin/riscv64-unknown-elf-gdb; [ -x "$GDB" ] || GDB=riscv64-unknown-elf-gdb

phase openocd
pkill -9 -x openocd 2>/dev/null; pkill -f "scripts/uart_ca[p].py" 2>/dev/null; sleep 2   # one console reader at a time
nohup "$BUNDLE/openocd/bin/openocd" -s "$BUNDLE/openocd" -c "adapter speed 2000" -c "set _INTERFACE ftdi" \
  -c "set _DEVICE olimex-arm-usb-ocd-h" -c "gdb_port 6699" -c "tcl_port disabled" -c "telnet_port disabled" \
  -c "set _NUM_CORES $NUM_HARTS" -c "set _TARGET smp" -f openocd.cfg \
  -c "foreach t [target names] { targets \$t; riscv expose_csrs 385 }" -c "init" > "$BOOT_DIR/openocd.log" 2>&1 &
for i in $(seq 1 60); do sleep 1; grep -q "Listening on port 6699" "$BOOT_DIR/openocd.log" 2>/dev/null && break; done
grep -q "Listening on port 6699" "$BOOT_DIR/openocd.log" || fail openocd "OpenOCD did not come up (openocd.log)"

stty -F "$UART" "$BAUD" raw -echo -crtscts 2>/dev/null
rm -f "$BOOT_DIR/uart.log"
nohup python3 "$SCRIPTS/uart_cap.py" "$UART" "$BAUD" "$UART_SECS" "$BOOT_DIR/uart.log" > /dev/null 2>&1 &
echo $! > "$BOOT_DIR/uart_cap.pid"

phase loading
{
  cat <<G
set pagination off
set confirm off
set remotetimeout 120
target extended-remote :6699
monitor load_image $DTB $DTB_ADDR
G
  for h in $(seq 1 "$NUM_HARTS"); do echo "thread $h"; echo "set \$pc=_start"; echo "set \$a1=$DTB_ADDR"; done   # a1 = the FDT OpenSBI hands on: the boot ROM left its own built-in DTB there
  for hv in ${SRMCFG_LIST:-}; do h=${hv%%:*}; v=${hv##*:}; echo "thread $((h+1))"; echo "set \$csr385=$v"; echo "echo SRMCFG hart $h: \\n"; echo "p/x \$csr385"; done
  cat <<G
load
monitor resume
quit
G
} > "$BOOT_DIR/boot.gdb"
"$GDB" -batch -x "$BOOT_DIR/boot.gdb" "$PAYLOAD" > "$BOOT_DIR/gdb.log" 2>&1
grep -q "Transfer rate" "$BOOT_DIR/gdb.log" || fail loading "payload load failed (gdb.log)"
phase released

# ---------- milestones from the console ----------
t0=$(date +%s)
seen_sbi=0; seen_kernel=0
for i in $(seq 1 $((UART_SECS/2))); do
  sleep 2
  u=$(tr -d '\r' < "$BOOT_DIR/uart.log" 2>/dev/null)
  if [ $seen_sbi = 0 ] && grep -q "OpenSBI v" <<< "$u"; then seen_sbi=1; phase opensbi t_opensbi_s=$(( $(date +%s) - t0 )); fi
  if [ $seen_kernel = 0 ] && grep -q "Linux version" <<< "$u"; then seen_kernel=1; phase kernel t_kernel_s=$(( $(date +%s) - t0 )); fi
  if grep -qE "built-in shell|login:" <<< "$u"; then sleep 6; phase shell t_shell_s=$(( $(date +%s) - t0 )); exit 0; fi
  if grep -qE "Kernel panic|Oops" <<< "$u"; then fail kernel "kernel panic (uart.log)"; fi
done
fail "$(python3 -c "import json;print(json.load(open('$PHASE_FILE')).get('phase'))")" "no shell within ${UART_SECS}s (uart.log)"
