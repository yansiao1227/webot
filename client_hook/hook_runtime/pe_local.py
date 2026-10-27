"""Local PE parser derived from tools/pe_trace.py; no outside imports."""
import hashlib
import re
import bisect
import struct
from pathlib import Path
class PE:
    def __init__(self, path: str):
        self.path = Path(path).resolve()
        self.data = self.path.read_bytes()
        if self.data[:2] != b"MZ":
            raise ValueError("Not MZ")
        nt = self.u32(0x3c)
        if self.data[nt:nt + 4] != b"PE\0\0":
            raise ValueError("Not PE")
        self.machine, count = struct.unpack_from("<HH", self.data, nt + 4)
        optional_size = self.u16(nt + 20)
        optional = nt + 24
        if self.u16(optional) != 0x20b:
            raise ValueError("Only PE32+ is supported")
        self.image_base = struct.unpack_from("<Q", self.data, optional + 24)[0]
        self.size_of_image = self.u32(optional + 56)
        self.size_of_headers = self.u32(optional + 60)
        directory_count = self.u32(optional + 108)
        self.directories = [struct.unpack_from("<II", self.data, optional + 112 + 8 * i)
                            for i in range(min(directory_count, 16))]
        sections = optional + optional_size
        self.sections = []
        for i in range(count):
            start = sections + i * 40
            name = self.data[start:start + 8].split(b"\0", 1)[0].decode("ascii", "replace")
            virtual_size, rva, raw_size, raw_offset = struct.unpack_from("<IIII", self.data, start + 8)
            characteristics = self.u32(start + 36)
            self.sections.append(dict(name=name, rva=rva, virtual_size=virtual_size,
                                      raw_size=raw_size, raw_offset=raw_offset,
                                      executable=bool(characteristics & 0x20000000)))
        self.functions = []
        if len(self.directories) > 3:
            rva, size = self.directories[3]
            if rva and size:
                offset = self.rva_to_offset(rva)
                for i in range(size // 12):
                    begin, end, unwind = struct.unpack_from("<III", self.data, offset + 12 * i)
                    if begin < end:
                        self.functions.append((begin, end, unwind))
        self.functions.sort()
        self.function_starts = [f[0] for f in self.functions]

    def u16(self, offset: int) -> int:
        return struct.unpack_from("<H", self.data, offset)[0]

    def u32(self, offset: int) -> int:
        return struct.unpack_from("<I", self.data, offset)[0]

    def rva_to_offset(self, rva: int) -> int:
        if 0 <= rva < self.size_of_headers:
            return rva
        for section in self.sections:
            relative = rva - section["rva"]
            if 0 <= relative < section["raw_size"]:
                return section["raw_offset"] + relative
        raise ValueError(f"RVA {rva:#x} has no raw bytes")

    def offset_to_rva(self, offset: int) -> int:
        if 0 <= offset < self.size_of_headers:
            return offset
        for section in self.sections:
            relative = offset - section["raw_offset"]
            if 0 <= relative < section["raw_size"]:
                return section["rva"] + relative
        raise ValueError(f"Offset {offset:#x} outside section raw data")

    def function(self, rva: int):
        index = bisect.bisect_right(self.function_starts, rva) - 1
        if index >= 0:
            begin, end, unwind = self.functions[index]
            if begin <= rva < end:
                return dict(begin_rva=begin, end_rva=end, unwind_rva=unwind,
                            size=end - begin)
        return None

    def find(self, value: bytes):
        output = []
        offset = 0
        while True:
            offset = self.data.find(value, offset)
            if offset < 0:
                break
            try:
                rva = self.offset_to_rva(offset)
            except ValueError:
                rva = None
            output.append(dict(file_offset=offset, rva=rva))
            offset += 1
        return output

    def riprefs(self, targets: list[int], radius: int = 0):
        output = []
        # REX + LEA/MOV + ModRM with mod=00/rm=101 + signed disp32.
        # A byte occurrence may be inside another instruction; objdump validation
        # from the .pdata function start is needed to establish alignment.
        pattern = re.compile(rb"[\x48-\x4f][\x8d\x8b\x89][\x05\x0d\x15\x1d\x25\x2d\x35\x3d]....", re.DOTALL)
        for section in self.sections:
            if not section["executable"]:
                continue
            raw = self.data[section["raw_offset"]:section["raw_offset"] + section["raw_size"]]
            for match in pattern.finditer(raw):
                instruction_rva = section["rva"] + match.start()
                instruction = match.group()
                displacement = struct.unpack_from("<i", instruction, 3)[0]
                target_rva = instruction_rva + 7 + displacement
                matching_targets = [t for t in targets if abs(t - target_rva) <= radius]
                if matching_targets:
                    output.append(dict(instruction_rva=instruction_rva,
                                       instruction_file_offset=section["raw_offset"] + match.start(),
                                       bytes=instruction.hex(" "), target_rva=target_rva,
                                       matching_targets=matching_targets,
                                       function=self.function(instruction_rva)))
        return output

    def relbranches(self, targets: list[int], kinds: bytes = b"\xe8"):
        output = []
        for section in self.sections:
            if not section["executable"]:
                continue
            raw = self.data[section["raw_offset"]:section["raw_offset"] + section["raw_size"]]
            for opcode in kinds:
                offset = 0
                while True:
                    offset = raw.find(bytes([opcode]), offset)
                    if offset < 0 or offset + 5 > len(raw):
                        break
                    instruction_rva = section["rva"] + offset
                    target_rva = instruction_rva + 5 + struct.unpack_from("<i", raw, offset + 1)[0]
                    if target_rva in targets:
                        output.append(dict(instruction_rva=instruction_rva,
                                           opcode=hex(opcode), target_rva=target_rva,
                                           function=self.function(instruction_rva)))
                    offset += 1
        return output

    def info(self):
        return dict(path=str(self.path), file_size=len(self.data),
                    sha256=hashlib.sha256(self.data).hexdigest(), machine=hex(self.machine),
                    image_base=self.image_base, size_of_image=self.size_of_image,
                    sections=self.sections, pdata_function_count=len(self.functions))

