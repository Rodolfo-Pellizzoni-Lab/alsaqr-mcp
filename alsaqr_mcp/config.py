"""Configuration for the AlSaqr simulation tools (paths, build switches)."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = Path(os.environ.get("ALSAQR_MCP_CONFIG", ROOT / "config.json"))

_PATH_KEYS = ("simexp", "work", "overrides", "override_map", "tb_file", "dpi_dir", "runs", "readelf",
              "riscv_gcc_bin", "uint_compat", "binaries", "hardware", "software")


class Config:
    def __init__(self, data: dict):
        self._d = data
        for k in _PATH_KEYS:
            setattr(self, k, Path(os.path.expanduser(data[k])))
        self.vivado_settings = data["vivado_settings"]
        self.tb_defines = list(data["tb_defines"])
        self.recompile_defines = dict(data.get("recompile_defines", {}))
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


def load() -> Config:
    with open(CONFIG_FILE) as f:
        return Config(json.load(f))
