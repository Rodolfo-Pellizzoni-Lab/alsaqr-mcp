"""Configuration for the AlSaqr simulation tools (paths, build switches).

The tools drive the in-tree xsim flow of the he-soc `xsim-port` branch: sources are the repository files,
the compiled library is hardware/xsim/work (built by hardware/xsim/build.sh), and the tools keep their own
state (runs, sessions, traces, caches, program copies) under `state`.
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = Path(os.environ.get("ALSAQR_MCP_CONFIG", ROOT / "config.json"))

_PATH_KEYS = ("hardware", "software", "work", "state", "tb_file", "dpi_dir", "uint_compat", "riscv_gcc_bin", "readelf")


class Config:
    def __init__(self, data: dict):
        self._d = data
        for k in _PATH_KEYS:
            setattr(self, k, Path(os.path.expanduser(data[k])))
        self.repo = self.hardware.parent
        self.runs = self.state / "runs"
        self.sessions = self.state / "sessions"
        self.traces = self.state / "traces"
        self.binaries = self.state / "bin"       # sw_build copies ELFs here so rebuilding a test cannot affect a live run
        self.vivado_settings = data["vivado_settings"]
        self.tb_defines = list(data["tb_defines"])
        self.xelab_generics = dict(data["xelab_generics"])
        self.plusargs = dict(data["plusargs"])
        mm = data["memory_map"]
        self.sram_base = int(mm["sram"]["base"], 16)   # on-chip SRAM (holds .tohost and small shared data)
        self.sram_size = int(mm["sram"]["size"], 16)
        self.dram_base = int(mm["dram"]["base"], 16)   # simulated DRAM: a byte array behind the LLC
        self.dram_size = int(mm["dram"]["size"], 16)
        self.entry = int(mm["entry"], 16)
        self.stall_idle_s = int(data.get("stall_idle_s", 180))
        self.default_timeout_s = int(data.get("default_timeout_s", 7200))
        self.runner = ROOT / "scripts" / "sim_runner.sh"
        # alsaqr-software checkout whose toolchains build tests that live outside he-soc software/ (optional)
        tb = data.get("toolchain_bundle")
        self.toolchain_bundle = Path(os.path.expanduser(tb)) if tb else None

    def flow_problem(self) -> dict | None:
        """{error, fix} when the xsim flow is not usable (wrong branch, library not built), else None."""
        if not (self.hardware / "xsim" / "build.sh").exists():
            return {"error": "xsim flow not found",
                    "fix": f"{self.repo} is not on the xsim-port branch: git -C {self.repo} checkout xsim-port"}
        if not (self.work / "compile.sh").exists() or not (self.dpi_dir / "libdpi.so").exists():
            return {"error": "no compiled library",
                    "fix": f"build it once: {self.hardware}/xsim/build.sh (~2 min)"}
        return None


def load() -> Config:
    with open(CONFIG_FILE) as f:
        return Config(json.load(f))
