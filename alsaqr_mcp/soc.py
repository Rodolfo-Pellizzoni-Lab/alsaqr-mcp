"""Category C: sw_build, soc_lookup, soc_bootflow — build tests, navigate the SoC, explain the boot."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import sources, typeinfo
from .config import Config, load
from .elf import ElfError, check_layout, elf_info


def _err(error, fix, **extra):
    d = {"error": error, "fix": fix}
    d.update(extra)
    return d


# ---------------------------------------------------------------- sw_build
_CERR_RE = re.compile(r"^(.+?):(\d+):(?:\d+:)?\s*(?:fatal )?error:\s*(.*)$")


def _bundle_of(d: Path) -> Path | None:
    """The alsaqr-software checkout a test directory belongs to (it ships its own toolchains), else None."""
    for parent in (d, *d.parents):
        if (parent / "source.sh").exists() and (parent / "toolchain" / "rv64" / "bin").is_dir():
            return parent
    return None


def sw_build(test: str, extra_cflags: str | None = None, clean: bool = False, target: str = "build") -> dict:
    cfg = load()
    d = Path(os.path.expanduser(test))
    if not d.is_dir():
        d = cfg.software / test
    if not (d / "Makefile").exists():
        known = sorted(p.name for p in cfg.software.iterdir() if (p / "Makefile").exists()) if cfg.software.exists() else []
        return _err("unknown test", f"no Makefile in {d}; tests under {cfg.software}: {', '.join(known[:40])}"
                    " (a directory path, e.g. an alsaqr-software test, is accepted too)")
    d = d.resolve()
    name = d.name
    target = target or "build"
    cfg.binaries.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    bundle = _bundle_of(d)
    if bundle:
        # alsaqr-software: its own rv64 gcc, the rv32 gcc + riscv-none-elf-* shim for the APMU firmware
        tc = bundle / "toolchain"
        env["PATH"] = ":".join(str(p) for p in (tc / "shim", tc / "rv32" / "bin", tc / "rv64" / "bin")) + ":" + env.get("PATH", "")
        env["SW_HOME"] = str(bundle / "tests")
        env["ALSAQR_ROOT"] = str(bundle)
        gcc = f"riscv64-unknown-elf-gcc {extra_cflags or ''}".strip()
    else:
        env["PATH"] = f"{cfg.riscv_gcc_bin}:{env.get('PATH', '')}"
        env["SW_HOME"] = str(cfg.software)
        gcc = (f"riscv64-unknown-elf-gcc -include {cfg.uint_compat} -Wno-error=int-conversion "
               f"-Wno-error=implicit-function-declaration {extra_cflags or ''}").strip()
    env["HW_HOME"] = str(cfg.hardware)
    cmds = []
    if clean:
        cmds.append(["make", "clean"])
    cmds.append(["make", target, f"RISCV_GCC={gcc}"])
    log_lines = []
    for c in cmds:
        r = subprocess.run(c, cwd=str(d), capture_output=True, text=True, env=env, timeout=900)
        log_lines += (r.stdout + r.stderr).splitlines()
        if r.returncode != 0 and c[1] == target:
            errs = []
            for l in log_lines:
                m = _CERR_RE.match(l)
                if m:
                    errs.append({"file": m.group(1).replace(str(d.parent) + "/", ""), "line": int(m.group(2)), "msg": m.group(4)[:200]})
            log = cfg.binaries / f"{name}_build.log"
            log.write_text("\n".join(log_lines))
            return _err("build failed", "fix the reported errors", errors=errs[:15] or log_lines[-8:], log=str(log))
    # the ELF is <test>.riscv for `make build`; other targets name it after themselves (pmu_bench -> pmu_bench.riscv)
    candidates = [d / f"{name}.riscv"] if target == "build" else [d / f"{target}.riscv", d / f"{name}.riscv"]
    elf = next((e for e in candidates if e.exists()), None)
    if elf is None:
        found = sorted(p.name for p in d.glob("*.riscv"))
        return _err("no ELF produced", f"expected {' or '.join(str(c) for c in candidates)}"
                    + (f"; ELFs in {d}: {', '.join(found)}" if found else ""))
    dst = cfg.binaries / (f"{name}.riscv" if elf.stem == name else f"{name}_{elf.stem}.riscv")
    shutil.copyfile(elf, dst)
    try:
        info = elf_info(cfg, dst)
    except ElfError as e:
        return _err("elf unreadable", str(e))
    checks, problems = check_layout(cfg, info)
    from .elf import region_of
    sections = [{"name": s["name"], "addr": f"0x{s['addr']:08x}", "size": s["size"],
                 "region": region_of(cfg, s["addr"], s["size"])} for s in info["sections"]]
    out = {"test": name, "target": target, "toolchain": "alsaqr-software bundle" if bundle else str(cfg.riscv_gcc_bin),
           "elf": str(dst), "entry": f"0x{info['entry']:08x}",
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
    # windows the RTL tables leave unnamed: name the table entry instead of adding a second one
    for name, base, end, src in (("SCMI mailbox", 0x10404000, 0x10405000, "host/axi_lite_subsystem.sv (idx 4)"),):
        same = [e for e in entries if e["base"] == base and e["end"] == end]
        for e in same:
            if e["name"].startswith("rule idx"):
                e["name"] = name
        if not same:
            entries.append({"name": name, "base": base, "end": end, "length": end - base, "source": src})
    _CACHE["amap"] = entries
    return entries


def _apmu_windows(cfg: Config) -> list[dict]:
    """APMU sub-windows (instruction/data scratchpads, counters) from the software's pmu_defines.h."""
    if "apmu" in _CACHE:
        return _CACHE["apmu"]
    out = []
    hdr = cfg.software / "quad_boot" / "pmu_defines.h"
    if hdr.exists():
        for m in re.finditer(r"#define\s+(\w*(?:BASE_ADDR|_ADDR))\s+(0x[0-9A-Fa-f]+)", hdr.read_text(errors="replace")):
            name = m.group(1).replace("_BASE_ADDR", "").replace("_ADDR", "")
            out.append({"name": f"APMU {name}", "base": int(m.group(2), 16), "end": None, "source": "software/quad_boot/pmu_defines.h"})
    _CACHE["apmu"] = out
    return out


def _hier_index(cfg: Config) -> dict:
    """module -> [(parent_module, instance_name, file, line, generate_label)] over every .sv the flow compiles.
    Cached on disk and rebuilt when a compiled source file changes."""
    fp = sources.fingerprint(cfg)
    if _CACHE.get("hier_fp") == fp:
        return _CACHE["hier"]
    cache_file = cfg.state / "hier_cache.json"
    try:
        cached = json.loads(cache_file.read_text())
        if cached.get("fingerprint") == fp:
            _CACHE["hier"], _CACHE["hier_fp"] = cached["index"], fp
            return cached["index"]
    except (OSError, ValueError, AttributeError, KeyError):
        pass
    files = {str(f) for f in sources.compiled_files(cfg) if f.suffix in (".sv", ".v") and f.exists()}
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
    _CACHE["hier"], _CACHE["hier_fp"] = index, fp
    try:
        cfg.state.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"fingerprint": fp, "index": index}))
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
    # a subsystem / window name: UART, PLIC, mailbox, ISPM, ...
    ql = q.lower()
    windows = [e for e in _address_map(cfg) if ql in e["name"].lower().replace("_", "")
               or ql in e["name"].lower()] + [e for e in _apmu_windows(cfg) if ql in e["name"].lower()]
    if windows:
        seen, uniq = set(), []
        for e in windows:
            key = (e["name"], e["base"])
            if key not in seen:
                seen.add(key)
                uniq.append({"name": e["name"], "base": f"0x{e['base']:08x}",
                             "end": f"0x{e['end']:08x}" if e.get("end") else None, "source": e["source"]})
        out["windows"] = uniq[:12]
    index = _hier_index(cfg)
    if q in index:
        out["module"] = {"instances": [{"parent": p, "instance": i, "file": f, "line": ln, "generate": lab}
                                       for p, i, f, ln, lab in index[q][:12]],
                         "paths": _paths_to(cfg, q),
                         "note": "paths are static (module nesting); generate indices appear as [i]; confirm with sim_session scope_list"}
    tname = q.split("::")[-1]  # pkg::type -> type (a name may have one layout per package)
    tdefs = typeinfo.build_index(cfg).get(tname)
    if tdefs:
        out["typedef"] = {"layouts": tdefs[:4]}
        if tname != q or len(tdefs) > 1:
            out["typedef"]["note"] = f"layouts of every typedef named {tname}, whichever package declares it"
    if "module" not in out and "typedef" not in out and "windows" not in out:
        out["identifier"] = _find_identifier(cfg, q)
    return out


# a line that defines or declares the identifier rather than just using it
_DEFINING_RE = re.compile(r"^\s*(`define|`undef|localparam|parameter|typedef|module|interface|package|"
                          r"input|output|inout|logic|wire|reg|bit|int|integer|genvar|struct|enum|function|task|class)\b")
_CONDITIONAL_RE = re.compile(r"^\s*`(ifn?def|elsif)\b")


def _find_identifier(cfg: Config, q: str) -> dict:
    """Where a signal / parameter / macro / other name appears in the compiled RTL and testbench (whole words)."""
    files = [str(f) for f in sources.design_files(cfg)]
    hits = []
    try:
        r = subprocess.run(["grep", "-n", "-w", "-F", "-m", "5", "--", q, *files],
                           capture_output=True, text=True, timeout=60)
        for l in r.stdout.splitlines():
            f, ln, txt = l.split(":", 2)
            kind = ("declaration" if _DEFINING_RE.match(txt) else
                    "conditional" if _CONDITIONAL_RE.match(txt) else "use")
            hits.append({"file": sources.rel(cfg, f), "line": int(ln), "text": txt.strip()[:120], "kind": kind})
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    order = {"declaration": 0, "conditional": 1, "use": 2}
    hits.sort(key=lambda h: order[h["kind"]])  # declarations, then `ifdef tests, then uses (file order kept)
    out = {"hits": hits[:15], "total": len(hits), "files": len({h["file"] for h in hits}),
           "note": ("no module, typedef or address window of that name; whole-word matches in the compiled RTL, "
                    "testbench and include headers (at most 5 per file)" if hits else
                    "no module, typedef, address window or source line with that name in the compiled design")}
    # macros set on the compiler command line have no `define anywhere
    cs = cfg.work / "compile.sh"
    if cs.exists() and re.search(rf"-d {re.escape(q)}(=\S*)?(\s|$)", cs.read_text()):
        out["compile_define"] = (f"{q} is defined on the xvlog command line (work/compile.sh, from "
                                 f"hardware/xsim/env.sh or a bender target)")
    return out


# ---------------------------------------------------------------- soc_bootflow
def soc_bootflow() -> dict:
    return {
        "what_is_simulated": "the AlSaqr SoC RTL (4 CVA6 cores, coherency unit, last-level cache, on-chip SRAM, "
                             "debug module, interrupt controllers, peripherals) in Vivado xsim. DRAM is not a chip model: "
                             "it is a plain byte array attached behind the last-level cache, zero unless written. "
                             "The console is a mock UART that prints each line the software writes.",
        "memory": {"DRAM (array)": "0x8000_0000, 512 MB: program code, data and stack",
                   "on-chip SRAM": "0x1C00_0000, 32 KB: the tohost word (exit code) and small shared variables",
                   "boot ROM": "0x1_0000: every core starts here after reset",
                   "SCMI mailbox": "0x1040_4000: word 0 = address a woken core jumps to; +0x24 raises the wake-up interrupt",
                   "PLIC": "0x0C00_0000 (context 2*hart+1 = machine mode of that hart)",
                   "UART": "0x4000_0000 (the mock UART the console lines come from)"},
        "how_a_program_runs": [
            {"t_ns": 0, "who": "testbench", "what": "reset and clocks; cores are held in reset for ~1.5 ms"},
            {"t_ns": 1000000, "who": "testbench JTAG", "what": "debug module initialised -> marker '[JTAG] Initialization success'"},
            {"t_ns": 1550000, "who": "cores", "what": "all four cores start in the boot ROM; cores 1-3 sleep (wfi) until a wake-up interrupt"},
            {"t_ns": 2000000, "who": "testbench JTAG", "what": "core 0 halted -> '[JTAG] Halted hart 0'"},
            {"t_ns": 2110000, "who": "testbench", "what": "the ELF is written into the DRAM array and the SRAM -> '[XSIM-L3] section ...' / '[XSIM-L2] section ...'"},
            {"t_ns": 2490000, "who": "testbench JTAG", "what": "core 0 resumed at the entry -> '[JTAG] Resumed hart 0 from 0x80000000'"},
            {"t_ns": 2530000, "who": "core 0 startup code", "what": "UART setup; writes the mailbox so core 1 wakes and jumps to the entry"},
            {"t_ns": 2560000, "who": "core 0", "what": "first console line (for a hello test)"},
            {"t_ns": 2620000, "who": "test code", "what": "tests that use cores 2 and 3 arm APMU counters whose overflow interrupts wake them"},
            {"t_ns": 2840000, "who": "testbench JTAG", "what": "reads the tohost word; exit code 0 -> '[JTAG] SUCCESS', else '[JTAG] FAILED'; then $finish"},
        ],
        "wakeups": {"core 1": "SCMI mailbox interrupt, raised by core 0's startup code",
                    "cores 2, 3": "APMU counter-overflow interrupts (PLIC sources 156, 157), raised by test code",
                    "any core": "the testbench can also halt/resume a core over JTAG"},
        "if_something_goes_wrong": {
            "no '[JTAG] Halted hart 0'": "core 0 never entered debug mode: debug module or JTAG path",
            "sim_status says stalled": "simulated time stopped advancing; sim_stall_trace shows what was executing",
            "console lines then no SUCCESS": "the program never wrote tohost, or wrote a non-zero exit code (FAILED)",
            "a secondary core never prints": "its wake-up interrupt never fired, or it jumped before the program was loaded"},
        "markers_in_order": ["[JTAG] Initialization success", "[JTAG] Halted hart 0", "[XSIM-L3] section at 0x80000000",
                             "[JTAG] Resumed hart 0", "Mock uart ...", "[JTAG] SUCCESS", "$finish"],
        "speed": {"sim_run": "about 40 s of wall clock per simulated millisecond",
                  "sim_session / sim_stall_trace": "about 80 s per simulated millisecond (debug-visible build)"},
    }
