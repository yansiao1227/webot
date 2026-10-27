"""离线读取本次构建的自建 PE；不加载或执行目标。"""
import hashlib
import json
import struct
import sys
from pathlib import Path

FUNCTIONS = ["FixtureRoot", "FixtureGetQueue", "FixtureMakeLocation", "FixturePost",
             "FixtureFnCopy", "FixtureFnMove", "FixtureFnInvoke", "FixtureFnType",
             "FixtureFnDelete", "FixtureCtrlDestroy", "FixtureCtrlDestroy", "FixtureCtrlDelete",
             "FixtureGetContext1", "FixtureGetContext2", "FixtureGetContext3", "FixtureTextFactory", "FixtureStringAssign",
             "FixtureSubmit", "FixtureSizedFree"]


def main() -> None:
    image_path, header_path = map(Path, sys.argv[1:])
    data = image_path.read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    assert data[:2] == b"MZ" and data[pe:pe + 4] == b"PE\0\0"
    machine, sections = struct.unpack_from("<HH", data, pe + 4)
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    assert machine == 0x8664 and struct.unpack_from("<H", data, optional)[0] == 0x20B
    image_size = struct.unpack_from("<I", data, optional + 56)[0]
    image_base = struct.unpack_from("<Q", data, optional + 24)[0]
    headers = struct.unpack_from("<I", data, optional + 60)[0]
    ranges = []
    for index in range(sections):
        off = optional + optional_size + 40 * index
        vsize, rva, rawsize, raw = struct.unpack_from("<IIII", data, off + 8)
        ranges.append((rva, max(vsize, rawsize), rawsize, raw))

    def at(rva: int, length: int) -> bytes:
        if rva < headers:
            offset = rva
        else:
            for section, _, rawsize, raw in ranges:
                if section <= rva and rva - section + length <= rawsize:
                    offset = raw + rva - section
                    break
            else:
                raise ValueError(f"unmapped RVA {rva:#x}")
        result = data[offset:offset + length]
        assert len(result) == length
        return result

    exports_rva, exports_size = struct.unpack_from("<II", data, optional + 112)
    exports = at(exports_rva, 40)
    _, _, _, _, _, _, function_count, name_count, functions_rva, names_rva, ordinals_rva = struct.unpack("<IIHHIIIIIII", exports)
    assert function_count < 1000 and name_count < 1000
    names = struct.unpack(f"<{name_count}I", at(names_rva, name_count * 4))
    ordinals = struct.unpack(f"<{name_count}H", at(ordinals_rva, name_count * 2))
    addresses = struct.unpack(f"<{function_count}I", at(functions_rva, function_count * 4))
    symbols = {}
    for name_rva, ordinal in zip(names, ordinals):
        name = bytearray()
        while len(name) < 128:
            value = at(name_rva + len(name), 1)[0]
            if not value:
                break
            name.append(value)
        symbol = name.decode("ascii")
        address = addresses[ordinal]
        assert not exports_rva <= address < exports_rva + exports_size
        symbols[symbol] = address
    required = FUNCTIONS + ["FixtureNativeVtable", "FixtureQueueControlVtable", "FixtureResultControlVtable",
                            "FixtureSendContextVtable", "FixtureTextVtable", "FixtureTextControlVtable", "FixtureExpectedWorkerTid"]
    for name in required:
        assert name in symbols, name
    native_slots = struct.unpack("<5Q", at(symbols["FixtureNativeVtable"], 40))
    control_slots = struct.unpack("<2Q", at(symbols["FixtureQueueControlVtable"], 16))
    result_slots = struct.unpack("<2Q", at(symbols["FixtureResultControlVtable"], 16))
    assert list(native_slots) == [image_base + symbols[name] for name in FUNCTIONS[4:9]]
    assert list(control_slots) == [image_base + symbols[name] for name in ("FixtureCtrlDestroy", "FixtureCtrlDelete")]
    assert result_slots == control_slots
    assert symbols["FixtureQueueControlVtable"] != symbols["FixtureResultControlVtable"]
    digest = hashlib.sha256(data).digest()
    path_literal = str(image_path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    output = ["/* Generated from self-built fixture PE; never supplied by caller. */",
              f'#define QUEUE_FIXTURE_MODULE_PATH L"{path_literal}"',
              f"#define QUEUE_FIXTURE_IMAGE_SIZE {image_size}u",
              f"#define QUEUE_FIXTURE_FN_VTABLE_RVA 0x{symbols['FixtureNativeVtable']:x}u",
              f"#define QUEUE_FIXTURE_QUEUE_CTRL_VTABLE_RVA 0x{symbols['FixtureQueueControlVtable']:x}u",
              f"#define QUEUE_FIXTURE_RESULT_CTRL_VTABLE_RVA 0x{symbols['FixtureResultControlVtable']:x}u",
              f"#define NATIVE_FIXTURE_CONTEXT_VTABLE_RVA 0x{symbols['FixtureSendContextVtable']:x}u",
              f"#define NATIVE_FIXTURE_TEXT_VTABLE_RVA 0x{symbols['FixtureTextVtable']:x}u",
              f"#define NATIVE_FIXTURE_TEXT_CONTROL_RVA 0x{symbols['FixtureTextControlVtable']:x}u",
              f"#define NATIVE_FIXTURE_EXPECTED_TID_RVA 0x{symbols['FixtureExpectedWorkerTid']:x}u",
              "static const unsigned char queue_fixture_sha256[32] = {" + ",".join(f"0x{x:02x}" for x in digest) + "};",
              "static const struct profile_entry queue_functions[19] = {"]
    for name in FUNCTIONS:
        rva = symbols[name]
        output.append("    {0x%xu,{%s}}, /* %s */" %
                      (rva, ",".join(f"0x{x:02x}" for x in at(rva, 16)), name))
    output.append("};\n")
    header_path.write_text("\n".join(output), encoding="utf-8")
    evidence = {"fixture_only": True, "module": str(image_path.resolve()), "sha256": digest.hex(),
                "image_size": image_size, "exports": {name: hex(symbols[name]) for name in required},
                "function_entry_bytes": {name: at(symbols[name], 16).hex() for name in FUNCTIONS}}
    header_path.with_suffix(".json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
