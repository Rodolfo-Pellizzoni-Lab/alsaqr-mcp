#!/bin/bash
# set_phase.sh <state.json> <phase> [key=value ...] -- update a runner's json state file (phase + fields + timestamp)
python3 - "$@" <<'EOF'
import json, sys, datetime, os
p = sys.argv[1]
d = json.load(open(p)) if os.path.exists(p) else {}
d["phase"] = sys.argv[2]
for kv in sys.argv[3:]:
    k, v = kv.split("=", 1)
    try:
        v = int(v)
    except ValueError:
        try:
            v = float(v)
        except ValueError:
            pass
    d[k] = v
d["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
tmp = p + ".tmp"
json.dump(d, open(tmp, "w"), indent=1)
os.replace(tmp, p)
EOF
