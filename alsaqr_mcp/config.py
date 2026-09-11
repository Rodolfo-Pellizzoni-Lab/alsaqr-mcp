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
        self.l2_base = int(mm["l2"]["base"], 16)
        self.l2_size = int(mm["l2"]["size"], 16)
        self.l3_base = int(mm["l3"]["base"], 16)
        self.l3_size = int(mm["l3"]["size"], 16)
        self.entry = int(mm["entry"], 16)
        self.stall_idle_s = int(data.get("stall_idle_s", 180))
        self.default_timeout_s = int(data.get("default_timeout_s", 7200))
        self.runner = ROOT / "scripts" / "sim_runner.sh"


def load() -> Config:
    with open(CONFIG_FILE) as f:
        return Config(json.load(f))
