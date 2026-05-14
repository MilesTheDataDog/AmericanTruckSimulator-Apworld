#!/usr/bin/env python3
"""Quick diagnostic — prints raw header and first few entries of def.scs"""
import struct, sys
from pathlib import Path

path = Path(sys.argv[1]) / "def.scs" if len(sys.argv) > 1 else Path(r"D:\SteamLibrary\steamapps\common\American Truck Simulator\def.scs")

with open(path, "rb") as f:
    header = f.read(32)
    print("Raw header (32 bytes):")
    print(" ".join(f"{b:02x}" for b in header))
    print()

    # Try 16-byte header
    magic   = header[0:4]
    ver     = struct.unpack_from("<H", header, 4)[0]
    salt    = struct.unpack_from("<H", header, 6)[0]
    htype   = struct.unpack_from("<I", header, 8)[0]
    ecount  = struct.unpack_from("<I", header, 12)[0]
    print(f"magic={magic}, version={ver}, salt={salt}, hash_type={htype:#010x}, entry_count={ecount}")
    print()

    # Print first 3 entries assuming they start at byte 16
    print("First 3 entries (assuming header=16 bytes, entry=32 bytes):")
    for i in range(3):
        pos = 16 + i * 32
        raw = header[pos:pos+32] if pos+32 <= 32 else None
        if raw is None:
            f.seek(pos)
            raw = f.read(32)
        h, off, fl, crc, sz, csz = struct.unpack("<QQIIII", raw)
        print(f"  [{i}] hash={h:#018x} offset={off:#018x} flags={fl:#010x} size={sz} comp={csz}")
