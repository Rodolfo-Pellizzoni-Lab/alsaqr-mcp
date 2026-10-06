"""Isolated he-soc sandboxes for benchmark runs.

A sandbox is a lean copy of a he-soc checkout (sources, .git, software, the compiled xsim library and the current
snapshots; no FPGA tree, no old run logs, no past MCP runs) whose every path points inside the sandbox:
compile.sh, vsim.tcl and the MCP's compile records are rewritten, so an agent, `run.sh` or `rtl_recompile` can only
read and build the sandbox's own files. The MCP's snapshot stamp hashes those paths, so the copied snapshots are
symlinked under the sandbox's stamp (xsim snapshots embed their own directory name and cannot be renamed).

A sandbox can itself be the source of other sandboxes (a "template" holding a planted bug that has been compiled and
elaborated once).
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

MCP_ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()


def _mcp_py(config: Path, code: str) -> str:
    env = dict(os.environ, PYTHONPATH=str(MCP_ROOT), ALSAQR_MCP_CONFIG=str(config))
    return subprocess.run(["python3", "-c", code], env=env, capture_output=True, text=True, check=True).stdout.strip()


def current_snapshot(config: Path) -> str:
    """Name of the snapshot the MCP would use for this checkout (tb_<stamp>)."""
    return _mcp_py(config, "from alsaqr_mcp import sim, config; print(sim.snapshot_name(sim.snapshot_stamp(config.load())))")


def write_config(sb: Path, base_config: Path):
    c = json.loads(base_config.read_text())
    h = sb / "he-soc"
    c.update({"hardware": f"{h}/hardware", "software": f"{h}/software", "work": f"{h}/hardware/xsim/work",
              "state": f"{h}/hardware/xsim/work/mcp", "tb_file": f"{h}/hardware/tb/ariane_tb.sv",
              "dpi_dir": f"{h}/hardware/xsim/work", "uint_compat": f"{h}/hardware/xsim/sw/uint_compat.h"})
    (sb / "mcp_config.json").write_text(json.dumps(c, indent=1))
    (sb / "mcp_servers.json").write_text(json.dumps({"mcpServers": {"alsaqr": {
        "command": "python3", "args": ["-m", "alsaqr_mcp", "serve"],
        "env": {"PYTHONPATH": str(MCP_ROOT), "ALSAQR_MCP_CONFIG": str(sb / "mcp_config.json")}}}}, indent=1))


def make(sb: Path, source: Path | None = None) -> Path:
    """Create sandbox `sb` from `source` (a he-soc checkout, default ~/he-soc, or another sandbox directory)."""
    if sb.exists():
        raise FileExistsError(sb)
    if source is None:
        src_repo, src_config = HOME / "he-soc", MCP_ROOT / "config.json"
    else:
        src_repo, src_config = source / "he-soc", source / "mcp_config.json"
    snap = current_snapshot(src_config)
    sb.mkdir(parents=True)
    (sb / "scratch").mkdir()
    dst = sb / "he-soc"
    subprocess.run(["rsync", "-a", "--exclude", "/hardware/fpga", "--exclude", "/hardware/xsim/work",
                    f"{src_repo}/", f"{dst}/"], check=True)
    # the FPGA tree is GBs of untracked build output: copy only its tracked files, so git sees no deletions
    tracked = subprocess.run(["git", "-C", str(src_repo), "ls-files", "hardware/fpga"], capture_output=True,
                             text=True, check=True).stdout
    subprocess.run(["rsync", "-a", "--files-from=-", f"{src_repo}/", f"{dst}/"], input=tracked, text=True, check=True)
    sw, dw = src_repo / "hardware/xsim/work", dst / "hardware/xsim/work"
    (dw / "xsim.dir").mkdir(parents=True)
    (dw / "mcp").mkdir()
    files = ["compile.sh", "compile.log", "libdpi.so", "tb_units.txt", "vsim.tcl", ".tb_xvlog_start", "xvlog.pb",
             "xvhdl.pb"]
    subprocess.run(["rsync", "-a", *[str(sw / f) for f in files if (sw / f).exists()], f"{dw}/"], check=True)
    snaps = [s for s in (snap, snap + "_dbg") if (sw / "xsim.dir" / s / "xsimk").exists() and (sw / f"{s}.ok").exists()]
    real = {s: (sw / "xsim.dir" / s).resolve().name for s in snaps}  # a template's snapshot may be a symlink
    for s in snaps:
        subprocess.run(["rsync", "-a", f"{sw}/xsim.dir/{real[s]}/", f"{dw}/xsim.dir/{real[s]}/"], check=True)
    subprocess.run(["rsync", "-a", f"{sw}/xsim.dir/work", f"{dw}/xsim.dir/"], check=True)
    mcp_files = [sw / "mcp" / f for f in ("compiled.json", "hier_cache.json", "typeinfo_cache.json", "recompile")]
    subprocess.run(["rsync", "-a", *[str(p) for p in mcp_files if p.exists()], f"{dw}/mcp/"], check=True)

    # every path into the sandbox (keep compile.sh's mtime: the MCP keys its file list on it)
    old, new = f"{src_repo}/", f"{dst}/"
    for f in ("compile.sh", "vsim.tcl"):
        p = dw / f
        if p.exists():
            st = p.stat()
            p.write_text(p.read_text().replace(old, new))
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    cj = dw / "mcp" / "compiled.json"
    if cj.exists():
        cj.write_text(json.dumps({k.replace(old, new): v for k, v in json.loads(cj.read_text()).items()}, indent=1))
    # run.sh's watchdog matches any `xsimk ... tb_l3` and, 180 s after its own run ends, kills it: scope it to this
    # run's ELF so parallel sandboxes cannot kill each other's simulations (behaviour of a single run unchanged)
    rs = dst / "hardware/xsim/run.sh"
    rs.write_text(rs.read_text().replace('x[s]imk.*$snap"', 'x[s]imk.*CVA6_STRING=$elf "'))
    subprocess.run(["git", "-C", str(dst), "update-index", "--skip-worktree", "hardware/xsim/run.sh"], check=True)

    write_config(sb, MCP_ROOT / "config.json")
    new_snap = current_snapshot(sb / "mcp_config.json")
    for s in snaps:
        n = new_snap + s[len(snap):]
        if n != real[s]:
            (dw / "xsim.dir" / n).symlink_to(real[s])
        (dw / f"{n}.ok").touch()
    return sb


def install(sb: Path, src: Path, rel: str):
    """Copy a fixture file or directory into the sandbox at `rel`."""
    d = sb / rel
    d.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, d, symlinks=True)
    else:
        shutil.copy2(src, d)


if __name__ == "__main__":
    import sys
    p = make(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None)
    print(p)
