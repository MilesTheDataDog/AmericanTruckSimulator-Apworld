#!/usr/bin/env python3
"""Diagnostic: inspect def.scs structure at key offsets."""
import struct
import sys
import zlib

path = (
    sys.argv[1]
    if len(sys.argv) > 1
    else r"D:\SteamLibrary\steamapps\common\American Truck Simulator\def.scs"
)

with open(path, "rb") as f:
    f.seek(0, 2)
    file_size = f.tell()
    print(f"File size: {file_size:,} bytes ({file_size/1024/1024:.1f} MB)")

    f.seek(0)
    header = f.read(32)

_ver, _salt, _hm, entry_count, start_offset = struct.unpack_from("<HHIII", header, 4)
print(f"Header: version={_ver}  entry_count={entry_count:,}  start_offset={start_offset}")
print(f"Extra header bytes 20-31: {header[20:32].hex()}")
print()

def show_offset(f, label, offset, length=128):
    f.seek(offset)
    data = f.read(length)
    print(f"=== {label} (offset {offset}, {length} bytes) ===")
    print(data.hex())
    # Check for zlib
    for i in range(len(data) - 1):
        if data[i] == 0x78 and data[i+1] in (0x01, 0x5E, 0x9C, 0xDA):
            print(f"  zlib magic at byte {i}: {data[i:i+2].hex()}")
            try:
                dec = zlib.decompress(data[i:])
                print(f"  zlib decompresses OK → {len(dec)} bytes")
                print(f"  Decompressed text preview: {repr(dec[:200])}")
            except zlib.error as e:
                print(f"  zlib error: {e}")
    print()

with open(path, "rb") as f:
    show_offset(f, "offset 20 (right after header)", 20)
    show_offset(f, f"offset {start_offset} (start_offset field)", start_offset)
    show_offset(f, "offset 32 (after possible 32-byte header)", 32)

    # Try to find zlib streams in first 1000 bytes
    f.seek(0)
    first_kb = f.read(1000)
    print("=== Searching first 1000 bytes for zlib magic ===")
    for i in range(len(first_kb) - 1):
        if first_kb[i] == 0x78 and first_kb[i+1] in (0x01, 0x5E, 0x9C, 0xDA):
            print(f"  zlib magic at byte {i}: {first_kb[i:i+2].hex()}")

    # Try entry sizes at offset 20 with looser validity check
    print()
    print("=== Entry scan at offset 20 (all entry sizes, loose check) ===")
    f.seek(20)
    scan_data = f.read(start_offset - 20)
    for es in [20, 24, 28, 32, 36, 40]:
        fmt_map = {
            20: ("<Q I I I", 8, 4, 4, 4, 0),    # hash, off32, flags, size
            24: ("<Q I I I I", 8, 4, 4, 4, 4),   # hash, off32, flags, crc, size
            28: ("<Q I I I I I", 8, 4, 4, 4, 4, 4),
            32: ("<Q Q I I I I", 8, 8, 4, 4, 4, 4),
            36: ("<Q Q I I I I I", 8, 8, 4, 4, 4, 4, 4),
            40: ("<Q Q Q I I I", 8, 8, 8, 4, 4, 4),
        }
        valid = 0
        samples = []
        for i in range(len(scan_data) // es):
            chunk = scan_data[i * es : i * es + es]
            if len(chunk) < es:
                break
            h = struct.unpack_from("<Q", chunk, 0)[0]
            if h == 0:
                continue
            # Try to extract offset from bytes 8-11 (32-bit) or 8-15 (64-bit)
            off32 = struct.unpack_from("<I", chunk, 8)[0]
            off64 = struct.unpack_from("<Q", chunk, 8)[0] if es >= 24 else 0
            # Loose check: offset somewhere in file
            if (start_offset <= off32 < file_size) or (start_offset <= off64 < file_size):
                valid += 1
                if len(samples) < 3:
                    off = off64 if (start_offset <= off64 < file_size) else off32
                    samples.append((i, h, off, chunk[8:].hex()))
        print(f"  {es}-byte entries: {valid} valid (offset in [{start_offset}, {file_size}])")
        for idx, h, off, rest in samples:
            print(f"    slot {idx:5d}: hash={h:016x}  offset={off}  rest={rest}")
    print()
    print("Done.")
