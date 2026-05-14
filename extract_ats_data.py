#!/usr/bin/env python3
"""Diagnostic: try multiple header layouts to find the correct one."""
import struct, sys, os
from pathlib import Path

path = Path(sys.argv[1]) / "def.scs" if len(sys.argv) > 1 else \
       Path(r"D:\SteamLibrary\steamapps\common\American Truck Simulator\def.scs")

file_size = os.path.getsize(path)
print(f"File: {path}")
print(f"Size: {file_size:,} bytes ({file_size / 1024 / 1024:.1f} MB)\n")

with open(path, "rb") as f:
    raw = f.read(128)

print("First 128 bytes:")
for i in range(0, 128, 16):
    hex_part = " ".join(f"{b:02x}" for b in raw[i:i+16])
    print(f"  {i:4d}: {hex_part}")
print()

# Try header sizes 16, 20, 24 and see which gives sane entry offsets
for hdr_size in (16, 20, 24):
    print(f"--- Trying {hdr_size}-byte header ---")
    if hdr_size == 16:
        magic, ver, salt, htype, ecount = struct.unpack_from("<4sHHII", raw, 0)
        extra = None
    elif hdr_size == 20:
        magic, ver, salt, htype, ecount, extra = struct.unpack_from("<4sHHIII", raw, 0)
    else:
        magic, ver, salt, htype, ecount, extra1, extra2 = struct.unpack_from("<4sHHIIII", raw, 0)
        extra = extra1

    print(f"  magic={magic} ver={ver} salt={salt} htype={htype:#010x} ecount={ecount} extra={extra}")

    ok_count = 0
    for i in range(min(5, ecount)):
        pos = hdr_size + i * 32
        if pos + 32 > len(raw):
            break
        h, off, fl, crc, sz, csz = struct.unpack_from("<QQIIII", raw, pos)
        sane = (off < file_size) and (csz <= file_size) and (sz <= 100_000_000)
        marker = "✓" if sane else "✗"
        print(f"  [{i}] {marker} hash={h:#018x} offset={off:#012x}({off:,}) flags={fl} size={sz} comp={csz}")
        if sane:
            ok_count += 1
    print(f"  → {ok_count} sane-looking entries\n")
