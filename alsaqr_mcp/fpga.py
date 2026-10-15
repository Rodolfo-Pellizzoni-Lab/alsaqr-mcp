"""VCU118 bitstream builds of the he-soc working tree: fpga_build (detached), fpga_status (progress, timing gate, verdict).

A build runs scripts/fpga_build.sh in its own session: the in-tree run.tcl project setup + synthesis (~40 min), one
linked checkpoint (IP contents + constraints), then opt / place / route / timing gate / bitstream in non-project mode
(~2.5 h). State lives in <state>/fpga/<build_id>/build.json; Vivado logs and reports stay next to it.
"""
import datetime
import json
import os
import subprocess
import time
from pathlib import Path

from .config import ROOT, load

MAX_WAIT_S = 900
TERMINAL = ("done", "failed")
PHASES = ["queued", "constr", "synth", "link", "opt", "place", "route", "route_physopt", "gate", "bitstream", "done"]


def _err(error, fix, **extra):
    d = {"error": error, "fix": fix}
    d.update(extra)
    return d


def _git(cfg):
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=cfg.repo, capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=cfg.repo,
                               capture_output=True, text=True).stdout.strip()
        return {"head": head, "modified_files": len(dirty.splitlines())}
    except Exception:
        return {}


def _state(cfg, build_id):
    f = cfg.fpga_builds / build_id / "build.json"
    return json.loads(f.read_text()) if f.exists() else None


def fpga_build(tag: str | None = None, opt: str | None = None, place: str | None = None, route: str | None = None,
               reuse_synth: str | None = None) -> dict:
    cfg = load()
    if not cfg.fpga_dir or not (cfg.fpga_dir / "run.tcl").exists():
        return _err("no FPGA flow", f"config fpga.dir ({cfg.fpga_dir}) has no run.tcl")
    cfg.fpga_builds.mkdir(parents=True, exist_ok=True)
    build_id = (tag or time.strftime("fb%m%d_%H%M%S")).replace("/", "_")
    bdir = cfg.fpga_builds / build_id
    if bdir.exists():
        return _err("build_id exists", f"choose another tag (builds: {', '.join(sorted(p.name for p in cfg.fpga_builds.iterdir())[-8:])})")
    synth_dcp = ""
    if reuse_synth:
        src = _state(cfg, reuse_synth) if reuse_synth != "last" else next(
            (s for s in (_state(cfg, p.name) for p in sorted(cfg.fpga_builds.iterdir(), key=lambda p: p.stat().st_mtime,
                                                               reverse=True)) if s and s.get("synth_dcp")), None)
        if not src or not src.get("synth_dcp") or not Path(src["synth_dcp"]).exists():
            return _err("no synthesized checkpoint to reuse", "give the build_id of a build that completed synthesis, or omit reuse_synth")
        synth_dcp = src["synth_dcp"]
    bdir.mkdir()
    directives = {"opt": opt or cfg.fpga_directives["opt"], "place": place or cfg.fpga_directives["place"],
                  "route": route or cfg.fpga_directives["route"]}
    state = {"build_id": build_id, "phase": "queued", "started": time.time(), "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "directives": directives, "soc_mhz": cfg.fpga_soc_mhz, "tree": _git(cfg),
             "reused_synth": reuse_synth or None, "synth_dcp": synth_dcp or None}
    (bdir / "build.json").write_text(json.dumps(state, indent=1))
    env = dict(os.environ)
    env.update({"BUILD_DIR": str(bdir), "FPGA_DIR": str(cfg.fpga_dir), "SYNTH_DCP": synth_dcp,
                "OPT_DIR": directives["opt"], "PLACE_DIR": directives["place"], "ROUTE_DIR": directives["route"],
                "SOC_PERIOD_NS": f"{1000.0 / cfg.fpga_soc_mhz:g}", "VIVADO_SETTINGS": cfg.vivado_settings,
                "FPGA_ENV": "\n".join(f"{k}={v}" for k, v in cfg.fpga_env.items()), "SCRIPTS": str(ROOT / "scripts")})
    if cfg.fpga_path_prepend:
        env["PATH"] = cfg.fpga_path_prepend + ":" + env.get("PATH", "")
    with open(bdir / "runner.log", "w") as out:
        subprocess.Popen(["setsid", "bash", str(ROOT / "scripts" / "fpga_build.sh")], cwd=str(cfg.fpga_dir), env=env,
                         stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    return {"build_id": build_id, "phase": "queued", "directives": directives, "reused_synth": bool(synth_dcp),
            "expect": "synthesis ~40 min, implementation ~2-3 h" if not synth_dcp else "implementation ~2-3 h",
            "next": f"fpga_status(build_id='{build_id}', wait_s=900)"}


def _summary(cfg, st: dict) -> dict:
    bdir = cfg.fpga_builds / st["build_id"]
    t_end = time.time()
    if st["phase"] in ("done", "failed") and st.get("updated_at"):   # elapsed stops at the end of the build
        t_end = datetime.datetime.fromisoformat(st["updated_at"]).timestamp()
    out = {"build_id": st["build_id"], "phase": st["phase"], "elapsed_s": int(t_end - st["started"]),
           "directives": st.get("directives"), "tree": st.get("tree")}
    if st.get("wns_place") is not None:
        out["wns_place"] = st["wns_place"]
    timing = {k: st[k] for k in ("setup_failing", "hold_failing_outside_adv_timer", "hold_failing_adv_timer", "wns", "whs",
                                 "unrouted", "soc_clk_mhz", "clk_constraint_mhz") if k in st}
    if timing:
        out["timing"] = timing
        # gate.txt: the gate line, "mmcm ..." and then one indented line per failing hold endpoint outside the class
        gate_lines = (bdir / "gate.txt").read_text().splitlines()[1:] if (bdir / "gate.txt").exists() else []
        hold_list = [l.strip() for l in gate_lines if l.startswith("  ")]
        if hold_list:
            out["hold_failing_endpoints"] = hold_list[:8]
        # where every number comes from, so the routed checkpoint and the reports need not be opened again
        out["sources"] = {
            "setup_failing, wns, whs": "timing paths of the routed design (post_route.dcp, the same analysis as "
                                       "post_route_timing_summary.rpt), after the final route",
            "hold_failing_adv_timer": "failing hold endpoints apb_adv_timer u_in_stage/r_ls_clk_sync_reg: a clock used "
                                      "as data, the one accepted class; every other failing hold endpoint is listed",
            "unrouted": "nets whose ROUTE_STATUS is not ROUTED / INTRASITE / NOLOADS / NODRIVER (report_route_status)",
            "soc_clk_mhz": "the MMCM's attributes in the routed design, 1000/CLKIN1_PERIOD*CLKFBOUT_MULT_F/DIVCLK_DIVIDE"
                           "/CLKOUT0_DIVIDE_F: " + (st.get("mmcm") or "(MMCM attributes not recorded by this build)"),
            "clk_constraint_mhz": "period of the generated clock clk_out1_xilinx_clk_mngr the timing analysis used",
        }
    if st["phase"] == "done":
        out["verdict"] = st.get("verdict")
        out["bitstream"] = st.get("bitstream")
        out["next"] = (f"board_program(bitstream='{st.get('bitstream')}') then board_run / board_regress / linux_boot"
                       if st.get("verdict") == "OK" else "the timing gate failed: see timing / hold_failing_endpoints, "
                       "post_route_timing_summary.rpt; a different placement directive (SSI_SpreadLogic_high) or an RTL fix")
    if st["phase"] == "failed":
        out["error"] = st.get("error")
        out["stage"] = st.get("stage")
        ef = bdir / "errors.txt"
        if ef.exists() and ef.read_text().strip():
            out["errors"] = ef.read_text().splitlines()[:5]
    if st.get("reused_synth"):
        out["reused_synth"] = st["reused_synth"]
    out["logs"] = str(bdir)
    return out


def fpga_status(build_id: str | None = None, wait_s: int = 0) -> dict:
    cfg = load()
    if not build_id:
        builds = sorted((p for p in cfg.fpga_builds.glob("*/build.json")), key=lambda p: p.stat().st_mtime, reverse=True)[:10] \
            if cfg.fpga_builds.exists() else []
        rows = []
        for b in builds:
            st = json.loads(b.read_text())
            rows.append({"build_id": st["build_id"], "phase": st["phase"], "verdict": st.get("verdict"),
                         "started_at": st.get("started_at"), "bitstream": st.get("bitstream")})
        return {"builds": rows, "note": "fpga_status(build_id=...) for details"}
    st = _state(cfg, build_id)
    if st is None:
        return _err("unknown build_id", "fpga_status() lists builds")
    wait_s = max(0, min(int(wait_s or 0), MAX_WAIT_S))
    t_end = time.time() + wait_s
    while st["phase"] not in TERMINAL and time.time() < t_end:
        time.sleep(10)
        st = _state(cfg, build_id)
    out = _summary(cfg, st)
    if st["phase"] not in TERMINAL:
        out["next"] = f"fpga_status(build_id='{build_id}', wait_s=900)"
    return out
