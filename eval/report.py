#!/usr/bin/env python3
"""Summary tables for a benchmark tag: per task MCP vs baseline (tokens, cost, calls, wall, score), savings,
failure categories and MCP issues reported by the grader.

usage: report.py TAG [--json out.json]
"""
import argparse
import json
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze import DATA, rows  # noqa: E402
from tasks import TASKS  # noqa: E402

KEYS = ("total_tokens", "cost_usd", "calls", "peak_ctx", "tool_result_chars", "wall_s", "poll_share")


def mean(rs, k):
    v = [r[k] for r in rs if r.get(k) is not None]
    return st.mean(v) if v else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--json")
    a = ap.parse_args()
    res = DATA / "results" / a.tag
    rs = rows(a.tag)
    for r in rs:
        g = res / f"{r['run']}.grade.json"
        r["grade"] = json.loads(g.read_text()) if g.exists() else {}
        r["score"] = r["grade"].get("score")
        r["max"] = sum(p for p, _ in TASKS[r["task"]]["rubric"])
    by = defaultdict(list)
    for r in rs:
        by[(r["task"], r["cond"])].append(r)
    summary = {}
    print(f"{'task':15} {'n':>3}  {'tokens MCP / base':>23} {'saved':>6}  {'cost MCP / base':>15} {'saved':>6}  "
          f"{'calls':>9}  {'wall s':>11}  {'score MCP / base':>17}")
    for t in TASKS:
        m, b = by.get((t, "mcp"), []), by.get((t, "base"), [])
        if not m or not b:
            continue
        sv = {k: 100 * (1 - mean(m, k) / mean(b, k)) if mean(b, k) else float("nan") for k in KEYS}
        sm = [r["score"] for r in m if r["score"] is not None]
        sb = [r["score"] for r in b if r["score"] is not None]
        mx = m[0]["max"]
        summary[t] = {"mcp": {k: mean(m, k) for k in KEYS}, "base": {k: mean(b, k) for k in KEYS}, "saving_pct": sv,
                      "score_mcp": st.mean(sm) if sm else None, "score_base": st.mean(sb) if sb else None,
                      "max": mx, "n": (len(m), len(b))}
        print(f"{t:15} {len(m)}/{len(b)}  {mean(m, 'total_tokens'):>11,.0f} / {mean(b, 'total_tokens'):>9,.0f} "
              f"{sv['total_tokens']:>5.0f}%  ${mean(m, 'cost_usd'):>5.2f} / ${mean(b, 'cost_usd'):>5.2f} "
              f"{sv['cost_usd']:>5.0f}%  {mean(m, 'calls'):>3.0f} / {mean(b, 'calls'):>3.0f}  "
              f"{mean(m, 'wall_s'):>4.0f} / {mean(b, 'wall_s'):>4.0f}  "
              f"{(st.mean(sm) if sm else float('nan')):>5.2f} / {(st.mean(sb) if sb else float('nan')):>5.2f} /{mx}")
    for c in ("mcp", "base"):
        cr = [r for r in rs if r["cond"] == c]
        sc = [r["score"] for r in cr if r["score"] is not None]
        print(f"ALL {c:4}: runs {len(cr)}  tokens {sum(r['total_tokens'] for r in cr):,}  "
              f"cost ${sum(r['cost_usd'] for r in cr):.2f}  calls {sum(r['calls'] for r in cr)}  "
              f"wall {sum(r['wall_s'] or 0 for r in cr) / 60:.0f} min  score {sum(sc):.1f}/"
              f"{sum(r['max'] for r in cr if r['score'] is not None):.1f}  denials "
              f"{sum(r['permission_denials'] for r in cr)}  outside-writes {sum(bool(r['outside_writes']) for r in cr)}")
    print("\nlost points by cause:")
    cause = Counter((r["cond"], r["grade"].get("failure_category")) for r in rs
                    if r["score"] is not None and r["score"] < r["max"])
    for (c, k), n in sorted(cause.items()):
        print(f"  {c:4} {k}: {n}")
    print("\nruns below full marks:")
    for r in rs:
        if r["score"] is not None and r["score"] < r["max"]:
            print(f"  {r['run']:24} {r['score']}/{r['max']} [{r['grade'].get('failure_category')}] "
                  f"{r['grade'].get('failure_explanation', '')[:300]}")
    print("\nMCP issues noted by the grader:")
    for r in rs:
        if r["cond"] == "mcp" and r["grade"].get("mcp_issues"):
            print(f"  {r['run']:24} {r['grade']['mcp_issues'][:300]}")
    if a.json:
        Path(a.json).write_text(json.dumps({"summary": summary, "runs": rs}, indent=1, default=str))


if __name__ == "__main__":
    main()
