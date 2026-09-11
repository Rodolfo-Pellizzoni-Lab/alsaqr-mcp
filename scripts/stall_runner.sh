#!/bin/bash
# Detached stall tracer: (1) elaborate the debug snapshot if needed, (2) step from START_NS in STEP_NS steps until
# the simulator stops advancing, (3) re-run to just before that time with ptrace on for WINDOW_NS.
# Env: TRACE_DIR WORK SNAP NEED_ELAB TB_FILE TB_OPTS DPI_DIR GENERICS PLUSARGS START_NS STEP_NS MAX_STEPS WINDOW_NS VIVADO_SETTINGS
set -u
cd "$WORK"
source "$VIVADO_SETTINGS" >/dev/null 2>&1

set_phase() {
  python3 - "$TRACE_DIR/state.json" "$@" <<'EOF'
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
kill_snap_run() { kill -9 -- -"$1" 2>/dev/null; }

if [ "$NEED_ELAB" = "1" ]; then
  set_phase elab
  exec 9>"$WORK/elab.lock"; flock 9
  if [ ! -f "$WORK/$SNAP.ok" ]; then
    touch "$WORK/.tb_xvlog_start"
    xvlog -sv -work work -L uvm $TB_OPTS "$TB_FILE" > "$WORK/xvlog_$SNAP.log" 2>&1 \
      || { grep -E 'ERROR' "$WORK/xvlog_$SNAP.log" | head -5 > "$TRACE_DIR/errors.txt"; set_phase elab_failed; flock -u 9; exit 1; }
    { cat "$WORK/tb_units.txt" 2>/dev/null; find "$WORK/xsim.dir/work" -maxdepth 1 -type f -newer "$WORK/.tb_xvlog_start" -printf "%f\n"; } | sort -u > "$WORK/tb_units.tmp" && mv "$WORK/tb_units.tmp" "$WORK/tb_units.txt"
    xelab -relax -L uvm -timescale 1ps/1ps --ignore_assertions --debug typical --snapshot "$SNAP" work.ariane_tb \
      -sv_root "$DPI_DIR" -sv_lib libdpi $GENERICS > "$WORK/xelab_$SNAP.log" 2>&1 \
      || { grep -E 'ERROR' "$WORK/xelab_$SNAP.log" | head -5 > "$TRACE_DIR/errors.txt"; set_phase elab_failed; flock -u 9; exit 1; }
    touch "$WORK/$SNAP.ok"
  fi
  flock -u 9
fi

launch() {  # launch <tcl> <stdout file> -> sets XPID (process group), honours the snapshot launch lock
  exec 8>"$WORK/$SNAP.start.lock"; flock 8
  setsid bash -c "exec xsim $SNAP -t $1 -nolog $PLUSARGS" > "$2" 2>&1 &
  XPID=$!
  for i in $(seq 1 120); do pgrep -g "$XPID" -x xsimk >/dev/null 2>&1 && break; kill -0 "$XPID" 2>/dev/null || break; sleep 1; done
  sleep 3; flock -u 8; exec 8>&-
}

# ---------- (2) locate: step until the marker file stops advancing ----------
set_phase locate
cat > "$TRACE_DIR/locate.tcl" <<EOF
set f [open "$TRACE_DIR/locate.txt" w]
run ${START_NS}ns; puts \$f "REACHED [current_time]"; flush \$f
for {set i 0} {\$i < $MAX_STEPS} {incr i} { run ${STEP_NS}ns; puts \$f "REACHED [current_time]"; flush \$f }
puts \$f "NOSTALL"; flush \$f
quit
EOF
: > "$TRACE_DIR/locate.txt"
launch "$TRACE_DIR/locate.tcl" "$TRACE_DIR/locate.log"
last=""; since=$(date +%s); stalled=0
while kill -0 "$XPID" 2>/dev/null; do
  cur=$(tail -1 "$TRACE_DIR/locate.txt" 2>/dev/null)
  now=$(date +%s); [ "$cur" != "$last" ] && { last=$cur; since=$now; }
  if grep -q NOSTALL "$TRACE_DIR/locate.txt" 2>/dev/null; then break; fi
  if [ -n "$last" ] && [ $((now-since)) -ge 90 ]; then stalled=1; kill_snap_run "$XPID"; break; fi
  sleep 5
done
wait "$XPID" 2>/dev/null
lastline=$(grep REACHED "$TRACE_DIR/locate.txt" | tail -1)
last_ns=$(python3 -c "
import re,sys; s='''$lastline'''
m=re.search(r'([\d.]+)\s*(ps|ns|us|ms)',s); u={'ps':1e-3,'ns':1,'us':1e3,'ms':1e6}
print(int(float(m.group(1))*u[m.group(2)]) if m else '')")
if [ "$stalled" != "1" ]; then set_phase no_stall last_ns="${last_ns:-0}"; exit 0; fi
set_phase trace stall_ns="$last_ns"

# ---------- (3) trace: ptrace window ending at the stall ----------
from_ns=$(( last_ns - 200 )); [ $from_ns -lt 0 ] && from_ns=0
printf 'run %sns\nptrace on\nrun %sns\nquit\n' "$from_ns" "$WINDOW_NS" > "$TRACE_DIR/trace.tcl"
exec 8>"$WORK/$SNAP.start.lock"; flock 8
setsid bash -c "exec xsim $SNAP -t $TRACE_DIR/trace.tcl -nolog $PLUSARGS 2>&1 | head -c 400000000 > $TRACE_DIR/ptrace.log" &
XPID=$!
for i in $(seq 1 120); do pgrep -g "$XPID" -x xsimk >/dev/null 2>&1 && break; kill -0 "$XPID" 2>/dev/null || break; sleep 1; done
sleep 3; flock -u 8; exec 8>&-
# the ring never ends: give the ptrace window a bounded time after the run reaches the stall
t0=$(date +%s); limit=$(( last_ns / 400000 + 240 ))   # ~1 wall-min per sim-ms (debug snapshot) + 4 min
while kill -0 "$XPID" 2>/dev/null; do
  sz=$(stat -c %s "$TRACE_DIR/ptrace.log" 2>/dev/null || echo 0)
  if [ "$sz" -ge 400000000 ] || [ $(( $(date +%s) - t0 )) -ge "$limit" ]; then kill_snap_run "$XPID"; break; fi
  sleep 5
done
wait "$XPID" 2>/dev/null
pkill -9 -g "$XPID" 2>/dev/null
set_phase done
