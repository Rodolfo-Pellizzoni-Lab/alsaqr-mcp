#!/usr/bin/env python3
"""Run the benchmark: each (task, condition, repetition) is one headless Claude Code run (`claude -p`) in its own
sandbox, with the alsaqr MCP server attached ("mcp") or not ("base").

usage: run_eval.py --tag NAME [--jobs N] [--reps N] [--rep-start N] [--tasks A,B] [--conds mcp,base]
                   [--model sonnet] [--effort high] [--keep-sandboxes]
Results go to $ALSAQR_EVAL_DIR/results/<tag>/ (default ~/.cache/alsaqr-mcp-eval): <run>.jsonl (stream-json
transcript), <run>.meta.json (timing, exit code), <run>.diff (what the agent changed in the sandbox).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sandbox  # noqa: E402
from tasks import QUAD_SUPPORT, TASKS, prompt  # noqa: E402

DATA = Path(os.environ.get("ALSAQR_EVAL_DIR", Path.home() / ".cache/alsaqr-mcp-eval"))
CLAUDE = str(Path.home() / ".local/bin/claude")
QUOTA_STOP = 0.70  # stop launching runs once the subscription's five-hour window is this full
WATCHED_REPOS = [Path.home() / "he-soc", Path.home() / "alsaqr-software-github"]
FIXTURE_IGNORE = shutil.ignore_patterns(".git", "*.riscv", "*.dump")  # agents must build, not reuse old binaries


def clean_env() -> dict:
    keep = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "DISPLAY", "XDG_RUNTIME_DIR")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + str(Path.home() / ".local/bin")
    env["SHELL"] = "/bin/bash"
    return env


def five_hour_util(results: Path) -> float:
    """Latest five-hour utilization reported by any transcript (rate_limit_event), 0 if none yet."""
    best = (0.0, 0.0)
    for f in sorted(results.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:6]:
        for line in f.open():
            if '"rate_limit_event"' in line:
                try:
                    w = json.loads(line)["rate_limit_info"]["unifiedWindows"]["five_hour"]
                    best = max(best, (f.stat().st_mtime, w["utilization"]))
                except (ValueError, KeyError):
                    pass
    return best[1]


def _src(spec: str) -> Path:
    if spec.startswith("data:"):
        return DATA / spec[5:]
    p = Path(os.path.expanduser(spec))
    return p if p.is_absolute() else HERE / "fixtures" / spec


def install_fixtures(sb: Path, task: str):
    for kind, src, dst in TASKS[task].get("fixtures", []):
        d = sb / dst
        if kind == "dir":
            shutil.copytree(_src(src), d, symlinks=True, ignore=FIXTURE_IGNORE)
            if not (d / ".git").exists() and not str(dst).startswith("he-soc/"):
                # a git baseline, so the run's changes can be recorded afterwards (one commit, no history)
                subprocess.run(["git", "init", "-q", str(d)], check=True)
                subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
                subprocess.run(["git", "-C", str(d), "-c", "user.name=eval", "-c", "user.email=eval@localhost",
                                "commit", "-q", "-m", "checkout"], check=True)
        elif kind == "file":
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_src(src), d)
        elif kind == "quad_support":
            d.mkdir(parents=True, exist_ok=True)
            for f in QUAD_SUPPORT:
                shutil.copy2(sb / "he-soc/software/quad_boot" / f, d / f)
        elif kind == "patch":
            subprocess.run(["patch", "-s", "-p1", "-d", str(d), "-i", str(_src(src))], check=True)
            if (d / ".git").exists():  # the planted change is part of the checkout the agent receives
                subprocess.run(["git", "-C", str(d), "-c", "user.name=eval", "-c", "user.email=eval@localhost",
                                "commit", "-q", "-a", "--amend", "--no-edit"], check=True)
        else:
            raise ValueError(kind)
    in_repo = [dst for _, _, dst in TASKS[task].get("fixtures", []) if dst.startswith("he-soc/")]
    if in_repo:  # part of the checkout the agent receives, so the recorded diff is only the agent's work
        rels = sorted({str(Path(d).relative_to("he-soc")) for d in in_repo})
        subprocess.run(["git", "-C", str(sb / "he-soc"), "add", "-f", *rels], check=True)
        subprocess.run(["git", "-C", str(sb / "he-soc"), "-c", "user.name=eval", "-c",
                        "user.email=eval@localhost", "commit", "-q", "-m",
                        "add " + ", ".join(sorted({Path(r).parts[1] for r in rels if len(Path(r).parts) > 1}))],
                       check=True)


def record_changes(sb: Path, out: Path):
    """git diff of every repository in the sandbox (he-soc and installed fixtures), for grading."""
    parts = []
    for g in sorted(sb.glob("*/.git")):
        repo = g.parent
        st = subprocess.run(["git", "-C", str(repo), "status", "--short"], capture_output=True, text=True).stdout
        df = subprocess.run(["git", "-C", str(repo), "diff"], capture_output=True, text=True).stdout
        parts.append(f"### {repo.name}: git status\n{st}\n### {repo.name}: git diff\n{df[:200000]}")
    out.write_text("\n".join(parts))


def repo_state() -> dict:
    return {str(r): subprocess.run(["git", "-C", str(r), "status", "--porcelain"], capture_output=True,
                                   text=True).stdout for r in WATCHED_REPOS if (r / ".git").exists()}


def run_one(task, cond, rep, a, results: Path) -> dict:
    name = f"{task}_{cond}_{rep}"
    meta_p = results / f"{name}.meta.json"
    if meta_p.exists():
        return json.loads(meta_p.read_text())
    u = five_hour_util(results)
    if u >= QUOTA_STOP:
        print(f"[{time.strftime('%H:%M:%S')}] skip {name}: five-hour quota at {u:.0%}", flush=True)
        return {"run": name, "skipped": True}
    sb = DATA / "sandboxes" / a.tag / name
    if sb.exists():
        shutil.rmtree(sb)
    tmpl = TASKS[task].get("template")
    sandbox.make(sb, DATA / "templates" / tmpl if tmpl else None)
    install_fixtures(sb, task)
    mcp_cfg = str(sb / "mcp_servers.json") if cond == "mcp" else '{"mcpServers":{}}'
    cmd = [CLAUDE, "-p", prompt(task, cond, str(sb)), "--model", a.model, "--effort", a.effort,
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", "auto", "--permission-prompts", "none",
           "--strict-mcp-config", "--mcp-config", mcp_cfg,
           "--setting-sources", "project", "--no-session-persistence", "--max-budget-usd", "25"]
    t0 = time.time()
    with open(results / f"{name}.jsonl", "w") as f, open(results / f"{name}.err", "w") as e:
        try:
            p = subprocess.run(cmd, cwd=sb, env=clean_env(), stdout=f, stderr=e, stdin=subprocess.DEVNULL,
                               timeout=TASKS[task]["timeout_s"])
            rc = p.returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
    wall = round(time.time() - t0, 1)
    # stop anything the run left behind (simulations, sessions) before recording and removing the sandbox
    subprocess.run(["pkill", "-9", "-f", f"CVA6_STRING={sb}/"], capture_output=True)
    subprocess.run(["pkill", "-9", "-f", f"{sb}/he-soc/hardware/xsim/work"], capture_output=True)
    record_changes(sb, results / f"{name}.diff")
    meta = {"run": name, "task": task, "cond": cond, "rep": rep, "rc": rc, "wall_s": wall, "sandbox": str(sb),
            "model": a.model, "effort": a.effort, "template": tmpl}
    meta_p.write_text(json.dumps(meta, indent=1))
    if not a.keep_sandboxes:
        shutil.rmtree(sb, ignore_errors=True)
    print(f"[{time.strftime('%H:%M:%S')}] done {name} rc={rc} wall={wall}s", flush=True)
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--rep-start", type=int, default=1)
    ap.add_argument("--tasks", default=",".join(TASKS))
    ap.add_argument("--conds", default="mcp,base")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--keep-sandboxes", action="store_true")
    a = ap.parse_args()
    results = DATA / "results" / a.tag
    results.mkdir(parents=True, exist_ok=True)
    tasks, conds = a.tasks.split(","), a.conds.split(",")
    before = repo_state()
    # interleave conditions so both see the same machine load; long tasks first
    order = sorted(tasks, key=lambda t: -TASKS[t]["timeout_s"])
    jobs = [(t, c, r) for r in range(a.rep_start, a.rep_start + a.reps) for t in order for c in conds]
    print(f"{len(jobs)} runs, {a.jobs} at a time -> {results}", flush=True)
    with ThreadPoolExecutor(a.jobs) as ex:
        list(ex.map(lambda j: run_one(*j, a, results), jobs))
    after = repo_state()
    changed = [r for r in before if before[r] != after.get(r)]
    (results / "repo_check.json").write_text(json.dumps({"changed_outside_sandboxes": changed}, indent=1))
    print("WARNING: repositories outside the sandboxes changed: " + ", ".join(changed) if changed
          else "outside repositories unchanged", flush=True)


if __name__ == "__main__":
    main()
