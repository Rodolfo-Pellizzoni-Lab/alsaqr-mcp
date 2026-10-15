"""sim_regress: build and run a set of self-checking test programs in parallel, one compact table back.

Suites:
  soc    SoC validation (he-soc software/soc_tests: memory paths, atomics, 4-core coherence, interrupts,
         accelerator socket, QoS IDs, DRAM timing model) plus hello_culsans and quad_boot
  accel  the C test of every registered accelerator (software/accel/<name>)
  all    both
A test is a directory with a Makefile (name under he-soc software/, or a path). Self-checking tests print
"TEST PASS n checks" / "TEST FAIL k/n checks" and exit with the number of failed checks; CHECK FAIL and TRAP lines
are reported per test.
"""
import json
import re
import time
from pathlib import Path

from . import accel, sim, soc, status
from .config import load

SOC_SUITE = ["soc_tests/t_mem", "soc_tests/t_atomics", "soc_tests/t_smp", "soc_tests/t_irq", "soc_tests/t_socket",
             "soc_tests/t_qos", "soc_tests/t_cbqri", "soc_tests/t_dram", "hello_culsans", "quad_boot"]
MAX_PARALLEL = 12
_KEEP = re.compile(r"^(TEST |CHECK FAIL|TRAP)")


def _err(error, fix, **extra):
    d = {"error": error, "fix": fix}
    d.update(extra)
    return d


def _suite(cfg, name: str) -> list[str]:
    acc = [str(cfg.software / "accel" / e["name"]) for e in accel._load(accel._P(cfg))["slots"].values()]
    return {"soc": SOC_SUITE, "accel": acc, "all": SOC_SUITE + acc}.get(name)


def _row(test: str, run_id: str | None, st: dict | None, build_err: dict | None = None) -> dict:
    name = Path(test).name
    if build_err:
        errs = build_err.get("errors") or []
        return {"test": name, "result": "build_error", "detail": (errs[:3] if errs else build_err.get("fix", ""))}
    state = st.get("state")
    ex = st.get("exit") or {}
    con = st.get("console") or []
    row = {"test": name, "run_id": run_id, "state": state}
    if state in ("finished", "stalled", "timeout", "killed", "elab_failed"):
        code = ex.get("program_exit_code")
        ok = ex.get("verdict") == "success" and (code in (0, None)) and not st.get("qos_errors")
        row["result"] = "pass" if ok else "fail"
        if code is not None:
            row["exit_code"] = code
        row["sim_ms"] = round((st.get("sim_time_ns") or 0) / 1e6, 2)
        lines = [c["text"] for c in con if _KEEP.match(c.get("text", ""))]
        if not ok:
            row["detail"] = (lines or [c["text"] for c in con[-5:]]
                             or [m["text"] for m in st.get("markers", []) if m["kind"] in ("fatal", "fail", "error")][-3:])[:8]
            if st.get("qos_errors"):
                row["detail"] = row["detail"] + ["[QOS] " + l for l in st["qos_errors"][:4]]
            if st.get("stall"):
                row["stall"] = st["stall"].get("meaning")
            if st.get("elab_errors"):
                row["detail"] = st["elab_errors"]
        elif lines:
            row["summary"] = lines[-1]
    else:
        row["result"] = "running"
        row["sim_ms"] = round((st.get("sim_time_ns") or 0) / 1e6, 2)
    return row


def sim_regress(suite: str | None = None, tests: list[str] | None = None, regress_id: str | None = None,
                wait_s: int = 900) -> dict:
    cfg = load()
    rdir = cfg.state / "regress"
    rdir.mkdir(parents=True, exist_ok=True)
    wait_s = max(0, min(int(wait_s or 0), 900))
    if regress_id:
        f = rdir / f"{regress_id}.json"
        if not f.exists():
            return _err("unknown regress_id", f"known: {sorted(p.stem for p in rdir.glob('*.json'))[-10:]}")
        state = json.loads(f.read_text())
    else:
        if tests is None:
            tests = _suite(cfg, suite or "soc")
            if tests is None:
                return _err("unknown suite", "suites: soc, accel, all (or pass tests=[...])")
        if not tests:
            return _err("empty suite", "no tests (no accelerators registered?)")
        if len(tests) > MAX_PARALLEL:
            return _err("too many tests", f"at most {MAX_PARALLEL} in parallel (each simulation needs ~2.7 GB)")
        fp = cfg.flow_problem()
        if fp:
            return fp
        c = accel.compile_changed(cfg)        # accelerator / socket sources edited since the last compile
        if "error" in c or c.get("errors"):
            return _err("accelerator sources do not compile", "fix them (accel_unit_test shows the same errors)",
                        errors=c.get("errors") or c)
        regress_id = time.strftime("rg%m%d_%H%M%S")
        state = {"regress_id": regress_id, "started": time.time(), "runs": [], "recompiled": c.get("recompiled", [])}
        for t in tests:
            b = soc.sw_build(t)
            if "error" in b:
                state["runs"].append({"test": t, "build_error": b})
                continue
            tag = f"{regress_id}_{Path(t).name}"
            r = sim.sim_run(b["elf"], tag=tag)
            if "error" in r:
                state["runs"].append({"test": t, "build_error": r})
                continue
            state["runs"].append({"test": t, "run_id": r["run_id"], "elab": r["elab"]})
        (rdir / f"{regress_id}.json").write_text(json.dumps(state))
    # wait for every run (shared budget)
    t_end = time.time() + wait_s
    rows = []
    for e in state["runs"]:
        if "build_error" in e:
            rows.append(_row(e["test"], None, None, e["build_error"]))
            continue
        left = max(0, int(t_end - time.time()))
        st = status.sim_status(e["run_id"], wait_s=left)
        rows.append(_row(e["test"], e["run_id"], st))
    passed = sum(r.get("result") == "pass" for r in rows)
    running = sum(r.get("result") == "running" for r in rows)
    out = {"regress_id": state["regress_id"], "passed": passed, "failed": len(rows) - passed - running,
           "running": running, "wall_s": int(time.time() - state["started"]), "tests": rows}
    if state.get("recompiled"):
        out["recompiled"] = state["recompiled"]
    if any(e.get("elab") == "rebuilding" for e in state["runs"]):
        out["note"] = "the design was re-elaborated first (~4 min)"
    if running:
        out["next"] = f"sim_regress(regress_id='{state['regress_id']}', wait_s=900)"
    return out
