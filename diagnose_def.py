#!/usr/bin/env python3
"""Diagnostic: inspect def.scs hash table entry format."""
import struct
import sys

path = (
    sys.argv[1]
    if len(sys.argv) > 1
    else r"D:\SteamLibrary\steamapps\common\American Truck Simulator\def.scs"
)

with open(path, "rb") as f:
    f.seek(0, 2)
    sz = f.tell()
    print(f"File size: {sz:,} bytes ({sz/1024/1024:.1f} MB)")

    f.seek(0)
    header = f.read(20)
    _ver, _salt, _hm, entry_count, start_offset = struct.unpack_from("<HHIII", header, 4)
    print(f"Version={_ver}  entry_count={entry_count:,}  start_offset={start_offset}")

    f.seek(start_offset)
    raw = f.read(512)

print(f"\nFirst 512 bytes at table offset {start_offset}:")
print(raw.hex())
print()

for es in [16, 20, 24, 28, 32, 36, 40]:
    found = []
    for i in range(len(raw) // es):
        chunk = raw[i * es : i * es + es]
        h = struct.unpack_from("<Q", chunk, 0)[0]
        if h != 0:
            found.append((i, h, chunk[8:].hex()))
    if found:
        print(f"{es}-byte entries: {len(found)} non-empty slots in first 512 bytes")
        for idx, h, rest in found[:5]:
            print(f"  slot {idx:3d}: hash={h:016x}  rest={rest}")
    else:
        print(f"{es}-byte entries: 0 non-empty (all zero hashes in first 512 bytes)")
