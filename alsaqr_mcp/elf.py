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
    if cfg.l2_base <= addr and addr + size <= cfg.l2_base + cfg.l2_size:
        return "l2"
    if cfg.l3_base <= addr and addr + size <= cfg.l3_base + cfg.l3_size:
        return "l3"
    return None


def check_layout(cfg: Config, info: dict) -> tuple[list[str], list[dict]]:
    """Checks the binary fits the single supported (L3) boot flow.

    Returns (passed checks, problems); each problem has 'error' and 'fix'."""
    passed, problems = [], []
    entry = info["entry"]
    if entry == cfg.entry:
        passed.append(f"entry 0x{entry:08x} is the L3 entry")
    elif region_of(cfg, entry, 4) == "l2":
        problems.append({
            "error": "l2-linked binary",
            "fix": "link against test.ld: run `make build RISCV_GCC=...` (not `make build_l2`) so code lives at "
                   "0x80000000 behind the LLC; the tools only support the L3 boot flow",
        })
    else:
        problems.append({"error": f"unexpected entry point 0x{entry:x}",
                         "fix": "the entry must be 0x80000000 (test.ld)"})
    tohost = info["symbols"].get("tohost")
    if tohost is None:
        problems.append({"error": "no tohost symbol", "fix": "link with the common crt/syscalls (tohost is how the tb sees the exit code)"})
    elif region_of(cfg, tohost, 8) == "l2":
        passed.append(f"tohost 0x{tohost:08x} in L2")
    else:
        problems.append({"error": f"tohost 0x{tohost:x} is not in L2",
                         "fix": "test.ld places .tohost in L2 (0x1C000000); the tb polls that address"})
    outside = [s for s in info["sections"] if region_of(cfg, s["addr"], s["size"]) is None]
    if outside:
        problems.append({"error": "sections outside L2/L3",
                         "fix": "sections: " + ", ".join(f"{s['name']}@0x{s['addr']:x}+{s['size']}" for s in outside[:6])})
    else:
        passed.append(f"{len(info['sections'])} loadable sections inside L2/L3")
    return passed, problems
