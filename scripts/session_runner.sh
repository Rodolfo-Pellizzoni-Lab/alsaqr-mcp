#!/bin/bash
# Detached runner for one interactive xsim session (debug snapshot, Tcl commands through a FIFO).
# Env: SESSION_DIR WORK SNAP NEED_ELAB TB_FILE TB_OPTS DPI_DIR GENERICS PLUSARGS VIVADO_SETTINGS
set -u
cd "$WORK"
source "$VIVADO_SETTINGS" >/dev/null 2>&1

set_phase() {
  python3 - "$SESSION_DIR/state.json" "$@" <<'EOF'
import json, sys, datetime
p = sys.argv[1]; d = json.load(open(p)); d["phase"] = sys.argv[2]
for kv in sys.argv[3:]:
    k, v = kv.split("=", 1)
    try: v = int(v)
    except ValueError: pass
    d[k] = v
d["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
json.dump(d, open(p, "w"), indent=1)
EOF
}

if [ "$NEED_ELAB" = "1" ]; then
  set_phase elab
  exec 9>"$WORK/elab.lock"; flock 9
  if [ ! -f "$WORK/$SNAP.ok" ]; then
    touch "$WORK/.tb_xvlog_start"
    xvlog -sv -work work -L uvm $TB_OPTS "$TB_FILE" > "$WORK/xvlog_$SNAP.log" 2>&1 \
      || { grep -E 'ERROR' "$WORK/xvlog_$SNAP.log" | head -5 > "$SESSION_DIR/elab_errors.txt"; set_phase elab_failed; flock -u 9; exit 1; }
    { cat "$WORK/tb_units.txt" 2>/dev/null; find "$WORK/xsim.dir/work" -maxdepth 1 -type f -newer "$WORK/.tb_xvlog_start" -printf "%f\n"; } | sort -u > "$WORK/tb_units.tmp" && mv "$WORK/tb_units.tmp" "$WORK/tb_units.txt"
    xelab -relax -L uvm -timescale 1ps/1ps --ignore_assertions --debug typical --snapshot "$SNAP" work.ariane_tb \
      -sv_root "$DPI_DIR" -sv_lib libdpi $GENERICS > "$WORK/xelab_$SNAP.log" 2>&1 \
      || { grep -E 'ERROR' "$WORK/xelab_$SNAP.log" | head -5 > "$SESSION_DIR/elab_errors.txt"; set_phase elab_failed; flock -u 9; exit 1; }
    touch "$WORK/$SNAP.ok"
  fi
  flock -u 9
fi

set_phase starting
rm -f "$SESSION_DIR/cmd.fifo"; mkfifo "$SESSION_DIR/cmd.fifo"
exec 3<>"$SESSION_DIR/cmd.fifo"          # read-write: keeps the FIFO open between writers
: > "$SESSION_DIR/out.log"
# launch window lock: xsim rewrites xsim.dir/$SNAP/xsim_script.tcl (plusargs) at start
exec 8>"$WORK/$SNAP.start.lock"; flock 8
setsid bash -c "exec xsim $SNAP -log $SESSION_DIR/xsim.log $PLUSARGS" <&3 > "$SESSION_DIR/out.log" 2>&1 &
XPID=$!
for i in $(seq 1 120); do pgrep -g "$XPID" -x xsimk >/dev/null 2>&1 && break; kill -0 "$XPID" 2>/dev/null || break; sleep 1; done
sleep 3; flock -u 8; exec 8>&-
set_phase starting xsim_pgid="$XPID"
# readiness probe: answered once the Tcl interpreter is up
printf 'set top [lindex [get_scopes /*] 0]\nputs "@@TOP $top"\nputs "@@DONE 0"\nflush stdout\n' >&3
wait "$XPID"
set_phase dead rc="$?"
