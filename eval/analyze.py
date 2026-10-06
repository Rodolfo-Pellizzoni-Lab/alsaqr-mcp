#!/usr/bin/env python3
"""Per-run metrics and failure signals from the stream-json transcripts.

usage: analyze.py TAG [--json out.json]
Tokens: input + cache write + cache read summed over unique API calls (deduplicated by message id; subagent calls
included) = everything the model processed; output separately. Cost is Claude Code's own estimate.
Failure signals: tool errors and MCP {error} results per tool, permission denials, polling and sleeping, simulations
started, budget/timeout terminations, and commands that look like writes outside the sandbox.
"""
import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path

DATA = Path(os.environ.get("ALSAQR_EVAL_DIR", Path.home() / ".cache/alsaqr-mcp-eval"))
POLL_TOOLS = {"mcp__alsaqr__sim_status", "mcp__alsaqr__sim_uart"}
# claude-sonnet-5-5 USD per token (input, cache write, cache read, output), fitted exactly on runs that reported a
# cost; used for runs killed before they could report one (timeouts)
PRICE = (2.0e-6, 4.0e-6, 0.2e-6, 10.0e-6)
WRITE_VERBS = re.compile(r"(sed -i|>\s*/|\bcp\b|\bmv\b|\brm\b|git (checkout|reset|commit|revert|apply)|patch\b|tee\b)")


def _text(body) -> str:
    if isinstance(body, str):
        return body
    if isinstance(body, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in body)
    return json.dumps(body)


def parse(path: Path) -> dict:
    calls, sub_ids, order = {}, set(), []
    tool_name, tool_input = {}, {}
    tools, result_chars, errors, mcp_errors = Counter(), Counter(), Counter(), []
    sleeps, sims, outside, waits = 0, 0, [], Counter()
    result, init = {}, {}
    meta_p = path.with_suffix("").with_suffix(".meta.json")
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    sb = meta.get("sandbox", "")
    for line in path.open():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t = d.get("type")
        if t == "system" and d.get("subtype") == "init":
            init = d
        elif t == "assistant":
            m = d["message"]
            u = m.get("usage") or {}
            if m["id"] not in calls:
                order.append(m["id"])
            cur = calls.setdefault(m["id"], {"tools": []})
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
                cur[k] = max(cur.get(k, 0), u.get(k) or 0)
            if d.get("parent_tool_use_id"):
                sub_ids.add(m["id"])
            for c in m.get("content", []):
                if c.get("type") != "tool_use":
                    continue
                n, inp = c["name"], c.get("input") or {}
                tool_name[c["id"]], tool_input[c["id"]] = n, inp
                tools[n] += 1
                cur["tools"].append(n)
                if n in POLL_TOOLS and inp.get("wait_s"):
                    waits[n] += 1
                if n == "mcp__alsaqr__sim_run":
                    sims += 1
                if n == "Bash":
                    cmd = inp.get("command", "")
                    if re.search(r"\bsleep\s+\d", cmd):
                        sleeps += 1
                    if re.search(r"run\.sh['\"]?\s+l[23]|\bxsim\s+\S+\s+(-R|-t|-tclbatch)", cmd):
                        sims += 1
                    # the real checkouts: reading them is allowed, writing is not (heuristic: a write verb + the path)
                    probe = re.sub(r"source\s+\S*alsaqr-software/source\.sh", "", cmd)  # sourcing the bundle is a read
                    if re.search(r"/home/mhassen/(he-soc|alsaqr-software|alsaqr-software-github)/", probe) \
                            and WRITE_VERBS.search(probe):
                        outside.append(cmd[:200])
                if n in ("Edit", "Write") and sb and not str(inp.get("file_path", "")).startswith(sb):
                    outside.append(f"{n} {inp.get('file_path')}")
        elif t == "user":
            content = d.get("message", {}).get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if not (isinstance(c, dict) and c.get("type") == "tool_result"):
                    continue
                n = tool_name.get(c.get("tool_use_id"), "?")
                body = _text(c.get("content"))
                result_chars[n] += len(body)
                if c.get("is_error"):
                    errors[n] += 1
                if n.startswith("mcp__alsaqr__") and body.lstrip().startswith("{") and '"error"' in body[:200]:
                    try:
                        e = json.loads(body)
                        mcp_errors.append(f"{n[13:]}: {e.get('error')} | {str(e.get('fix'))[:120]}")
                    except ValueError:
                        mcp_errors.append(f"{n[13:]}: {body[:160]}")
        elif t == "result":
            result = d

    def ctx(u):
        return u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]
    main = [i for i in order if i not in sub_ids]
    tot = Counter()
    for c in calls.values():
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
            tot[k] += c[k]
    poll_tokens = sum(ctx(calls[i]) + calls[i]["output_tokens"] for i in main
                      if calls[i]["tools"] and all(x in POLL_TOOLS for x in calls[i]["tools"]))
    total = sum(tot.values())
    denials = result.get("permission_denials") or []
    return {
        "run": path.stem, "task": meta.get("task"), "cond": meta.get("cond"), "rep": meta.get("rep"),
        "rc": meta.get("rc"), "wall_s": meta.get("wall_s"),
        "calls": len(calls), "sub_calls": len(sub_ids),
        "input": tot["input_tokens"], "cache_write": tot["cache_creation_input_tokens"],
        "cache_read": tot["cache_read_input_tokens"], "output": tot["output_tokens"],
        "total_tokens": total, "peak_ctx": max((ctx(calls[i]) for i in main), default=0),
        "first_ctx": ctx(calls[main[0]]) if main else 0,
        "poll_tokens": poll_tokens, "poll_share": round(poll_tokens / total, 3) if total else 0,
        "tool_calls": sum(tools.values()), "mcp_tool_calls": sum(v for k, v in tools.items() if k.startswith("mcp__")),
        "tools": dict(tools), "tool_result_chars": sum(result_chars.values()),
        "result_chars_by_tool": dict(result_chars),
        "tool_errors": dict(errors), "mcp_errors": mcp_errors[:20], "waits": dict(waits),
        "bash_sleeps": sleeps, "sims_started": sims, "outside_writes": outside[:10],
        "cost_usd": result.get("total_cost_usd") or round(sum(
            p * tot[k] for p, k in zip(PRICE, ("input_tokens", "cache_creation_input_tokens",
                                               "cache_read_input_tokens", "output_tokens"))), 4),
        "cost_estimated": not result.get("total_cost_usd"), "num_turns": result.get("num_turns"),
        "subtype": result.get("subtype") or ("timeout" if meta.get("rc") == "timeout" else "no_result"),
        "is_error": result.get("is_error"),
        "permission_denials": len(denials),
        "denials": [f"{x.get('tool_name')}: {str(x.get('tool_input'))[:120]}" for x in denials][:10],
        "answer": result.get("result", ""), "n_tools_offered": len(init.get("tools", [])),
    }


def rows(tag: str) -> list[dict]:
    out = [parse(p) for p in sorted((DATA / "results" / tag).glob("*.jsonl"))]
    return [r for r in out if r["task"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--json")
    a = ap.parse_args()
    rs = rows(a.tag)
    print(f"{'run':28} {'tokens':>10} {'$':>6} {'calls':>5} {'mcp':>4} {'poll%':>5} {'err':>3} {'deny':>4} "
          f"{'sims':>4} {'wall':>6} subtype")
    for r in rs:
        print(f"{r['run'][:28]:28} {r['total_tokens']:>10,} {r['cost_usd']:>6.2f} {r['calls']:>5} "
              f"{r['mcp_tool_calls']:>4} {100*r['poll_share']:>5.0f} {sum(r['tool_errors'].values()):>3} "
              f"{r['permission_denials']:>4} {r['sims_started']:>4} {r['wall_s'] or 0:>6.0f} {r['subtype']}"
              + ("  OUTSIDE-WRITES!" if r["outside_writes"] else ""))
    if a.json:
        Path(a.json).write_text(json.dumps(rs, indent=1))


if __name__ == "__main__":
    main()
