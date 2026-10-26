"""
Atalla ISA Bootloader
Converts ELF to .hex
"""

"""
Key for .hex values
    :LLAAAATT[DATA...]CC

    LL Data Length (1 byte, hex-encoded): Number of data bytes in this line, should be 0x05 = 40 bits).

    AAAA Address (2 bytes, hex-encoded): The 16-bit load address offset.

    TT Record Type (1 byte, hex-encoded): Tells the loader how to interpret the data.
        00 = Data Record
        01 = End of file
        04 = Extend Linear Address Record (I don't think we have this)

    [DATA] Payload (LL bytes): The actual machine code or address extension bytes.

    CC Checksum (1 byte, hex-encoded): Verification byte. 
        1. Sum the length, address, record type, and data.
        2. Isolate lower 8 bits
        3. Bitwise inversion
"""

from dataclasses import dataclass

ISA_BYTES = 5
EOF = ":00000001FF"

@dataclass
class Hexcode:
    byte_count: int
    address: int
    record_type: int
    data: bytes
    checksum: int

def extract_bits(value, start, end):
    """Extract bits [start:end) from value (LSB = bit 0)"""
    mask = (1 << (end - start)) - 1
    return (value >> start) & mask

def bytes_to_insn_int(data):
    """Convert ISA_BYTES bytes to an instruction integer (little-endian)."""
    return int.from_bytes(data, byteorder='little')

def sign_extend(value, bits):
    """Sign extend a value from 'bits' width to full int"""
    sign_bit = 1 << (bits - 1)
    if value & sign_bit:
        return value - (1 << bits)
    return value

def get_elf_sections(data):
    """Return a list of ELF section descriptors.
    Each descriptor has: name (bytes), type (int), flags (int), off (int), size (int)
    """
    if len(data) < 52 or data[0:4] != b"\x7fELF":
        return []

    e_shoff = int.from_bytes(data[32:36], byteorder="little")
    e_shentsize = int.from_bytes(data[46:48], byteorder="little")
    e_shnum = int.from_bytes(data[48:50], byteorder="little")
    e_shstrndx = int.from_bytes(data[50:52], byteorder="little")

    if not (e_shoff and e_shentsize and e_shnum and e_shstrndx < e_shnum):
        return []

    sh_table_end = e_shoff + (e_shentsize * e_shnum)
    if sh_table_end > len(data):
        return []

    shstr_hdr = e_shoff + (e_shstrndx * e_shentsize)
    shstr_off = int.from_bytes(data[shstr_hdr + 16:shstr_hdr + 20], byteorder="little")
    shstr_size = int.from_bytes(data[shstr_hdr + 20:shstr_hdr + 24], byteorder="little")
    if shstr_off + shstr_size > len(data):
        return []

    shstr = data[shstr_off:shstr_off + shstr_size]
    sections = []
    for i in range(e_shnum):
        sh = e_shoff + (i * e_shentsize)
        name_off = int.from_bytes(data[sh:sh + 4], byteorder="little")
        sec_type = int.from_bytes(data[sh + 4:sh + 8], byteorder="little")
        sec_flags = int.from_bytes(data[sh + 8:sh + 12], byteorder="little")
        sec_off = int.from_bytes(data[sh + 16:sh + 20], byteorder="little")
        sec_size = int.from_bytes(data[sh + 20:sh + 24], byteorder="little")

        if name_off >= len(shstr):
            continue
        end = shstr.find(b"\x00", name_off)
        if end == -1:
            continue

        name = shstr[name_off:end]
        if 0 <= sec_off <= len(data) and 0 <= sec_off + sec_size <= len(data):
            sections.append(
                {
                    "name": name,
                    "type": sec_type,
                    "flags": sec_flags,
                    "off": sec_off,
                    "size": sec_size,
                }
            )

    return sections

def get_code_bounds(data):
    """Infer code bounds from ELF metadata, preferring the .text section."""
    if len(data) < 52 or data[0:4] != b"\x7fELF":
        return 0, len(data)
    
    # ELF32 header fields used by current Atalla output.
    e_shoff = int.from_bytes(data[32:36], byteorder="little")
    e_ehsize = int.from_bytes(data[40:42], byteorder="little")
    # e_shentsize = int.from_bytes(data[46:48], byteorder="little")
    # e_shnum = int.from_bytes(data[48:50], byteorder="little")
    # e_shstrndx = int.from_bytes(data[50:52], byteorder="little")

    for sec in get_elf_sections(data):
        is_named_code = sec["name"] in (b".text", b"text", b"code")
        is_exec_progbits = sec["type"] == 1 and (sec["flags"] & 0x4) != 0
        if is_named_code or is_exec_progbits:
            text_start = sec["off"]
            text_end = sec["off"] + sec["size"]
            if 0 <= text_start < text_end <= len(data):
                return text_start, text_end

    code_start = e_ehsize if 0 < e_ehsize <= len(data) else 0
    code_end = e_shoff if code_start < e_shoff <= len(data) else len(data)

    if code_start >= code_end:
        return 0, len(data)

    return code_start, code_end

def get_data_bounds(data):
    """Infer data bounds from ELF metadata, preferring .data/data section."""
    for sec in get_elf_sections(data):
        is_named_data = sec["name"] in (b".data", b"data")
        # PROGBITS and writable usually indicates initialized data.
        is_writable_progbits = sec["type"] == 1 and (sec["flags"] & 0x1) != 0
        if is_named_data or is_writable_progbits:
            data_start = sec["off"]
            data_end = sec["off"] + sec["size"]
            if 0 <= data_start < data_end <= len(data):
                return data_start, data_end
    return None, None


def calculate_checksum(byte_list):
    total_sum = sum(byte_list)
    checksum = (~total_sum + 1) & 0xFF
    return checksum

def get_memory(data, code_start, code_end, data_start, data_end):
    hex_records: list[Hexcode] = []
    offset = code_start
    
    # Code Section
    # We will be assuming instructions start at address 0x0000
    while offset < code_end:
        insn_bytes = data[offset:min(offset + ISA_BYTES, code_end)]
        address = offset - code_start
        byte_count = len(insn_bytes)
        record_type = 0x00
        checksum = calculate_checksum([
            byte_count,
            (address >> 8) & 0xFF,
            address & 0xFF,
            record_type,
            *insn_bytes,
        ])
        hex_records.append(Hexcode(
            byte_count=byte_count,
            address=address,
            record_type=record_type,
            data=insn_bytes,
            checksum=checksum,
        ))
        offset += ISA_BYTES

    # Data Section
    # We will be assuming instructions start at address 0x2000
    if data_start is not None and data_end is not None:
        offset = data_start
        while offset < data_end:
            chunk = data[offset:min(offset + 4, data_end)]
            address = offset - data_start + 0x2000
            byte_count = len(chunk)
            record_type = 0x00
            checksum = calculate_checksum([
                byte_count,
                (address >> 8) & 0xFF,
                address & 0xFF,
                record_type,
                *chunk,
            ])
            hex_records.append(Hexcode(
                byte_count=byte_count,
                address=address,
                record_type=record_type,
                data=chunk,
                checksum=checksum,
            ))
            offset += 4
    return hex_records

def hex_to_string(record: Hexcode):
    fields = (
        bytes([record.byte_count])
        + record.address.to_bytes(2, byteorder="big")
        + bytes([record.record_type])
        + record.data
        + bytes([record.checksum])
    )
    return ":" + fields.hex().upper()

def elf_to_hex(input_file, output_file):
    with open(input_file, 'rb') as f:
        data = f.read()

    code_start, code_end = get_code_bounds(data)
    data_start, data_end = get_data_bounds(data)

    hex_records = get_memory(data, code_start, code_end, data_start, data_end)
    hex_records.sort(key = lambda x:x.address)

    with open(output_file, 'w') as out:
        for record in hex_records:
            hex_str = hex_to_string(record)
            out.write(f"{hex_str}\n")
        out.write(EOF)

if __name__ == "__main__":
    elf_to_hex("output.elf", "meminit.hex")
    print("ELF converted to hex and written to meminit.hex")
