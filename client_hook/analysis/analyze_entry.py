"""Instruction-align the fixed 4.1.9.30 entry's direct callers, offline only."""
from __future__ import annotations

from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import argparse
from tool.settings import executable_setting, path_setting

import hashlib
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
from hook_runtime.pe_local import PE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--objdump", default=executable_setting('RE_WECHAT_OBJDUMP', 'objdump'))
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "build/static-41930")
    args = parser.parse_args()
    pe = PE(str(args.artifact))
    digest = hashlib.sha256(pe.data).hexdigest()
    profile = json.loads((Path(__file__).resolve().parents[1] / "profiles/weixin-4.1.9.30.json").read_text())
    if digest != profile["sha256"] or pe.machine != 0x8664:
        raise SystemExit("fixed artifact identity mismatch")
    args.output.mkdir(parents=True, exist_ok=True)
    cache = {}

    def disassemble(start, end):
        key = start, end
        if key not in cache:
            raw = subprocess.check_output([args.objdump, "-d", "-M", "intel", "--insn-width=16",
                f"--start-address={pe.image_base+start:#x}", f"--stop-address={pe.image_base+end:#x}",
                str(pe.path)], text=True, errors="replace")
            (args.output / f"aligned-{start:x}.asm").write_text(raw)
            lines = []
            for line in raw.splitlines():
                m = re.match(r"\s*([0-9a-f]+):\s*((?:[0-9a-f]{2}\s+)+)\s*(.*)", line)
                if m:
                    lines.append((int(m[1], 16)-pe.image_base, m[3]))
            cache[key] = lines
        return cache[key]

    callers = []
    for ref in pe.relbranches([0x9dbbf0], b"\xe8\xe9"):
        f = ref["function"]
        # Known tiny leaf has no pdata; disassemble its aligned start explicitly.
        start, end = (f["begin_rva"], f["end_rva"]) if f else (0x9e17d0, 0x9e17e2)
        instructions = disassemble(start, end)
        rva = ref["instruction_rva"]
        found = [i for i, item in enumerate(instructions) if item[0] == rva and
                 item[1].startswith(("call", "jmp")) and "1809dbbf0" in item[1]]
        i = found[0] if found else 0
        callers.append({"site_rva": hex(rva), "opcode": ref["opcode"],
                        "caller_start_rva": hex(start), "instruction_aligned": bool(found),
                        "context": [{"rva": hex(a), "instruction": b}
                                    for a,b in instructions[max(0,i-4):i+5]]})
    for start in (0x9dbbf0, 0x9da310, 0x9dbd90, 0x9dfdb0):
        f = pe.function(start)
        disassemble(start, f["end_rva"])
    at = pe.rva_to_offset(0x9dbbf0)
    report = {"schema": "client-hook-entry-analysis-v1", "artifact_sha256": digest,
              "scope": "offline_objdump_and_pe_scan_no_execution", "callers": callers,
              "entry_first_16_hex": pe.data[at:at+16].hex(),
              "limitations": ["direct rel32 calls/jumps only; indirect references excluded",
                              "alignment is checked; source-level names/types are inferred"]}
    (args.output / "entry-analysis.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"candidates": len(callers), "aligned": sum(c["instruction_aligned"] for c in callers),
                      "entry_first_16_hex": report["entry_first_16_hex"]}))


if __name__ == "__main__":
    main()
