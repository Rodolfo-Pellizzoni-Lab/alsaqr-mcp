#!/bin/bash
# Detached runner: boot Linux (OpenSBI fw_payload + DTB) on the programmed VCU118, keep the console captured, and
# record boot milestones. Over JTAG the DTB and the payload are loaded with GDB; with NETBOOT_ELF set only that
# small loader goes over JTAG and the board fetches the two files from this machine over Ethernet (netboot_server.py
# on NETBOOT_PORT serving BOOT_DIR/serve). Environment (set by board.py): BOOT_DIR BUNDLE PAYLOAD DTB DTB_ADDR
#   UART BAUD UART_SECS NUM_HARTS SRMCFG_LIST ("hart:value ..." QoS tags written through GDB before release) SCRIPTS
#   NETBOOT_ELF NETBOOT_BIN NETBOOT_PORT (Ethernet boot only)
# Phases (boot.json): openocd -> loading -> released [-> link -> transfer] -> opensbi -> kernel -> shell | failed
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
if [ -n "${NETBOOT_ELF:-}" ]; then
  # Ethernet: the boot server (exits after the payload), then the loader over JTAG; the loader releases harts 1-3 itself
  mkdir -p "$BOOT_DIR/serve"; ln -sfn "$DTB" "$BOOT_DIR/serve/alsaqr.dtb"; ln -sfn "$NETBOOT_BIN" "$BOOT_DIR/serve/fw_payload.bin"
  pkill -f "scripts/netboot_serve[r].py" 2>/dev/null; sleep 0.5
  nohup python3 "$SCRIPTS/netboot_server.py" --dir "$BOOT_DIR/serve" --port "$NETBOOT_PORT" --once > "$BOOT_DIR/netboot_server.log" 2>&1 &
  echo $! > "$BOOT_DIR/netboot_server.pid"
  LOAD_ELF=$NETBOOT_ELF
  # as board_run.sh does: reset, load the loader (hart 0 runs it), release harts 2-3 through the debug module and resume;
  # harts 1-3 then wait in the boot ROM, where the loader's mailbox doorbell / APMU interrupts send them into the payload
  {
    cat <<G
set pagination off
set confirm off
set remotetimeout 120
target extended-remote :6699
monitor reset halt
load
monitor riscv dmi_write 0x10 0x40020001
monitor riscv dmi_write 0x10 0x00020001
monitor riscv dmi_write 0x10 0x40030001
monitor riscv dmi_write 0x10 0x00030001
monitor riscv dmi_write 0x10 0x00000001
monitor sleep 200
monitor resume
quit
G
  } > "$BOOT_DIR/boot.gdb"
else
  LOAD_ELF=$PAYLOAD
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
fi
"$GDB" -batch -x "$BOOT_DIR/boot.gdb" "$LOAD_ELF" > "$BOOT_DIR/gdb.log" 2>&1
grep -q "Transfer rate" "$BOOT_DIR/gdb.log" || fail loading "payload load failed (gdb.log)"
phase released

# ---------- milestones from the console ----------
t0=$(date +%s)
seen_sbi=0; seen_kernel=0; seen_link=0; seen_xfer=0
for i in $(seq 1 $((UART_SECS/2))); do
  sleep 2
  u=$(tr -d '\r' < "$BOOT_DIR/uart.log" 2>/dev/null)
  if [ -n "${NETBOOT_ELF:-}" ]; then
    if [ $seen_link = 0 ] && grep -q "netboot: link up" <<< "$u"; then seen_link=1; phase link t_link_s=$(( $(date +%s) - t0 )); fi
    if [ $seen_xfer = 0 ] && grep -q "SERVED fw_payload.bin" "$BOOT_DIR/netboot_server.log" 2>/dev/null; then
      seen_xfer=1; phase transfer t_transfer_s=$(( $(date +%s) - t0 )) transfer_ms=$(grep "SERVED fw_payload.bin" "$BOOT_DIR/netboot_server.log" | awk '{print $4}')
    fi
    if grep -q "netboot: no link yet" <<< "$u" && [ $seen_link = 0 ] && [ $i -gt 60 ]; then fail link "the board sees no link partner (cable to the PC's adapter?)"; fi
  fi
  if [ $seen_sbi = 0 ] && grep -q "OpenSBI v" <<< "$u"; then seen_sbi=1; phase opensbi t_opensbi_s=$(( $(date +%s) - t0 )); fi
  if [ $seen_kernel = 0 ] && grep -q "Linux version" <<< "$u"; then seen_kernel=1; phase kernel t_kernel_s=$(( $(date +%s) - t0 )); fi
  if grep -qE "built-in shell|login:" <<< "$u"; then sleep 6; phase shell t_shell_s=$(( $(date +%s) - t0 )); exit 0; fi
  if grep -qE "Kernel panic|Oops" <<< "$u"; then fail kernel "kernel panic (uart.log)"; fi
done
fail "$(python3 -c "import json;print(json.load(open('$PHASE_FILE')).get('phase'))")" "no shell within ${UART_SECS}s (uart.log)"
