"""固定版本真实 PE 的离线 profile。只读文件，不加载或执行 DLL。"""
import hashlib
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hook_runtime.pe_local import PE
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tool.settings import path_setting

EXPECTED_SHA256 = "89fc940377a779babbd64c9cd4abe6256663681d92f12802aa2df97a8db18494"
FUNCTIONS = [0x1F930, 0x20F20, 0x1854B0, 0x291280,
             0x30733D0, 0x30733D0, 0x30733F0, 0x3073420, 0x95A0,
             0x37030, 0xA640, 0xA650, 0x20E30, 0x300340, 0x63F7E0, 0x63F6C0, 0x32CD0,
             0x166C970, 0x65688F8]


def main() -> None:
    image_path, header_path = map(Path, sys.argv[1:])
    pe = PE(str(image_path))
    digest = hashlib.sha256(pe.data).hexdigest()
    assert digest == EXPECTED_SHA256 and pe.size_of_image == 0xAD2E000 and pe.machine == 0x8664
    vt_rva = 0x86D02E8
    slots = struct.unpack_from("<6Q", pe.data, pe.rva_to_offset(vt_rva))
    assert list(slots) == [pe.image_base + rva for rva in FUNCTIONS[4:9] + [0x95B0]]
    queue_control_rva, result_control_rva = 0x8041A68, 0x81F7378
    queue_slots = struct.unpack_from("<2Q", pe.data, pe.rva_to_offset(queue_control_rva))
    result_slots = struct.unpack_from("<2Q", pe.data, pe.rva_to_offset(result_control_rva))
    assert list(queue_slots) == [pe.image_base + 0x37030, pe.image_base + 0xA650]
    assert list(result_slots) == [pe.image_base + 0xA640, pe.image_base + 0xA650]
    module_path = str(path_setting('RE_WECHAT_WEIXIN_DLL', image_path.resolve()))
    output = ['#define QUEUE_REAL_MODULE_PATH L' + json.dumps(module_path),
              "#define QUEUE_REAL_IMAGE_SIZE 0xAD2E000u",
              "#define QUEUE_REAL_FN_VTABLE_RVA 0x86D02E8u",
              "#define QUEUE_REAL_QUEUE_CTRL_VTABLE_RVA 0x8041A68u",
              "#define QUEUE_REAL_RESULT_CTRL_VTABLE_RVA 0x81F7378u",
              "#define QUEUE_REAL_RELEASE_READY 1",
              "#define NATIVE_REAL_CONTEXT_VTABLE_RVA 0x83A8BE8u",
              "#define NATIVE_REAL_TEXT_VTABLE_RVA 0x813D8F8u",
              "#define NATIVE_REAL_TEXT_CONTROL_RVA 0x813D868u",
              "static const unsigned char queue_real_sha256[32] = {" +
              ",".join(f"0x{x:02x}" for x in bytes.fromhex(digest)) + "};",
              "static const struct profile_entry queue_real_functions[19] = {"]
    entries = []
    for rva in FUNCTIONS:
        off = pe.rva_to_offset(rva)
        entry = pe.data[off:off + 16]
        assert len(entry) == 16
        output.append("    {0x%xu,{%s}}," % (rva, ",".join(f"0x{x:02x}" for x in entry)))
        entries.append({"rva": hex(rva), "entry_bytes": entry.hex()})
    output.append("};\n")
    header_path.write_text("\n".join(output), encoding="utf-8")
    header_path.with_suffix(".json").write_text(json.dumps({
        "artifact": str(image_path.resolve()), "sha256": digest,
        "module_path": module_path,
        "image_size": hex(pe.size_of_image), "native_wrapper_vtable_rva": hex(vt_rva),
        "native_wrapper_slots": [hex(slot - pe.image_base) for slot in slots],
        "functions": entries, "client_calls_enabled": True,
        "queue_control_vtable_rva": hex(queue_control_rva),
        "result_control_vtable_rva": hex(result_control_rva),
        "queue_control_slots": [hex(slot - pe.image_base) for slot in queue_slots],
        "result_control_slots": [hex(slot - pe.image_base) for slot in result_slots],
        "scope": "client_hook generic text sender profile; per-operation queue and ownership validation",
        "client_submit_enabled": True,
    }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
