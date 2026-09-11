#!/bin/bash
# Detached runner for one xsim run. Everything comes in through the environment (set by sim.py):
#   RUN_DIR WORK SNAP NEED_ELAB TB_FILE TB_OPTS DPI_DIR GENERICS PLUSARGS TIMEOUT_S STALL_IDLE_S VIVADO_SETTINGS
# Phases (run.json): queued -> elab -> running -> finished | stalled | timeout | elab_failed | killed
set -u
cd "$WORK"
source "$VIVADO_SETTINGS" >/dev/null 2>&1

set_phase() {   # set_phase <phase> [key=value ...]
  python3 - "$RUN_DIR/run.json" "$@" <<'EOF'
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

# ---------- elaboration (once per design stamp, serialised per snapshot) ----------
if [ "$NEED_ELAB" = "1" ]; then
  set_phase elab
  exec 9>"$WORK/elab.lock"; flock 9   # one elaboration at a time (tb xvlog + xelab share the library)
  if [ ! -f "$WORK/$SNAP.ok" ]; then
    touch "$WORK/.tb_xvlog_start"
    xvlog -sv -work work -L uvm $TB_OPTS "$TB_FILE" > "$WORK/xvlog_$SNAP.log" 2>&1 \
      || { grep -E 'ERROR' "$WORK/xvlog_$SNAP.log" | head -5 > "$RUN_DIR/elab_errors.txt"; set_phase elab_failed stage=xvlog; flock -u 9; exit 1; }
    { cat "$WORK/tb_units.txt" 2>/dev/null; find "$WORK/xsim.dir/work" -maxdepth 1 -type f -newer "$WORK/.tb_xvlog_start" -printf "%f\n"; } | sort -u > "$WORK/tb_units.tmp" && mv "$WORK/tb_units.tmp" "$WORK/tb_units.txt"
    xelab -relax -L uvm -timescale 1ps/1ps --ignore_assertions --snapshot "$SNAP" work.ariane_tb \
      -sv_root "$DPI_DIR" -sv_lib libdpi $GENERICS > "$WORK/xelab_$SNAP.log" 2>&1 \
      || { grep -E 'ERROR' "$WORK/xelab_$SNAP.log" | head -5 > "$RUN_DIR/elab_errors.txt"; set_phase elab_failed stage=xelab; flock -u 9; exit 1; }
    touch "$WORK/$SNAP.ok"
  fi
  flock -u 9
fi

# ---------- simulation ----------
: > "$RUN_DIR/stdout.log"; : > "$RUN_DIR/uart.txt"
set_phase running sim_started_at="$(date -Iseconds)"
# xsim rewrites xsim.dir/$SNAP/xsim_script.tcl (which carries the plusargs) at every launch, so two runs
# starting on the same snapshot must not overlap in that window: hold a per-snapshot lock until xsimk is up.
exec 8>"$WORK/$SNAP.start.lock"; flock 8
setsid bash -c "exec timeout $TIMEOUT_S xsim $SNAP -R -log $RUN_DIR/xsim.log $PLUSARGS" > "$RUN_DIR/stdout.log" 2>&1 &
XPID=$!
echo "$XPID" > "$RUN_DIR/xsim.pgid"
for i in $(seq 1 90); do pgrep -g "$XPID" -x xsimk >/dev/null 2>&1 && break; kill -0 "$XPID" 2>/dev/null || break; sleep 1; done
sleep 3; flock -u 8; exec 8>&-
# console follower: one line per mock-UART line, prefixed with the host time it appeared
setsid bash -c "tail -n +1 -F '$RUN_DIR/stdout.log' 2>/dev/null | grep --line-buffered 'Mock uart' | while IFS= read -r l; do printf '%s %s\\n' \"\$(date +%H:%M:%S)\" \"\$l\"; done > '$RUN_DIR/uart.txt'" &
FPID=$!
# watchdog: sim time from the TICK heartbeat; kill the run's process group after STALL_IDLE_S without progress
setsid bash -c '
  last=""; since=$(date +%s); sleep 20
  while kill -0 "'"$XPID"'" 2>/dev/null; do
    t=$(grep -a TICK "'"$RUN_DIR"'/stdout.log" 2>/dev/null | tail -1 | awk '"'"'{print $2}'"'"')
    now=$(date +%s); [ "$t" != "$last" ] && { last=$t; since=$now; }
    rss=$(ps -o rss= --ppid "'"$XPID"'" 2>/dev/null | awk '"'"'{s+=$1} END {print int(s/1024)}'"'"')
    rss2=$(pgrep -g "'"$XPID"'" | xargs -r ps -o rss= -p 2>/dev/null | awk '"'"'{s+=$1} END {print int(s/1024)}'"'"')
    echo "WD $(date +%T) tick=$t idle=$((now-since))s rss=${rss2:-$rss}MB"
    if [ -n "$t" ] && [ $((now-since)) -ge "'"$STALL_IDLE_S"'" ]; then echo "STALLED at $t"; kill -9 -- -"'"$XPID"'" 2>/dev/null; break; fi
    sleep 30
  done' > "$RUN_DIR/watchdog.txt" 2>&1 &
WPID=$!
wait "$XPID"; rc=$?
sleep 1; kill -9 -- -"$FPID" 2>/dev/null; kill -9 -- -"$WPID" 2>/dev/null
# final UART sweep (the follower may have been killed before the last lines)
grep -a 'Mock uart' "$RUN_DIR/stdout.log" | while IFS= read -r l; do printf '%s %s\n' "--:--:--" "$l"; done > "$RUN_DIR/uart.final.txt"
if [ $(wc -l < "$RUN_DIR/uart.final.txt") -gt $(wc -l < "$RUN_DIR/uart.txt") ]; then cp "$RUN_DIR/uart.final.txt" "$RUN_DIR/uart.txt"; fi
rm -f "$RUN_DIR/uart.final.txt"
if grep -q 'STALLED' "$RUN_DIR/watchdog.txt" 2>/dev/null; then phase=stalled
elif [ $rc -eq 124 ]; then phase=timeout
elif [ -f "$RUN_DIR/killed" ]; then phase=killed
else phase=finished; fi
set_phase "$phase" rc="$rc" sim_finished_at="$(date -Iseconds)"
