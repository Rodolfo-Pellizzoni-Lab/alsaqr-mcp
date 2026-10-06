#!/usr/bin/env python3
"""Grade each run against its task rubric with a separate model call, and explain shortfalls.

usage: grade.py TAG [--jobs N] [--model opus] [--force]
Writes $ALSAQR_EVAL_DIR/results/<tag>/<run>.grade.json. The grader sees the task, the rubric, the agent's final
answer, the git diff of what it changed, and a condensed trace (tool calls, truncated results, errors). It scores
the answer and the delivered fix, uses the trace only to check claims (e.g. that a verification run really ran) and
to classify why points were lost.
"""
import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from analyze import DATA, _text  # noqa: E402
from run_eval import CLAUDE, clean_env  # noqa: E402
from tasks import TASKS  # noqa: E402

CATEGORIES = {
    "wrong_tool_info": "a tool (MCP or repo script/doc) returned wrong or misleading information that the agent followed",
    "tool_failure": "a tool errored, timed out, or could not do what was needed, and the agent could not work around it",
    "environment": "sandbox/setup problem outside the agent's control (missing file, permission denial, quota)",
    "agent_error": "the agent misread data, reasoned wrongly, or made an incorrect change despite correct tool output",
    "incomplete": "the answer leaves rubric items unaddressed or unverified although the agent had the means",
    "timeout_or_budget": "the run hit its wall-clock or budget limit",
}
SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "awarded": {"type": "number"}, "max": {"type": "number"}, "note": {"type": "string"}},
            "required": ["awarded", "max", "note"]}},
        "score": {"type": "number"}, "max": {"type": "number"},
        "failure_category": {"type": "string", "enum": ["none", *CATEGORIES]},
        "failure_explanation": {"type": "string"},
        "mcp_issues": {"type": "string"},
    },
    "required": ["items", "score", "max", "failure_category", "failure_explanation", "mcp_issues"],
}


def condensed_trace(path: Path, limit: int = 30000) -> str:
    out, names = [], {}
    for line in path.open():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "assistant":
            for c in d["message"].get("content", []):
                if c.get("type") == "text" and c.get("text", "").strip():
                    out.append("ASSISTANT: " + c["text"].strip()[:300])
                elif c.get("type") == "tool_use":
                    names[c["id"]] = c["name"]
                    out.append(f"CALL {c['name']}: {json.dumps(c.get('input'))[:300]}")
        elif d.get("type") == "user" and isinstance(d.get("message", {}).get("content"), list):
            for c in d["message"]["content"]:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    tag = "ERROR " if c.get("is_error") else ""
                    out.append(f"RESULT {tag}{names.get(c.get('tool_use_id'), '?')}: "
                               f"{_text(c.get('content')).strip()[:400]}")
    s = "\n".join(out)
    if len(s) > limit:  # keep the start (approach) and the end (verification, conclusion)
        s = s[:int(limit * 0.4)] + "\n[... trace truncated ...]\n" + s[-int(limit * 0.6):]
    return s


def grade_one(run_jsonl: Path, model: str, force: bool) -> dict:
    out = run_jsonl.with_suffix("").with_suffix(".grade.json")
    if out.exists() and not force:
        return json.loads(out.read_text())
    meta = json.loads(run_jsonl.with_suffix("").with_suffix(".meta.json").read_text())
    task = TASKS[meta["task"]]
    answer = ""
    for line in run_jsonl.open():
        if '"type":"result"' in line.replace(" ", ""):
            try:
                answer = json.loads(line).get("result") or ""
            except ValueError:
                pass
    diff_p = run_jsonl.with_suffix("").with_suffix(".diff")
    diff = diff_p.read_text()[:15000] if diff_p.exists() else "(none recorded)"
    rubric = "\n".join(f"- [{p} pt] {c}" for p, c in task["rubric"])
    prompt = f"""You grade an AI agent's work on an engineering task about the AlSaqr SoC (RTL simulated in Vivado \
xsim). Score strictly by the rubric; it was established by the benchmark authors from the tools and the RTL.

TASK GIVEN TO THE AGENT:
{task['prompt']}

RUBRIC (award each item 0..max; partial credit only where the item says so or where it is clearly half right):
{rubric}

AGENT'S FINAL ANSWER:
{answer or '(no final answer: the run ended without one)'}

WHAT THE AGENT CHANGED (git status/diff of the repositories in its sandbox):
{diff}

CONDENSED TRACE (tool calls, truncated results, errors):
{condensed_trace(run_jsonl)}

Instructions:
- Score the final answer, and for fix items the diff. Use the trace only to check claims (a claimed verification \
run must actually appear in the trace) and to explain lost points. An answer that states a fact correctly earns the \
item even if phrased differently; numeric times within the rubric's tolerance are fine.
- items: one entry per rubric item in order, with a short note.
- failure_category: "none" if score == max, else the main reason points were lost, one of:
{json.dumps(CATEGORIES, indent=1)}
- failure_explanation: if points were lost, 1-3 sentences on what went wrong and where in the trace (else "").
- mcp_issues: any problem with the alsaqr MCP tools (mcp__alsaqr__*) visible in the trace: errors, misleading \
output, missing capability, awkward usage that cost calls. "" if none or if the agent did not use them."""
    prompt = prompt.replace("\x00", "")  # transcripts can carry NUL bytes from binary tool output
    cmd = [CLAUDE, "-p", prompt, "--model", model, "--output-format", "json", "--tools", "",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "project",
           "--no-session-persistence", "--json-schema", json.dumps(SCHEMA)]
    p = subprocess.run(cmd, cwd=str(run_jsonl.parent), env=clean_env(), capture_output=True, text=True, timeout=900)
    try:
        g = json.loads(p.stdout)["structured_output"]
    except (ValueError, KeyError, TypeError):
        g = {"error": "grader failed", "stdout": p.stdout[-2000:], "stderr": p.stderr[-2000:]}
    g["run"], g["task"], g["cond"] = meta["run"], meta["task"], meta["cond"]
    out.write_text(json.dumps(g, indent=1))
    print(f"graded {meta['run']}: {g.get('score')}/{g.get('max')} {g.get('failure_category', '')}", flush=True)
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--model", default="opus")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    runs = [p for p in sorted((DATA / "results" / a.tag).glob("*.jsonl"))
            if p.with_suffix("").with_suffix(".meta.json").exists()]
    with ThreadPoolExecutor(a.jobs) as ex:
        list(ex.map(lambda p: grade_one(p, a.model, a.force), runs))


if __name__ == "__main__":
    main()
