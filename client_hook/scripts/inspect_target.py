"""根据静态配置校验磁盘产物，从不加载 DLL。"""
from __future__ import annotations

from pathlib import Path
import sys

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import argparse
import hashlib
import json
import struct

ROOT = Path(__file__).resolve().parents[2]
from hook_runtime.pe_local import PE


def inspect(path: Path) -> dict:
    profile = json.loads((Path(__file__).resolve().parents[1] / "profiles" / "weixin-4.1.9.30.json").read_text())
    pe = PE(str(path))
    digest = hashlib.sha256(pe.data).hexdigest()
    checks = {"sha256": digest == profile["sha256"],
              "amd64": pe.machine == int(profile["machine"], 0)}
    # 不得在其他二进制文件中解释这些固定偏移。
    details = {}
    if all(checks.values()):
        rva = int(profile["observation_call_rva"], 0)
        expected = bytes.fromhex(profile["observation_expected_hex"])
        at = pe.rva_to_offset(rva)
        value = pe.data[at:at + len(expected)]
        checks["observation_bytes"] = value == expected
        target = rva + 5 + struct.unpack_from("<i", value, 1)[0] if value[:1] == b"\xe8" else -1
        checks["relative_call_target"] = target == int(profile["call_target_rva"], 0)
        function = pe.function(target) if target >= 0 else None
        checks["candidate_pdata_entry"] = bool(function and function["begin_rva"] == target)
        entry_at = pe.rva_to_offset(target)
        entry_expected = bytes.fromhex(profile["entry_expected_hex"])
        checks["entry_bytes"] = pe.data[entry_at:entry_at+len(entry_expected)] == entry_expected
        checks["allowed_return"] = rva + 5 == int(profile["allowed_return_rva"], 0)
        details = {"observation_bytes": value.hex(), "call_target_rva": hex(target),
                   "candidate_function": function}
    return {"schema": "client-hook-artifact-check-v1", "matched": all(checks.values()),
            "artifact": str(path.resolve()), "sha256": digest, "checks": checks,
            "details": details, "scope": "disk_only_no_target_execution",
            "live_hook_supported": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    try:
        report = inspect(args.artifact)
    except (OSError, ValueError, struct.error) as error:
        print(json.dumps({"matched": False, "error": str(error)}))
        return 3
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["matched"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
