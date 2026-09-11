"""Category C: sw_build, soc_lookup, soc_bootflow — build tests, navigate the SoC, explain the boot."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import typeinfo
from .config import Config, load
from .elf import ElfError, check_layout, elf_info


def _err(error, fix, **extra):
    d = {"error": error, "fix": fix}
    d.update(extra)
    return d


# ---------------------------------------------------------------- sw_build
_CERR_RE = re.compile(r"^(.+?):(\d+):(?:\d+:)?\s*(?:fatal )?error:\s*(.*)$")


def sw_build(test: str, extra_cflags: str | None = None, clean: bool = False) -> dict:
    cfg = load()
    d = Path(os.path.expanduser(test))
    if not d.is_dir():
        d = cfg.software / test
    if not (d / "Makefile").exists():
        known = sorted(p.name for p in cfg.software.iterdir() if (p / "Makefile").exists()) if cfg.software.exists() else []
        return _err("unknown test", f"no Makefile in {d}; tests under {cfg.software}: {', '.join(known[:40])}")
    name = d.name
    gcc = (f"riscv64-unknown-elf-gcc -include {cfg.uint_compat} -Wno-error=int-conversion "
           f"-Wno-error=implicit-function-declaration {extra_cflags or ''}").strip()
    env = dict(os.environ)
    env["PATH"] = f"{cfg.riscv_gcc_bin}:{env.get('PATH', '')}"
    env["SW_HOME"] = str(cfg.software)
    env["HW_HOME"] = str(cfg.hardware)
    cmds = []
    if clean:
        cmds.append(["make", "clean"])
    cmds.append(["make", "build", f"RISCV_GCC={gcc}"])
    log_lines = []
    for c in cmds:
        r = subprocess.run(c, cwd=str(d), capture_output=True, text=True, env=env, timeout=900)
        log_lines += (r.stdout + r.stderr).splitlines()
        if r.returncode != 0 and c[1] == "build":
            errs = []
            for l in log_lines:
                m = _CERR_RE.match(l)
                if m:
                    errs.append({"file": m.group(1).replace(str(cfg.software) + "/", ""), "line": int(m.group(2)), "msg": m.group(4)[:200]})
            log = cfg.binaries / f"{name}_build.log"
            log.write_text("\n".join(log_lines))
            return _err("build failed", "fix the reported errors", errors=errs[:15] or log_lines[-8:], log=str(log))
    elf = d / f"{name}.riscv"
    if not elf.exists():
        return _err("no ELF produced", f"expected {elf}")
    cfg.binaries.mkdir(parents=True, exist_ok=True)
    dst = cfg.binaries / f"{name}_l3.riscv"
    shutil.copyfile(elf, dst)
    try:
        info = elf_info(cfg, dst)
    except ElfError as e:
        return _err("elf unreadable", str(e))
    checks, problems = check_layout(cfg, info)
    from .elf import region_of
    sections = [{"name": s["name"], "addr": f"0x{s['addr']:08x}", "size": s["size"],
                 "region": region_of(cfg, s["addr"], s["size"])} for s in info["sections"]]
    out = {"test": name, "elf": str(dst), "entry": f"0x{info['entry']:08x}",
           "tohost": f"0x{info['symbols']['tohost']:08x}" if "tohost" in info["symbols"] else None,
           "size": info["size"], "sections": sections, "checks": checks, "problems": problems,
           "next": f"sim_run(binary='{dst}')" if not problems else "fix the layout problems before sim_run"}
    return out


# ---------------------------------------------------------------- soc_lookup
_CACHE = {}


def _address_map(cfg: Config) -> list[dict]:
    if "amap" in _CACHE:
        return _CACHE["amap"]
    entries = []
    inc = cfg.hardware / "include"
    for pkg in sorted(inc.glob("*pkg*.sv")):
        txt = pkg.read_text(errors="replace")
        bases = {m.group(1): int(m.group(2).replace("_", ""), 16)
                 for m in re.finditer(r"\b(\w+)Base\s*=\s*(?:64|32)'h([0-9A-Fa-f_]+)", txt)}
        lens = {m.group(1): int(m.group(2).replace("_", ""), 16)
                for m in re.finditer(r"\b(\w+)Length\s*=\s*(?:64|32)'h([0-9A-Fa-f_]+)", txt)}
        for n, b in bases.items():
            entries.append({"name": n, "base": b, "end": b + lens[n] if n in lens else None,
                            "length": lens.get(n), "source": f"include/{pkg.name}"})
    # rule tables '{idx: N, start_addr: 32'hX, end_addr: 32'hY},  // Name
    for f in sorted((cfg.hardware / "host").glob("*.sv")):
        txt = f.read_text(errors="replace")
        for m in re.finditer(r"idx:\s*32'd(\d+),\s*start_addr:\s*32'h([0-9A-Fa-f_]+),\s*end_addr:\s*32'h([0-9A-Fa-f_]+)\}\s*,?[ \t]*(?://[ \t]*([^\n]*))?", txt):
            b, e = int(m.group(2).replace("_", ""), 16), int(m.group(3).replace("_", ""), 16)
            entry = {"name": (m.group(4) or f"rule idx {m.group(1)}").strip(), "base": b, "end": e, "length": e - b,
                     "source": f"host/{f.name} (idx {m.group(1)})"}
            if not any(x["base"] == b and x["end"] == e and x["source"] == entry["source"] for x in entries):
                entries.append(entry)
    _CACHE["amap"] = entries
    return entries


def _hier_index(cfg: Config) -> dict:
    """module -> [(parent_module, instance_name, file, line, generate_label)] over every .sv the flow compiles."""
    if "hier" in _CACHE:
        return _CACHE["hier"]
    cache_file = cfg.simexp / "hier_cache.json"
    if cache_file.exists():
        _CACHE["hier"] = json.loads(cache_file.read_text())
        return _CACHE["hier"]
    files = set()
    for l in (cfg.work / "compile.sh").read_text().splitlines():
        for tok in l.split():
            if tok.endswith((".sv", ".v")) and os.path.exists(tok):
                files.add(tok)
    inst_re = re.compile(r"^[ \t]*([A-Za-z_]\w*)[ \t]*(?:#[ \t]*\((?:[^()]|\([^()]*\)|\((?:[^()]|\([^()]*\))*\))*\))?\s+([A-Za-z_]\w*)[ \t]*\(", re.M)
    kw = {"module", "input", "output", "inout", "logic", "wire", "reg", "assign", "always_comb", "always_ff", "if",
          "for", "else", "case", "typedef", "localparam", "parameter", "function", "task", "initial", "return", "generate",
          "begin", "end", "import", "int", "bit", "byte", "integer", "genvar", "unique", "priority", "automatic", "void",
          "static", "string", "enum", "struct", "packed", "signed", "unsigned", "endmodule", "endcase", "default"}
    index: dict[str, list] = {}
    for f in sorted(files):
        try:
            txt = Path(f).read_text(errors="replace")
        except OSError:
            continue
        txt = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), txt, flags=re.S)
        txt = re.sub(r"//[^\n]*", "", txt)
        lines = txt.split("\n")
        # per-line context: enclosing module and generate labels (begin : label after for/if)
        mod_at, label_at = [None] * (len(lines) + 1), [None] * (len(lines) + 1)
        cur, stack = None, []  # stack of begin blocks: generate label or None
        for ln, line in enumerate(lines, 1):
            m = re.match(r"^\s*module\s+([A-Za-z_]\w*)", line)
            if m:
                cur, stack = m.group(1), []
            # re-scan in order (findall loses which token was which): walk begin/end tokens explicitly
            stack_line = []
            for m2 in re.finditer(r"\bbegin\b(?:\s*:\s*([A-Za-z_]\w*))?|\bend\b", line):
                stack_line.append(m2)
            labels = [s for s in stack if s]
            mod_at[ln], label_at[ln] = cur, ".".join(labels) or None
            for m2 in stack_line:
                if m2.group(0).startswith("begin"):
                    lab = m2.group(1)
                    stack.append((lab + ("[i]" if re.search(r"\bfor\b", line) else "")) if lab and re.search(r"\b(for|if|else)\b|generate", line) else None)
                else:
                    if stack:
                        stack.pop()
        # instantiations may span lines: match over the whole text, map to the line of the module name
        for m in inst_re.finditer(txt):
            if m.group(1) in kw or m.group(2) in kw:
                continue
            ln = txt.count("\n", 0, m.start()) + 1
            if mod_at[ln] is None or mod_at[ln] == m.group(1):
                continue
            index.setdefault(m.group(1), []).append(
                [mod_at[ln], m.group(2), f.replace(str(cfg.hardware) + "/", ""), ln, label_at[ln]])
    # the testbench instantiates the SoC behind an `ifndef, which the regex cannot see: fix the root by hand
    if not any(p == "ariane_tb" for p, *_ in index.get("al_saqr", [])):
        index.setdefault("al_saqr", []).append(["ariane_tb", "dut", "tb/ariane_tb.sv", None, None])
    _CACHE["hier"] = index
    try:
        cache_file.write_text(json.dumps(index))
    except OSError:
        pass
    return index


def _paths_to(cfg: Config, module: str, depth: int = 0, seen=None) -> list[str]:
    index = _hier_index(cfg)
    if module in ("ariane_tb",):
        return ["tb"]
    seen = seen or set()
    out = []
    for parent, inst, _f, _ln, label in index.get(module, [])[:12]:
        if parent in seen or depth > 14:
            continue
        pref = (label + "." if label else "") + inst
        for pp in _paths_to(cfg, parent, depth + 1, seen | {module}):
            out.append(f"{pp}/{pref}")
    return out[:20]


def soc_lookup(query: str) -> dict:
    cfg = load()
    q = (query or "").strip()
    if not q:
        return _err("empty query", "pass an address (0x...), a module name, a typedef name or a signal name")
    if re.fullmatch(r"(0x)?[0-9A-Fa-f_]+", q) and (q.lower().startswith("0x") or len(q) >= 6):
        addr = int(q.replace("_", ""), 16)
        hits = [e for e in _address_map(cfg) if e["end"] is not None and e["base"] <= addr < e["end"]]
        hits.sort(key=lambda e: e["end"] - e["base"])
        if not hits:
            near = sorted(_address_map(cfg), key=lambda e: abs(e["base"] - addr))[:5]
            return {"query": q, "address": f"0x{addr:08x}", "matches": [], "nearest": [
                {"name": e["name"], "base": f"0x{e['base']:08x}", "source": e["source"]} for e in near]}
        return {"query": q, "address": f"0x{addr:08x}", "matches": [
            {"name": e["name"], "base": f"0x{e['base']:08x}", "end": f"0x{e['end']:08x}", "offset": f"0x{addr - e['base']:x}",
             "source": e["source"]} for e in hits[:8]],
            "note": "smallest enclosing window first; rule tables from axi_lite_subsystem/periph wrappers are named by their comment"}
    out = {"query": q}
    index = _hier_index(cfg)
    if q in index:
        out["module"] = {"instances": [{"parent": p, "instance": i, "file": f, "line": ln, "generate": lab}
                                       for p, i, f, ln, lab in index[q][:12]],
                         "paths": _paths_to(cfg, q),
                         "note": "paths are static (module nesting); generate indices appear as [i]; confirm with sim_session scope_list"}
    tdefs = typeinfo.build_index(cfg).get(q)
    if tdefs:
        out["typedef"] = {"layouts": tdefs[:4]}
    if "module" not in out and "typedef" not in out:
        # signal / identifier: where is it declared or assigned?
        hits = []
        try:
            r = subprocess.run(["grep", "-rn", "-m", "3", "-E", rf"\b{re.escape(q)}\b\s*(,|;|\)|=|\[)", "--include=*.sv",
                                str(cfg.hardware / "host"), str(cfg.hardware / "include"), str(cfg.hardware / "ip_list" / "cva6" / "core")],
                               capture_output=True, text=True, timeout=60)
            for l in r.stdout.splitlines()[:15]:
                f, ln, txt = l.split(":", 2)
                hits.append({"file": f.replace(str(cfg.hardware) + "/", ""), "line": int(ln), "text": txt.strip()[:120]})
        except Exception:
            pass
        out["identifier"] = {"hits": hits, "note": "no module or typedef of that name; grep hits in host/, include/ and cva6/core"}
    return out


# ---------------------------------------------------------------- soc_bootflow
def soc_bootflow() -> dict:
    return {
        "flow": "L3 (the only flow the tools support)",
        "memory": {"L2": "0x1C00_0000 + 32 KB SRAM: .tohost and .spm_data only", "L3": "0x8000_0000 + 512 MB simulation DRAM behind the LLC: code, data, stack",
                   "boot ROM": "0x1_0000", "SCMI mailbox": "0x1040_4000 (word 0 = boot address for woken cores, +0x24 completion irq)",
                   "PLIC": "0x0C00_0000 (context 2*hart+1 = M-mode of hart)", "UART (mock)": "0x1A10_0000"},
        "sequence": [
            {"t_ns": 0, "who": "tb", "what": "reset, FLL dummy clocks; cores held until ~1.5 ms"},
            {"t_ns": 1000000, "who": "tb JTAG", "what": "[JTAG] Initialization success (DTM idcode, dmactive)"},
            {"t_ns": 1550000, "who": "cores", "what": "all four cores leave reset and run the boot ROM; harts 1-3 wait in wfi for their boot interrupt"},
            {"t_ns": 2000000, "who": "tb JTAG", "what": "haltreq hart 0 -> [JTAG] Halted hart 0"},
            {"t_ns": 2110000, "who": "tb", "what": "[XSIM-L2]/[XSIM-L3] sections: ELF written straight into L2 banks and the sim DRAM"},
            {"t_ns": 2490000, "who": "tb JTAG", "what": "dpc = entry, resumereq -> [JTAG] Resumed hart 0 from 0x80000000"},
            {"t_ns": 2530000, "who": "core 0 startup (syscalls.c)", "what": "UART setup, PLIC priorities, mailbox word 0 = 0x80000000, completion irq -> core 1 wakes and jumps to the entry"},
            {"t_ns": 2560000, "who": "core 0", "what": "first Mock uart line (hello tests)"},
            {"t_ns": 2620000, "who": "test code", "what": "e.g. quad_boot: APMU counters armed -> overflow irqs (PLIC IDs 156/157) wake cores 2 and 3"},
            {"t_ns": 2840000, "who": "tb JTAG", "what": "tohost polled over SBA; exit code -> [JTAG] SUCCESS / FAILED, $finish"},
        ],
        "wakeups": {"core 1": "SCMI mailbox completion interrupt (PLIC source 10), raised by core 0's startup code",
                    "cores 2, 3": "APMU counter overflow interrupts (PLIC sources 156, 157), raised by the test's counter setup",
                    "any core": "JTAG haltreq/resume also works while a core is in wfi"},
        "failure_signatures": {
            "no UART, [JTAG] Halted hart 0 never printed": "hart 0 not halting: DM/DMI path (dm_mem ring) or JTAG timing",
            "state stalled, memory growing": "combinational ring -> sim_stall_trace",
            "state stalled, memory flat": "deadlock: a handshake never completes -> sim_session probe of valid/ready pairs",
            "core prints once then nothing, SUCCESS missing": "test never writes tohost, or wrong tohost address (tb polls 0x1C00_0000 for L3 builds)",
            "secondary core silent": "it jumped to the mailbox word (0x80000000) before the program was there, or its PLIC context is not enabled"},
        "markers": ["[JTAG] Initialization success", "[JTAG] Halted hart 0", "[XSIM-L3] section at 0x80000000",
                    "[JTAG] Resumed hart 0", "Mock uart ...", "[JTAG] SUCCESS", "$finish"],
        "rates": {"sim_run": "~40 s wall per simulated ms", "sim_session/trace (debug snapshot)": "~80 s wall per simulated ms"},
    }
