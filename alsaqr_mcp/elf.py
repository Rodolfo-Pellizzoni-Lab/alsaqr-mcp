"""ELF inspection through the RISC-V readelf (no Python dependencies)."""
import re
import subprocess
from pathlib import Path

from .config import Config

_SEC_RE = re.compile(
    r"^\s*\[\s*\d+\]\s+(\S+)\s+(\S+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+\S+\s+([A-Za-z]*)\s"
)
_SYM_RE = re.compile(r"^\s*\d+:\s+([0-9a-fA-F]+)\s+\d+\s+\S+\s+\S+\s+\S+\s+\S+\s+(\S+)\s*$")


class ElfError(Exception):
    pass


def elf_info(cfg: Config, path: Path) -> dict:
    """Return entry, loadable sections and a few symbols of an ELF file."""
    try:
        out = subprocess.run(
            [str(cfg.readelf), "-W", "-h", "-S", "-s", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except OSError as e:
        raise ElfError(f"cannot run readelf: {e}")
    if out.returncode != 0:
        raise ElfError(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "readelf failed")
    entry = None
    sections, symbols = [], {}
    for line in out.stdout.splitlines():
        if "Entry point address" in line:
            entry = int(line.split(":")[1].strip(), 16)
            continue
        m = _SEC_RE.match(line)
        if m and "A" in m.group(6):
            name, typ, addr, _off, size = m.group(1), m.group(2), int(m.group(3), 16), m.group(4), int(m.group(5), 16)
            if size:
                sections.append({"name": name, "type": typ, "addr": addr, "size": size})
            continue
        m = _SYM_RE.match(line)
        if m and m.group(2) in ("tohost", "fromhost", "_start", "main", "thread_entry"):
            symbols[m.group(2)] = int(m.group(1), 16)
    if entry is None:
        raise ElfError("not an ELF file (no entry point)")
    return {"entry": entry, "sections": sections, "symbols": symbols, "size": path.stat().st_size}


def region_of(cfg: Config, addr: int, size: int) -> str | None:
    if cfg.sram_base <= addr and addr + size <= cfg.sram_base + cfg.sram_size:
        return "sram"
    if cfg.dram_base <= addr and addr + size <= cfg.dram_base + cfg.dram_size:
        return "dram"
    return None


def check_layout(cfg: Config, info: dict) -> tuple[list[str], list[dict]]:
    """Checks that the program is laid out the way the simulator loads it: code and data in DRAM
    (entry 0x80000000), the tohost word in the on-chip SRAM. Returns (passed checks, problems)."""
    passed, problems = [], []
    entry = info["entry"]
    if entry == cfg.entry:
        passed.append(f"entry 0x{entry:08x} is in DRAM")
    elif region_of(cfg, entry, 4) == "sram":
        problems.append({
            "error": "program linked for the wrong memory",
            "fix": "the simulator loads code and data into DRAM at 0x80000000: build the test with `make build` "
                   "(or sw_build), which uses the DRAM linker script; this binary was linked for the on-chip SRAM",
        })
    else:
        problems.append({"error": f"unexpected entry point 0x{entry:x}",
                         "fix": "the entry must be 0x80000000 (DRAM); build with `make build` or sw_build"})
    tohost = info["symbols"].get("tohost")
    if tohost is None:
        problems.append({"error": "no tohost symbol",
                         "fix": "link with the common crt/syscalls: the testbench reads the exit code from the tohost word"})
    elif region_of(cfg, tohost, 8) == "sram":
        passed.append(f"tohost 0x{tohost:08x} in the on-chip SRAM")
    else:
        problems.append({"error": f"tohost 0x{tohost:x} is not in the on-chip SRAM",
                         "fix": "the testbench polls tohost at 0x1C000000; use the standard linker script"})
    outside = [s for s in info["sections"] if region_of(cfg, s["addr"], s["size"]) is None]
    if outside:
        problems.append({"error": "sections outside DRAM/SRAM",
                         "fix": "sections: " + ", ".join(f"{s['name']}@0x{s['addr']:x}+{s['size']}" for s in outside[:6])})
    else:
        passed.append(f"{len(info['sections'])} loadable sections inside DRAM/SRAM")
    return passed, problems
